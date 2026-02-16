"""
Media Download Manager - Central coordinator for all media services.

This module provides a unified interface for downloading media from various sources
while applying appropriate rate limiting, error handling, and service-specific logic.
Integrates Reddit, Imgur, and generic HTTP downloaders with the existing codebase.
"""

import os
import logging
import threading
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from .service_abstractions import DownloadResult, DownloadStatus
from .media_services.reddit_media import RedditMediaDownloader
from .media_services.imgur_media import ImgurMediaDownloader
from .media_services.base_downloader import BaseHTTPDownloader
from .rate_limiter import rate_limit_manager
from .error_isolation import get_service_manager
from .feature_flags import get_media_config
from .retry_queue import get_retry_queue
from .content_recovery.recovery_service import ContentRecoveryService
from .url_transformer import url_transformer
from .url_security import get_url_validator
from .constants import TRUSTED_MEDIA_DOMAINS


class MediaDownloadManager:
    """
    Central manager for all media download operations.

    Provides unified interface for downloading media while applying appropriate
    service-specific handling, rate limiting, and error isolation.
    """

    def __init__(self):
        self._reddit_downloader = None
        self._imgur_downloader = None
        self._generic_downloader = None
        self._service_manager = get_service_manager()
        self._media_config = get_media_config()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Lock for thread-safe access to URL tracking sets
        self._url_lock = threading.Lock()

        # Permanent failures (403, 404, security) — never retry in this session
        self._permanent_failures = set()
        # Transient failures (timeout, 5xx, rate limit) — retry after threshold
        self._transient_failures = {}  # {url: failure_count}
        _TRANSIENT_FAILURE_THRESHOLD = 2

        # Session-level URL tracking to prevent duplicate downloads
        self._downloaded_urls = {}  # {url: local_path}

        # Persistent retry queue for cross-run recovery
        self._retry_queue = get_retry_queue()

        # Content recovery service for failed downloads
        self._recovery_service = ContentRecoveryService(config=self._media_config)

        # Initialize services if media downloads are enabled
        if self._media_config.is_images_enabled():
            self._initialize_services()

    def _is_permanent_failure(self, error_message: str) -> bool:
        """Classify whether an error is permanent (don't retry) or transient (may retry)."""
        if not error_message:
            return False
        err_lower = error_message.lower()
        permanent_patterns = ['404', '403', 'not found', 'forbidden', 'security validation',
                              'invalid url', 'cannot handle']
        return any(p in err_lower for p in permanent_patterns)

    def _record_failure(self, url: str, error_message: str) -> None:
        """Record a URL failure as permanent or transient (thread-safe, call with _url_lock held)."""
        if self._is_permanent_failure(error_message):
            self._permanent_failures.add(url)
        else:
            count = self._transient_failures.get(url, 0) + 1
            self._transient_failures[url] = count

    def _should_skip_url(self, url: str) -> bool:
        """Check if URL should be skipped (thread-safe, call with _url_lock held)."""
        if url in self._permanent_failures:
            return True
        return self._transient_failures.get(url, 0) >= 2

    def _initialize_services(self):
        """Initialize all media download services."""
        try:
            # Initialize Reddit media downloader with per-subdomain circuit breakers
            # so video timeouts don't cascade to block image downloads
            self._reddit_downloader = RedditMediaDownloader()
            from .error_isolation import CircuitBreakerConfig
            for reddit_service in ('reddit_video', 'reddit_image', 'reddit_preview'):
                rate_limit_manager.register_service_from_config(
                    reddit_service,
                    self._reddit_downloader.config
                )
                self._service_manager.register_service(reddit_service, CircuitBreakerConfig(
                    failure_threshold=5,
                    recovery_timeout=60.0,
                    success_threshold=3,
                    timeout=45.0
                ))

            # Initialize Imgur downloader
            self._imgur_downloader = ImgurMediaDownloader()

            # Configure Imgur API credentials if available (supports comma-separated rotation)
            imgur_ids_raw = os.getenv('IMGUR_CLIENT_IDS') or os.getenv('IMGUR_CLIENT_ID') or ''
            imgur_client_ids = [cid.strip() for cid in imgur_ids_raw.split(',') if cid.strip()]
            if imgur_client_ids:
                self._logger.info(f"Configuring Imgur API authentication with {len(imgur_client_ids)} client ID(s)")
                self._imgur_downloader.set_client_credentials(imgur_client_ids)
            else:
                self._logger.warning("No IMGUR_CLIENT_IDS found - using IP-based limits (500 req/hour)")

            rate_limit_manager.register_service_from_config(
                'imgur',
                self._imgur_downloader.config
            )

            # Configure circuit breaker for Imgur with longer timeout to handle rate limiting delays
            from .error_isolation import CircuitBreakerConfig
            imgur_circuit_config = CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout=60.0,
                success_threshold=3,
                timeout=90.0  # Increased from 30s to 90s for Imgur rate limiting
            )
            self._service_manager.register_service('imgur', imgur_circuit_config)

            # Initialize generic HTTP downloader
            from .service_abstractions import ServiceConfig
            generic_config = ServiceConfig(
                name="Generic",
                rate_limit_per_minute=30,
                timeout_seconds=30,
                max_file_size=52428800,  # 50MB default
                user_agent="Reddit Stash Media Downloader/1.0"
            )
            self._generic_downloader = BaseHTTPDownloader(generic_config)

            self._logger.info("Media download services initialized successfully")

        except Exception as e:
            self._logger.error(f"Failed to initialize media services: {e}")
            # Create fallback generic downloader
            self._generic_downloader = BaseHTTPDownloader()

    def download_media(self, url: str, save_path: str) -> Optional[str]:
        """
        Download media from URL using appropriate service.

        Args:
            url: URL of the media to download
            save_path: Local path where media should be saved

        Returns:
            Local file path if successful, None if failed
        """
        if not self._media_config.is_images_enabled():
            self._logger.info("Media downloads disabled by configuration")
            return None

        if not url or not save_path:
            return None

        # Check session-level blacklist and dedup cache (thread-safe)
        with self._url_lock:
            if self._should_skip_url(url):
                self._logger.debug(f"Skipping URL (failed earlier in session): {url}")
                return None

            if url in self._downloaded_urls:
                existing_path = self._downloaded_urls[url]
                if os.path.exists(existing_path) and os.path.getsize(existing_path) > 0:
                    self._logger.debug(f"URL already downloaded in this session: {url} -> {existing_path}")
                    return existing_path
                else:
                    self._logger.debug(f"Cached file no longer valid, re-downloading: {url}")
                    del self._downloaded_urls[url]

        try:
            # Apply URL transformation to convert viewer URLs to direct download URLs
            transform_result = url_transformer.transform(url)
            download_url = transform_result.url

            if transform_result.transformed:
                self._logger.info(f"Transformed URL for direct access: {transform_result.platform}")
                self._logger.debug(f"Original: {url}")
                self._logger.debug(f"Transformed: {download_url}")
                if transform_result.notes:
                    self._logger.debug(f"Note: {transform_result.notes}")
            else:
                download_url = url

            # Skip full URL validation for trusted media CDN domains
            parsed_check = urlparse(download_url)
            is_trusted = parsed_check.netloc.lower() in TRUSTED_MEDIA_DOMAINS

            if not is_trusted:
                # Validate URL security before downloading
                url_validator = get_url_validator()
                validation_result = url_validator.validate_url(download_url)

                if not validation_result.is_valid:
                    self._logger.error(f"URL failed security validation: {download_url}")
                    self._logger.error(f"Security issues: {validation_result.issues}")
                    self._logger.error(f"Risk level: {validation_result.risk_level}")

                    # Add to permanent failures (security failures never retry)
                    with self._url_lock:
                        self._record_failure(url, "security validation failed")

                    # Add to retry queue with security failure status
                    self._retry_queue.add_failed_url(
                        url=url,
                        error_message=f"Security validation failed: {validation_result.issues}",
                        content_type="media",
                        max_retries=0  # Don't retry security failures
                    )

                    return None

                # Use cleaned URL if available
                if validation_result.cleaned_url:
                    download_url = validation_result.cleaned_url
                    self._logger.debug(f"Using cleaned URL: {download_url}")

                # Log security warnings for medium-risk URLs
                if validation_result.risk_level == "medium":
                    self._logger.warning(f"Medium-risk URL detected: {download_url}")
                    self._logger.warning(f"Issues: {validation_result.issues}")
                    # Continue with download but log the warning

            # Determine appropriate service for URL (using transformed URL if available)
            service_name, downloader = self._get_service_for_url(download_url)

            if not downloader:
                self._logger.warning(f"No suitable downloader found for URL: {download_url}")
                return None

            # Execute download with error isolation (using transformed URL)
            result = self._service_manager.execute_with_protection(
                service_name,
                lambda: downloader.download(download_url, save_path),
                fallback_value=DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="Service unavailable"
                )
            )

            if result and result.is_success and result.local_path:
                self._logger.debug(f"Successfully downloaded {url} to {result.local_path}")
                # Track this URL as successfully downloaded in this session
                with self._url_lock:
                    self._downloaded_urls[url] = result.local_path
                # Mark as successful in retry queue if it was a retry
                self._retry_queue.mark_retry_completed(url, success=True)
                return result.local_path
            else:
                error_msg = result.error_message if result else "Unknown error"
                self._logger.warning(f"Failed to download {url}: {error_msg}")

                # Attempt content recovery before blacklisting
                if self._recovery_service.is_enabled():
                    self._logger.info(f"Attempting content recovery for failed URL: {url}")
                    recovery_result = self._recovery_service.attempt_recovery(url, error_msg)

                    if recovery_result.success and recovery_result.recovered_url:
                        self._logger.info(f"Recovery successful! Using recovered URL: {recovery_result.recovered_url}")
                        # Try downloading from the recovered URL (apply transformation if needed)
                        try:
                            recovery_transform = url_transformer.transform(recovery_result.recovered_url)
                            final_recovery_url = recovery_transform.url
                            if recovery_transform.transformed:
                                self._logger.debug(f"Also transformed recovery URL: {recovery_result.recovered_url} -> {final_recovery_url}")

                            # Validate recovered URL security before downloading
                            recovery_validation = url_validator.validate_url(final_recovery_url)
                            if not recovery_validation.is_valid:
                                self._logger.warning(f"Recovered URL failed security validation: {final_recovery_url} — {recovery_validation.issues}")
                                recovery_download_result = None
                            else:
                                if recovery_validation.cleaned_url:
                                    final_recovery_url = recovery_validation.cleaned_url

                                # Re-resolve downloader for recovery URL domain
                                # (e.g., web.archive.org needs generic downloader, not reddit_media)
                                recovery_service_name, recovery_downloader = self._get_service_for_url(final_recovery_url)
                                if not recovery_downloader:
                                    self._logger.warning(f"No suitable downloader for recovered URL: {final_recovery_url}")
                                    recovery_download_result = None
                                else:
                                    recovery_download_result = recovery_downloader.download(final_recovery_url, save_path)
                            if recovery_download_result and recovery_download_result.is_success and recovery_download_result.local_path:
                                self._logger.info(f"Successfully downloaded from recovered URL: {recovery_result.recovered_url}")
                                # Track both original and recovered URLs as successfully downloaded
                                with self._url_lock:
                                    self._downloaded_urls[url] = recovery_download_result.local_path
                                    self._downloaded_urls[recovery_result.recovered_url] = recovery_download_result.local_path
                                # Mark original URL as successful in retry queue (recovery counts as success)
                                self._retry_queue.mark_retry_completed(url, success=True)
                                return recovery_download_result.local_path
                            else:
                                recovery_error = recovery_download_result.error_message if recovery_download_result else "Unknown recovery download error"
                                self._logger.warning(f"Failed to download from recovered URL: {recovery_error}")
                        except Exception as e:
                            self._logger.warning(f"Exception downloading from recovered URL: {e}")
                    else:
                        self._logger.debug(f"Content recovery failed: {recovery_result.error_message}")

                # Classify and record failure (permanent vs transient)
                with self._url_lock:
                    self._record_failure(url, error_msg)
                # Add to persistent retry queue for cross-run recovery
                self._retry_queue.add_failed_download(url, error_msg, service_name)
                return None

        except Exception as e:
            self._logger.error(f"Exception during media download from {url}: {e}")

            # Attempt content recovery before blacklisting
            if self._recovery_service.is_enabled():
                self._logger.info(f"Attempting content recovery after exception for URL: {url}")
                try:
                    recovery_result = self._recovery_service.attempt_recovery(url, str(e))

                    if recovery_result.success and recovery_result.recovered_url:
                        self._logger.info(f"Recovery successful after exception! Using recovered URL: {recovery_result.recovered_url}")
                        # Try downloading from the recovered URL (apply transformation if needed)
                        recovery_transform = url_transformer.transform(recovery_result.recovered_url)
                        final_recovery_url = recovery_transform.url
                        if recovery_transform.transformed:
                            self._logger.debug(f"Also transformed recovery URL: {recovery_result.recovered_url} -> {final_recovery_url}")

                        # Validate recovered URL security before downloading
                        url_validator = get_url_validator()
                        recovery_validation = url_validator.validate_url(final_recovery_url)
                        if not recovery_validation.is_valid:
                            self._logger.warning(f"Recovered URL failed security validation: {final_recovery_url} — {recovery_validation.issues}")
                        else:
                            if recovery_validation.cleaned_url:
                                final_recovery_url = recovery_validation.cleaned_url
                            downloader = self._get_service_for_url(final_recovery_url)[1]
                            if downloader:
                                recovery_download_result = downloader.download(final_recovery_url, save_path)
                                if recovery_download_result and recovery_download_result.is_success and recovery_download_result.local_path:
                                    self._logger.info(f"Successfully downloaded from recovered URL after exception: {recovery_result.recovered_url}")
                                    # Track both original and recovered URLs as successfully downloaded
                                    with self._url_lock:
                                        self._downloaded_urls[url] = recovery_download_result.local_path
                                        self._downloaded_urls[recovery_result.recovered_url] = recovery_download_result.local_path
                                    # Mark original URL as successful in retry queue (recovery counts as success)
                                    self._retry_queue.mark_retry_completed(url, success=True)
                                    return recovery_download_result.local_path
                except Exception as recovery_e:
                    self._logger.warning(f"Recovery attempt also failed: {recovery_e}")

            # Classify and record failure (permanent vs transient)
            with self._url_lock:
                self._record_failure(url, str(e))
            # Add to persistent retry queue for cross-run recovery
            service_name, _ = self._get_service_for_url(url)
            self._retry_queue.add_failed_download(url, str(e), service_name or "unknown")
            return None

    def _get_service_for_url(self, url: str) -> tuple[str, Optional[Any]]:
        """
        Determine the appropriate service for a given URL.

        Args:
            url: URL to analyze

        Returns:
            Tuple of (service_name, downloader_instance)
        """
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            # Reddit media domains — separate circuit breakers per subdomain
            # so video timeouts don't cascade to block image downloads
            if domain.endswith('v.redd.it'):
                return 'reddit_video', self._reddit_downloader
            elif domain.endswith('i.redd.it'):
                return 'reddit_image', self._reddit_downloader
            elif domain.endswith('preview.redd.it') or domain.endswith('external-preview.redd.it'):
                return 'reddit_preview', self._reddit_downloader

            # Imgur domains
            elif any(domain.endswith(imgur_domain) for imgur_domain in [
                'i.imgur.com', 'imgur.com', 'm.imgur.com'
            ]):
                return 'imgur', self._imgur_downloader

            # Generic HTTP for other domains
            else:
                return 'generic', self._generic_downloader

        except Exception as e:
            self._logger.warning(f"Error parsing URL {url}: {e}")
            return 'generic', self._generic_downloader

    def get_service_health(self) -> Dict[str, Any]:
        """Get health status of all media services."""
        return self._service_manager.get_service_health()

    def is_service_available(self, service_name: str) -> bool:
        """Check if a specific service is available."""
        return self._service_manager.is_service_available(service_name)

    def reset_service(self, service_name: str):
        """Reset a service's error state."""
        self._service_manager.reset_service(service_name)
        rate_limit_manager.reset_service(service_name)

    def process_pending_retries(self, max_retries: int = 50) -> Dict[str, int]:
        """
        Process pending retry downloads from previous failed attempts.

        Args:
            max_retries: Maximum number of retries to process in this session

        Returns:
            Dictionary with statistics about retry processing
        """
        stats = {"processed": 0, "successful": 0, "failed": 0, "skipped": 0}

        try:
            # Get pending retries from the retry queue
            pending_retries = self._retry_queue.get_pending_retries(limit=max_retries)

            if not pending_retries:
                self._logger.info("No pending retries found")
                return stats

            self._logger.info(f"Processing {len(pending_retries)} pending retry downloads")

            for retry_item in pending_retries:
                url = retry_item['url']
                service_name = retry_item['service_name']

                # Skip if URL is in session blacklist
                with self._url_lock:
                    is_blacklisted = self._should_skip_url(url)
                if is_blacklisted:
                    self._logger.debug(f"Skipping retry for blacklisted URL: {url}")
                    stats["skipped"] += 1
                    continue

                # Mark retry as started
                if not self._retry_queue.mark_retry_started(url, service_name):
                    self._logger.warning(f"Failed to mark retry as started for {url}")
                    continue

                # Attempt the download
                # Use a temporary save path for retry attempts
                temp_save_path = f"/tmp/retry_{retry_item['id']}"

                try:
                    result_path = self.download_media(url, temp_save_path)
                    stats["processed"] += 1

                    if result_path:
                        self._logger.info(f"Successfully retried download: {url}")
                        stats["successful"] += 1
                        # Mark as completed (success=True) - this is handled in download_media
                    else:
                        self._logger.warning(f"Retry failed for {url}")
                        stats["failed"] += 1
                        # Mark as failed (success=False) - this is handled in download_media

                except Exception as e:
                    self._logger.error(f"Exception during retry of {url}: {e}")
                    stats["failed"] += 1
                    self._retry_queue.mark_retry_completed(url, success=False, error_message=str(e))

            self._logger.info(f"Retry processing complete: {stats}")

        except Exception as e:
            self._logger.error(f"Error processing pending retries: {e}")

        return stats


# Global manager instance
_media_manager = None

def get_media_manager() -> MediaDownloadManager:
    """Get the global media download manager instance."""
    global _media_manager
    if _media_manager is None:
        _media_manager = MediaDownloadManager()
    return _media_manager


def download_media_file(url: str, save_directory: str, file_id: str) -> Optional[str]:
    """
    Convenience function for downloading media files.

    Compatible with the existing download_image function signature.

    Args:
        url: URL of the media to download
        save_directory: Directory to save the file
        file_id: Unique identifier for the file

    Returns:
        Local file path if successful, None if failed
    """
    if not url or not save_directory or not file_id:
        return None

    try:
        # Ensure save directory exists
        os.makedirs(save_directory, exist_ok=True)

        # Determine file extension from URL
        parsed_url = urlparse(url)
        path = parsed_url.path.lower()

        # Extract extension from URL
        if '.' in path:
            extension = os.path.splitext(path)[1]
            # Validate common image/video extensions
            if extension not in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm']:
                extension = '.jpg'  # Default fallback
        else:
            # Domain-aware fallback
            domain = parsed_url.netloc.lower()
            if 'v.redd.it' in domain:
                extension = '.mp4'
            else:
                extension = '.jpg'

        # Create save path
        filename = f"{file_id}{extension}"
        save_path = os.path.join(save_directory, filename)

        # Download using manager
        manager = get_media_manager()
        result_path = manager.download_media(url, save_path)

        return result_path

    except Exception as e:
        logging.error(f"Error in download_media_file for {url}: {e}")
        return None