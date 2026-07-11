"""
Main Content Recovery Service for Reddit Stash.

This module provides the central orchestration for content recovery attempts,
managing multiple providers and implementing the recovery cascade strategy.
"""

import json
import time
import logging
from dataclasses import replace
from typing import Optional, Dict, Any, FrozenSet
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

from .recovery_metadata import (
    RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality, RecoveryAttempt
)
from .cache_manager import RecoveryCacheManager
from .providers import (
    WaybackMachineProvider,
    ArcticShiftProvider,
    PullPushProvider,
)
from ..feature_flags import get_media_config
from ..rate_limiter import rate_limit_manager


PARALLEL_RECOVERY_TIMEOUT_SECONDS = 30
NEGATIVE_CACHE_TTL_HOURS = 1


class ContentRecoveryService:
    """
    Central service for coordinating content recovery across multiple providers.

    Implements a sophisticated recovery cascade that tries multiple archival
    sources in order of reliability and success rate.
    """

    def __init__(self, config=None, cache_path: Optional[str] = None):
        self.config = config or get_media_config()
        self.cache_manager = RecoveryCacheManager(cache_path)
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Initialize providers
        self._init_providers()

        # Recovery statistics
        self._stats = {
            'total_attempts': 0,
            'cache_hits': 0,
            'successful_recoveries': 0,
            'failed_recoveries': 0,
            'provider_stats': {}
        }

    def _init_providers(self):
        """Initialize recovery providers based on configuration."""
        self.providers = {}

        # Get recovery configuration
        recovery_config = self.config.get_recovery_config()
        timeout = recovery_config.get('timeout_seconds', 10)
        self._parallel_timeout_seconds = timeout

        # Initialize providers based on configuration
        if recovery_config.get('use_wayback_machine', True):
            self.providers[RecoverySource.WAYBACK_MACHINE] = WaybackMachineProvider(timeout)
            self._logger.debug("Initialized Wayback Machine provider")

        if recovery_config.get('use_arctic_shift', True):
            self.providers[RecoverySource.ARCTIC_SHIFT] = ArcticShiftProvider(timeout)
            self._logger.debug("Initialized Arctic Shift provider")

        if recovery_config.get('use_pushshift_api', True):
            self.providers[RecoverySource.PULLPUSH_IO] = PullPushProvider(timeout)
            self._logger.debug("Initialized PullPush.io provider")

        if recovery_config.get('use_reddit_previews', False):
            self._logger.warning(
                "Reddit preview recovery is disabled because no authoritative preview discovery is available"
            )

        if recovery_config.get('use_reveddit_api', False):
            self._logger.warning(
                "Reveddit recovery is disabled because public pages no longer expose archive content"
            )

        if not self.providers:
            self._logger.warning("No recovery providers enabled")

    def attempt_recovery(self, url: str, original_failure_reason: Optional[str] = None,
                        async_mode: bool = True) -> RecoveryResult:
        """
        Attempt to recover content from the given URL using available providers.

        Args:
            url: Original URL that failed to download
            original_failure_reason: Reason why the original download failed
            async_mode: Whether to try providers in parallel (faster) or sequence

        Returns:
            RecoveryResult with the best recovery found, or failure if none found
        """
        start_time = time.time()
        self._stats['total_attempts'] += 1

        try:
            self._logger.info(f"Starting content recovery for: {url}")

            # Check cache first
            cached_result = self._check_cache(url)
            if cached_result:
                self._stats['cache_hits'] += 1
                self._logger.debug(f"Cache hit for recovery of: {url}")
                return cached_result

            # Clean up expired cache entries periodically
            if self._stats['total_attempts'] % 100 == 0:
                self.cache_manager.cleanup_expired_cache()

            # Try recovery providers
            if async_mode and len(self.providers) > 1:
                recovery_result = self._attempt_parallel_recovery(url, original_failure_reason)
            else:
                recovery_result = self._attempt_sequential_recovery(url, original_failure_reason)

            # Cache the result
            if recovery_result and recovery_result.success:
                self._cache_successful_result(url, recovery_result)
                self._stats['successful_recoveries'] += 1
            else:
                self._cache_negative_results(url, recovery_result.attempted_sources)
                self._stats['failed_recoveries'] += 1

            duration = time.time() - start_time
            self._logger.info(
                f"Recovery attempt completed in {duration:.2f}s: "
                f"{'SUCCESS' if recovery_result and recovery_result.success else 'FAILED'}"
            )

            return recovery_result

        except Exception as e:
            duration = time.time() - start_time
            self._logger.error(f"Unexpected error in content recovery: {e}")
            self._stats['failed_recoveries'] += 1

            return RecoveryResult.failure_result(
                f"Recovery system error: {str(e)} (duration: {duration:.2f}s)"
            )

    def _check_cache(self, url: str) -> Optional[RecoveryResult]:
        """Check if we have a cached recovery result for this URL."""
        try:
            successful_results = []
            negative_sources = set()
            quality_priorities = {
                RecoveryQuality.ORIGINAL: 5,
                RecoveryQuality.HIGH_QUALITY: 4,
                RecoveryQuality.MEDIUM_QUALITY: 3,
                RecoveryQuality.LOW_QUALITY: 2,
                RecoveryQuality.THUMBNAIL: 1,
                RecoveryQuality.METADATA_ONLY: 0,
            }
            provider_priorities = {
                RecoverySource.WAYBACK_MACHINE: 5,
                RecoverySource.ARCTIC_SHIFT: 4,
                RecoverySource.PULLPUSH_IO: 3,
                RecoverySource.REDDIT_PREVIEWS: 2,
                RecoverySource.REVEDDIT: 1,
            }

            for source in self.providers.keys():
                cached_entry = self.cache_manager.get_cached_result(url, source)
                if cached_entry and not cached_entry.is_expired:
                    additional_metadata = None
                    if cached_entry.metadata_json:
                        try:
                            additional_metadata = json.loads(cached_entry.metadata_json)
                        except (TypeError, json.JSONDecodeError):
                            self._logger.debug("Cached recovery metadata could not be decoded")

                    metadata = RecoveryMetadata(
                        source=RecoverySource(cached_entry.recovery_source),
                        recovered_url=cached_entry.recovered_url,
                        recovery_timestamp=cached_entry.cached_at,
                        content_quality=RecoveryQuality(cached_entry.content_quality),
                        cache_hit=True,
                        additional_metadata=additional_metadata,
                    )

                    if cached_entry.success:
                        successful_results.append(RecoveryResult.success_result(
                            cached_entry.recovered_url,
                            metadata,
                        ))
                    else:
                        negative_sources.add(source)

            if successful_results:
                return max(
                    successful_results,
                    key=lambda result: (
                        quality_priorities.get(result.metadata.content_quality, 0),
                        provider_priorities.get(result.metadata.source, 0),
                    ),
                )

            if set(self.providers).issubset(negative_sources):
                return RecoveryResult.failure_result("Cached negative result")

        except Exception as e:
            self._logger.debug(f"Cache check failed: {e}")

        return None

    def _attempt_sequential_recovery(self, url: str, failure_reason: Optional[str]) -> RecoveryResult:
        """Try recovery providers sequentially in order of reliability."""

        # Define provider order (most reliable first)
        provider_order = [
            RecoverySource.WAYBACK_MACHINE,    # Most reliable, no rate limits
            RecoverySource.ARCTIC_SHIFT,        # Current Reddit text archive
            RecoverySource.PULLPUSH_IO,        # Good for Reddit content
            RecoverySource.REDDIT_PREVIEWS,    # Lower quality but sometimes works
            RecoverySource.REVEDDIT            # Specific to moderator deletions
        ]

        completed_sources = set()

        for source in provider_order:
            if source not in self.providers:
                continue

            provider = self.providers[source]
            self._logger.debug(f"Trying recovery provider: {source.value}")

            if source == RecoverySource.PULLPUSH_IO and not rate_limit_manager.can_proceed(source.value):
                deferred_result = RecoveryResult.failure_result(
                    "PullPush.io recovery deferred by rate limiter",
                    source,
                )
                self._record_attempt(url, source, deferred_result, failure_reason)
                continue

            try:
                result = provider.attempt_recovery(url)
                completed_sources.add(source)

                # Record the attempt
                self._record_attempt(url, source, result, failure_reason)

                if result.success:
                    self._logger.info(f"Recovery successful via {source.value}: {result.recovered_url}")
                    return replace(
                        result,
                        attempted_sources=result.attempted_sources | frozenset(completed_sources),
                    )
                else:
                    self._logger.debug(f"Recovery failed via {source.value}: {result.error_message}")

            except Exception as e:
                completed_sources.add(source)
                self._logger.error(f"Provider {source.value} threw exception: {e}")
                # Continue to next provider

        # All providers failed
        return RecoveryResult.failure_result(
            "All recovery providers failed",
            attempted_sources=frozenset(completed_sources),
        )

    def _attempt_parallel_recovery(self, url: str, failure_reason: Optional[str]) -> RecoveryResult:
        """Collect parallel recovery results and select the highest-quality result."""

        provider_priorities = {
            RecoverySource.WAYBACK_MACHINE: 5,
            RecoverySource.ARCTIC_SHIFT: 4,
            RecoverySource.PULLPUSH_IO: 3,
            RecoverySource.REDDIT_PREVIEWS: 2,
            RecoverySource.REVEDDIT: 1,
        }
        quality_priorities = {
            RecoveryQuality.ORIGINAL: 5,
            RecoveryQuality.HIGH_QUALITY: 4,
            RecoveryQuality.MEDIUM_QUALITY: 3,
            RecoveryQuality.LOW_QUALITY: 2,
            RecoveryQuality.THUMBNAIL: 1,
            RecoveryQuality.METADATA_ONLY: 0,
        }

        # Submit all provider attempts to thread pool.
        executor = ThreadPoolExecutor(max_workers=min(4, len(self.providers)))
        completed_sources = set()
        try:
            future_to_source = {}

            for source, provider in self.providers.items():
                if source == RecoverySource.PULLPUSH_IO and not rate_limit_manager.can_proceed(source.value):
                    deferred_result = RecoveryResult.failure_result(
                        "PullPush.io recovery deferred by rate limiter",
                        source,
                    )
                    self._record_attempt(url, source, deferred_result, failure_reason)
                    continue
                future = executor.submit(provider.attempt_recovery, url)
                future_to_source[future] = source

            successful_results = []
            try:
                # Process every completed result before choosing the best quality.
                for future in as_completed(
                    future_to_source,
                    timeout=getattr(
                        self,
                        '_parallel_timeout_seconds',
                        PARALLEL_RECOVERY_TIMEOUT_SECONDS,
                    ),
                ):
                    source = future_to_source[future]

                    try:
                        result = future.result()
                        completed_sources.add(source)
                        self._record_attempt(url, source, result, failure_reason)

                        if result.success:
                            self._logger.info(
                                f"Recovery successful via {source.value}: {result.recovered_url}"
                            )
                            successful_results.append((source, result))
                        else:
                            self._logger.debug(
                                f"Recovery failed via {source.value}: {result.error_message}"
                            )

                    except Exception as e:
                        completed_sources.add(source)
                        self._logger.error(f"Provider {source.value} threw exception: {e}")
            except FuturesTimeoutError:
                for future in future_to_source:
                    future.cancel()
                self._logger.warning("Parallel recovery timed out before all providers returned")
        finally:
            # Provider requests have their own I/O timeouts. Do not make the caller wait
            # for an already-running provider after the overall recovery deadline expires.
            executor.shutdown(wait=False, cancel_futures=True)

        if successful_results:
            _, best_result = max(
                successful_results,
                key=lambda item: (
                    quality_priorities.get(
                        item[1].metadata.content_quality if item[1].metadata else RecoveryQuality.METADATA_ONLY,
                        0,
                    ),
                    provider_priorities.get(item[0], 0),
                ),
            )
            return replace(
                best_result,
                attempted_sources=best_result.attempted_sources | frozenset(completed_sources),
            )

        # All providers failed
        return RecoveryResult.failure_result(
            "All recovery providers failed",
            attempted_sources=frozenset(completed_sources),
        )

    def _record_attempt(self, url: str, source: RecoverySource, result: RecoveryResult,
                       failure_reason: Optional[str]):
        """Record recovery attempt for analytics."""
        try:
            attempt = RecoveryAttempt(
                original_url=url,
                recovery_source=source.value,
                success=result.success,
                recovered_url=result.recovered_url,
                error_message=result.error_message or failure_reason,
                duration_seconds=result.metadata.attempt_duration if result.metadata else None
            )

            self.cache_manager.record_attempt(attempt)

            # Update provider statistics
            if source.value not in self._stats['provider_stats']:
                self._stats['provider_stats'][source.value] = {'attempts': 0, 'successes': 0}

            self._stats['provider_stats'][source.value]['attempts'] += 1
            if result.success:
                self._stats['provider_stats'][source.value]['successes'] += 1

        except Exception as e:
            self._logger.debug(f"Failed to record recovery attempt: {e}")

    def _cache_successful_result(self, url: str, result: RecoveryResult):
        """Cache a successful recovery result."""
        try:
            if result.metadata:
                recovery_config = self.config.get_recovery_config()
                cache_hours = recovery_config.get('cache_duration_hours', 24)

                self.cache_manager.cache_result(
                    url=url,
                    source=result.metadata.source,
                    recovered_url=result.recovered_url,
                    quality=result.metadata.content_quality,
                    ttl_hours=cache_hours,
                    metadata=result.metadata.additional_metadata
                )

        except Exception as e:
            self._logger.debug(f"Failed to cache recovery result: {e}")

    def _cache_negative_results(
        self,
        url: str,
        completed_sources: FrozenSet[RecoverySource],
    ):
        """Cache completed provider failures without suppressing deferred providers."""
        try:
            for source in completed_sources:
                self.cache_manager.cache_result(
                    url=url,
                    source=source,
                    recovered_url=None,
                    quality=RecoveryQuality.METADATA_ONLY,
                    ttl_hours=NEGATIVE_CACHE_TTL_HOURS,
                    success=False,
                )
        except Exception as e:
            self._logger.debug(f"Failed to cache negative recovery result: {e}")

    def get_provider_info(self) -> Dict[str, Dict[str, Any]]:
        """Get information about all available providers."""
        info = {}
        for source, provider in self.providers.items():
            try:
                info[source.value] = provider.get_provider_info()
            except Exception as e:
                self._logger.debug(f"Failed to get info for provider {source.value}: {e}")
                info[source.value] = {'error': str(e)}

        return info

    def get_recovery_statistics(self, days: int = 30) -> Dict[str, Any]:
        """Get recovery statistics for the specified period."""
        try:
            # Get database statistics
            db_stats = self.cache_manager.get_recovery_statistics(days)

            # Combine with current session statistics
            stats = {
                'session_stats': self._stats.copy(),
                'database_stats': db_stats,
                'cache_size': self.cache_manager.get_cache_size(),
                'enabled_providers': list(self.providers.keys()),
                'period_days': days
            }

            return stats

        except Exception as e:
            self._logger.error(f"Failed to get recovery statistics: {e}")
            return {'error': str(e)}

    def is_enabled(self) -> bool:
        """Check if content recovery is enabled."""
        return len(self.providers) > 0

    def test_providers(self) -> Dict[str, bool]:
        """Test all providers with a known URL to check connectivity."""
        test_url = "https://httpbin.org/status/404"  # URL that should be in archives
        results = {}

        for source, provider in self.providers.items():
            try:
                # Quick test with short timeout
                old_timeout = getattr(provider, 'timeout', 10)
                provider.timeout = 5  # Quick test

                provider.attempt_recovery(test_url)
                results[source.value] = True  # Provider responded (success/failure doesn't matter)

                provider.timeout = old_timeout  # Restore original timeout

            except Exception as e:
                self._logger.debug(f"Provider {source.value} test failed: {e}")
                results[source.value] = False

        return results
