"""
Main Content Recovery Service for Reddit Stash.

This module provides the central orchestration for content recovery attempts,
managing multiple providers and implementing the recovery cascade strategy.
"""

import json
import time
import logging
from dataclasses import replace
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

from utils.content_recovery.recovery_metadata import RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality, RecoveryAttempt, RecoveryCacheEntry
from utils.content_recovery.cache_manager import RecoveryCacheManager
from utils.content_recovery.recovery_config import RecoveryConfig, load_recovery_config
from utils.content_recovery.providers import WaybackMachineProvider, ArcticShiftProvider, PullPushProvider
from utils.rate_limiter import rate_limit_manager


NEGATIVE_CACHE_TTL_HOURS = 1
QUALITY_PRIORITIES = {
    RecoveryQuality.ORIGINAL: 5,
    RecoveryQuality.HIGH_QUALITY: 4,
    RecoveryQuality.MEDIUM_QUALITY: 3,
    RecoveryQuality.LOW_QUALITY: 2,
    RecoveryQuality.METADATA_ONLY: 0,
}
PROVIDER_PRIORITIES = {
    RecoverySource.WAYBACK_MACHINE: 5,
    RecoverySource.ARCTIC_SHIFT: 4,
    RecoverySource.PULLPUSH_IO: 3,
}


class ContentRecoveryService:
    """
    Central service for coordinating content recovery across multiple providers.

    Asks every enabled archive at once and returns the best answer.
    """

    def __init__(self, config: RecoveryConfig | None = None, cache_path: str | None = None):
        self.config = config or load_recovery_config()
        self.cache_manager = RecoveryCacheManager(cache_path)
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        self._init_providers()

    def _init_providers(self):
        """Initialize recovery providers based on configuration."""
        self.providers = {}

        timeout = self.config.timeout_seconds
        self._deadline_seconds = timeout

        if self.config.use_wayback_machine:
            self.providers[RecoverySource.WAYBACK_MACHINE] = WaybackMachineProvider(timeout)
            self._logger.debug("Initialized Wayback Machine provider")

        if self.config.use_arctic_shift:
            self.providers[RecoverySource.ARCTIC_SHIFT] = ArcticShiftProvider(timeout)
            self._logger.debug("Initialized Arctic Shift provider")

        if self.config.use_pullpush:
            self.providers[RecoverySource.PULLPUSH_IO] = PullPushProvider(timeout)
            self._logger.debug("Initialized PullPush.io provider")

        if not self.providers:
            self._logger.warning("No recovery providers enabled")

    def attempt_recovery(self, url: str, original_failure_reason: str | None = None) -> RecoveryResult:
        """
        Attempt to recover content from the given URL using available providers.

        Args:
            url: Original URL that failed to download
            original_failure_reason: Reason why the original download failed

        Returns:
            RecoveryResult with the best recovery found, or failure if none found
        """
        start_time = time.time()

        try:
            self._logger.debug(f"Starting content recovery for: {url}")

            cached_result = self._lookup_cached_result(url)
            if cached_result:
                self._logger.debug(f"Cache hit for recovery of: {url}")
                return cached_result

            self.cache_manager.cleanup_if_due()

            recovery_result = self._run_providers(url, original_failure_reason)

            # Cache the result
            if recovery_result.success:
                self._cache_successful_result(url, recovery_result)
            else:
                self._cache_negative_results(url, recovery_result.negative_cache_sources)

            duration = time.time() - start_time
            self._logger.info(
                f"Recovery attempt completed in {duration:.2f}s: "
                f"{'SUCCESS' if recovery_result.success else 'FAILED'}"
            )

            return recovery_result

        except Exception as e:
            duration = time.time() - start_time
            self._logger.error(f"Unexpected error in content recovery: {e}")

            return RecoveryResult.failure_result(
                f"Recovery system error: {str(e)} (duration: {duration:.2f}s)"
            )

    def _lookup_cached_result(self, url: str) -> RecoveryResult | None:
        """Check if we have a cached recovery result for this URL."""
        try:
            successful_results = []
            negative_sources = set()
            for source in self.providers:
                cached_entry = self.cache_manager.get_cached_result(url, source)
                if not cached_entry:
                    continue
                if cached_entry.success:
                    successful_results.append(self._cached_success(cached_entry))
                else:
                    negative_sources.add(source)

            if successful_results:
                return self._best_result(successful_results)

            if set(self.providers).issubset(negative_sources):
                return RecoveryResult.failure_result("Cached negative result")

        except Exception as e:
            self._logger.debug(f"Cache check failed: {e}")

        return None

    def _cached_success(self, entry: RecoveryCacheEntry) -> RecoveryResult:
        """Rebuild the result a provider returned when this entry was cached."""
        additional_metadata = None
        if entry.metadata_json:
            try:
                additional_metadata = json.loads(entry.metadata_json)
            except (TypeError, json.JSONDecodeError):
                self._logger.debug("Cached recovery metadata could not be decoded")

        return RecoveryResult.success_result(entry.recovered_url, RecoveryMetadata(
            source=entry.recovery_source,
            recovered_url=entry.recovered_url,
            recovery_timestamp=entry.cached_at,
            content_quality=entry.content_quality,
            cache_hit=True,
            additional_metadata=additional_metadata,
        ))

    @staticmethod
    def _deferred_by_rate_limiter(source: RecoverySource) -> bool:
        """Skip a provider rather than park a worker in acquire() until its timeout."""
        if source == RecoverySource.PULLPUSH_IO:
            return not rate_limit_manager.can_proceed(source.value)
        # Wayback only skips during a 429 backoff; a short token wait still runs as before.
        return source == RecoverySource.WAYBACK_MACHINE and rate_limit_manager.in_backoff(source.value)

    @staticmethod
    def _best_result(results: Iterable[RecoveryResult]) -> RecoveryResult:
        """Pick the highest-quality result, breaking ties by provider reliability."""
        return max(
            results,
            key=lambda result: (
                QUALITY_PRIORITIES.get(
                    result.metadata.content_quality if result.metadata else RecoveryQuality.METADATA_ONLY,
                    0,
                ),
                PROVIDER_PRIORITIES.get(result.source, 0),
            ),
        )

    def _skip_deferred(self, url: str, source: RecoverySource, failure_reason: str | None) -> bool:
        """Record and skip a provider that the rate limiter is holding back."""
        if not self._deferred_by_rate_limiter(source):
            return False
        deferred_result = RecoveryResult.failure_result(
            f"{source.value} recovery deferred by rate limiter",
            source,
        )
        self._record_attempt(url, source, deferred_result, failure_reason)
        return True

    def _note_result(self, url: str, source: RecoverySource, result: RecoveryResult,
                     failure_reason: str | None, negative_cache_sources: set[RecoverySource]) -> None:
        """Record a provider's answer and note whether it confirmed an archive miss."""
        self._record_attempt(url, source, result, failure_reason)
        if result.success:
            self._logger.debug(f"Recovery successful via {source.value}: {result.recovered_url}")
            return
        self._logger.debug(f"Recovery failed via {source.value}: {result.error_message}")
        if result.confirmed_miss:
            negative_cache_sources.add(source)

    def _run_providers(self, url: str, failure_reason: str | None) -> RecoveryResult:
        """Ask every enabled provider at once and keep the highest-quality answer."""

        executor = ThreadPoolExecutor(max_workers=len(self.providers))
        completed_sources = set()
        negative_cache_sources = set()
        successful_results = []
        try:
            future_to_source = {}
            for source, provider in self.providers.items():
                if not self._skip_deferred(url, source, failure_reason):
                    future_to_source[executor.submit(provider.attempt_recovery, url)] = source

            try:
                # Process every completed result before choosing the best quality.
                for future in as_completed(future_to_source, timeout=self._deadline_seconds):
                    source = future_to_source[future]
                    completed_sources.add(source)
                    try:
                        result = future.result()
                    except Exception as e:
                        self._logger.error(f"Provider {source.value} threw exception: {e}")
                        result = RecoveryResult.failure_result(f"{source.value} raised: {e}", source)

                    self._note_result(url, source, result, failure_reason, negative_cache_sources)
                    if result.success:
                        successful_results.append(result)
            except FuturesTimeoutError:
                self._logger.warning("Recovery timed out before all providers returned")
        finally:
            # Provider requests have their own I/O timeouts. Do not make the caller wait
            # for an already-running provider after the overall recovery deadline expires.
            executor.shutdown(wait=False, cancel_futures=True)

        if successful_results:
            best_result = self._best_result(successful_results)
            return replace(
                best_result,
                attempted_sources=best_result.attempted_sources | frozenset(completed_sources),
            )

        return RecoveryResult.failure_result(
            "All recovery providers failed",
            attempted_sources=frozenset(completed_sources),
            negative_cache_sources=frozenset(negative_cache_sources),
        )

    def _record_attempt(self, url: str, source: RecoverySource, result: RecoveryResult,
                       failure_reason: str | None):
        """Record recovery attempt for analytics."""
        try:
            attempt = RecoveryAttempt(
                original_url=url,
                recovery_source=source,
                success=result.success,
                recovered_url=result.recovered_url,
                error_message=result.error_message or failure_reason,
                duration_seconds=result.metadata.attempt_duration if result.metadata else None
            )

            self.cache_manager.record_attempt(attempt)

        except Exception as e:
            self._logger.debug(f"Failed to record recovery attempt: {e}")

    def _cache_successful_result(self, url: str, result: RecoveryResult):
        """Cache a successful recovery result."""
        try:
            if result.metadata:
                self.cache_manager.cache_result(
                    url=url,
                    source=result.metadata.source,
                    recovered_url=result.recovered_url,
                    quality=result.metadata.content_quality,
                    ttl_hours=self.config.cache_duration_hours,
                    metadata=result.metadata.additional_metadata
                )

        except Exception as e:
            self._logger.debug(f"Failed to cache recovery result: {e}")

    def _cache_negative_results(
        self,
        url: str,
        negative_sources: frozenset[RecoverySource],
    ):
        """Cache only provider outcomes that confirmed an archive miss."""
        try:
            for source in negative_sources:
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

    def is_enabled(self) -> bool:
        """Check if content recovery is enabled."""
        return len(self.providers) > 0
