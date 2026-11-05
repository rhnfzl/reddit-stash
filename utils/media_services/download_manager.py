"""
Media Download Manager - Central coordinator for all media services.

This module provides a unified interface for downloading media from various sources
while applying appropriate rate limiting, error handling, and service-specific logic.
Integrates Reddit, Imgur, and generic HTTP downloaders with the existing codebase.
"""

import os
import logging
import threading
import tempfile
from collections.abc import Callable
from urllib.parse import urlparse

from utils.media_services.media_types import (
    DownloadResult, DownloadStatus, NEVER_RETRY_STATUSES, SERVICE_FAILURE_STATUSES, ServiceConfig,
)
from utils.media_services.reddit_media import RedditMediaDownloader
from utils.media_services.imgur_media import ImgurMediaDownloader
from utils.media_services.base_downloader import BaseHTTPDownloader, UNSHARED_SERVICE
from utils.media_services.error_isolation import CircuitBreaker, CircuitBreakerConfig
from utils.feature_flags import get_media_config
from utils.media_services.retry_queue import RetryItem, get_retry_queue
from utils.content_recovery.recovery_service import ContentRecoveryService
from utils.media_services.url_transformer import url_transformer
from utils.media_services.url_security import get_url_validator
from utils.constants import SAVED_MEDIA_EXTENSIONS, TRUSTED_MEDIA_DOMAINS
from utils.domain_matching import domain_matches

_TRANSIENT_FAILURE_THRESHOLD = 2

# Item problems, not service outages: never retried in this session, never count against a breaker.
# A 403 can lift by a later run (IP blocks), so unlike the others it is still queued.
_PERMANENT_STATUSES = NEVER_RETRY_STATUSES | {DownloadStatus.FORBIDDEN}


logger = logging.getLogger(__name__)


def _saved_size(path: str) -> int:
    """Bytes saved at path, which is a directory of images for an Imgur album; 0 if it is gone."""
    if os.path.isdir(path):
        return sum(entry.stat().st_size for entry in os.scandir(path) if entry.is_file())
    return os.path.getsize(path) if os.path.exists(path) else 0


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
        self._breakers: dict[str, CircuitBreaker] = {}
        self._media_config = get_media_config()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        self._url_lock = threading.Lock()

        # Permanent failures (_PERMANENT_STATUSES) — never retry in this session
        self._permanent_failures = set()
        # Transient failures (timeout, 5xx, rate limit) — retry after threshold
        self._transient_failures = {}  # {url: failure_count}

        # Session-level URL tracking to prevent duplicate downloads
        self._downloaded_urls = {}  # {url: local_path}
        # Content hashes point to the first local file for session-level media deduplication.
        self._downloaded_content_hashes = {}

        self._retry_queue = get_retry_queue()

        self._recovery_service = ContentRecoveryService()

        if self._media_config.is_media_enabled():
            self._initialize_services()

    def _record_failure(self, url: str, status: DownloadStatus) -> None:
        """Record a URL failure as permanent or transient (thread-safe, call with _url_lock held)."""
        if status in _PERMANENT_STATUSES:
            self._permanent_failures.add(url)
        else:
            count = self._transient_failures.get(url, 0) + 1
            self._transient_failures[url] = count

    def _should_skip_url(self, url: str) -> bool:
        """Check if URL should be skipped (thread-safe, call with _url_lock held)."""
        if url in self._permanent_failures:
            return True
        return self._transient_failures.get(url, 0) >= _TRANSIENT_FAILURE_THRESHOLD

    def _breaker(self, service_name: str) -> CircuitBreaker:
        """The circuit breaker of a service, so one failing service cannot stall the others."""
        return self._breakers.setdefault(service_name, CircuitBreaker(service_name, CircuitBreakerConfig()))

    def _deduplicate_download(self, result: DownloadResult) -> None:
        """Hard-link identical content to the first downloaded file when possible."""
        local_path = result.local_path
        content_hash = result.content_hash
        if not local_path or not content_hash or not os.path.isfile(local_path):
            return

        with self._url_lock:
            local_path_key = os.path.realpath(local_path)
            for tracked_hash, tracked_path in list(self._downloaded_content_hashes.items()):
                if tracked_hash != content_hash and os.path.realpath(tracked_path) == local_path_key:
                    del self._downloaded_content_hashes[tracked_hash]

            canonical_path = self._downloaded_content_hashes.get(content_hash)
            if canonical_path and canonical_path != local_path and os.path.isfile(canonical_path):
                self._hard_link_duplicate(canonical_path, local_path)
            else:
                self._downloaded_content_hashes[content_hash] = local_path

    def _hard_link_duplicate(self, canonical_path: str, local_path: str) -> None:
        """Replace local_path with a hard link to canonical_path, keeping the file if that fails."""
        replacement_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=os.path.dirname(local_path) or ".",
                delete=False,
            ) as replacement:
                replacement_path = replacement.name
            os.unlink(replacement_path)
            os.link(canonical_path, replacement_path)
            os.replace(replacement_path, local_path)
            self._logger.debug("Hard-linked duplicate media %s to %s", local_path, canonical_path)
        except OSError as error:
            self._logger.debug("Could not hard-link duplicate media %s: %s", local_path, error)
            try:
                if replacement_path and os.path.exists(replacement_path):
                    os.unlink(replacement_path)
            except OSError as cleanup_error:
                self._logger.warning(
                    "Could not remove temporary file %s: %s", replacement_path, cleanup_error
                )

    def _initialize_services(self):
        """Initialize all media download services."""
        try:
            # Generic first, so a failure in a named service below still leaves it usable
            generic_config = ServiceConfig(
                name="Generic",
                timeout_seconds=30,
                max_file_size=52428800,  # 50MB default
                user_agent="Reddit Stash Media Downloader/1.0"
            )
            self._generic_downloader = BaseHTTPDownloader(generic_config)

            self._reddit_downloader = RedditMediaDownloader()

            self._imgur_downloader = ImgurMediaDownloader()

            # Configure Imgur API credentials if available (supports comma-separated rotation)
            imgur_ids_raw = os.getenv('IMGUR_CLIENT_IDS') or os.getenv('IMGUR_CLIENT_ID') or ''
            imgur_client_ids = [cid.strip() for cid in imgur_ids_raw.split(',') if cid.strip()]
            if imgur_client_ids:
                self._logger.info(f"Configuring Imgur API authentication with {len(imgur_client_ids)} client ID(s)")
                self._imgur_downloader.set_client_credentials(imgur_client_ids)
            else:
                self._logger.warning("No IMGUR_CLIENT_IDS found - using IP-based limits (500 req/hour)")

            self._logger.info("Media download services initialized successfully")

        except Exception as e:
            self._logger.error(f"Failed to initialize media services: {e}")

    def download_media(self, url: str, save_path: str) -> DownloadResult:
        """
        Download media to save_path through the URL's service, queueing failures for a later run.

        Returns:
            The outcome. Its local_path is set only when a file was saved.
        """
        if not self._media_config.is_media_enabled():
            self._logger.info("Media downloads disabled by configuration")
            return DownloadResult(status=DownloadStatus.SKIPPED, error_message="Media downloads disabled")

        if not url or not save_path:
            return DownloadResult(status=DownloadStatus.INVALID_URL, error_message="Missing URL or save path")

        result, service_name = self._fetch(url, save_path)
        if result.is_success and result.local_path:
            return result

        if service_name and not self._retry_queue.add_failed_download(
            url,
            result.error_message or "Unknown error",
            service_name,
            metadata={"save_path": save_path},
        ):
            self._logger.error(f"Could not queue failed download for a later run: {url}")
        return result

    def _fetch(self, url: str, save_path: str) -> tuple[DownloadResult, str | None]:
        """
        Download a URL, falling back to content recovery. Never touches the retry queue.

        Returns:
            The result, and the service a failure should be queued under.
            That service is None when a retry could not help.
        """
        known = self._session_outcome(url)
        if known:
            return known, None

        try:
            download_url = self._resolve_download_url(url)
            if download_url is None:
                # Security failures never retry and never reach recovery.
                with self._url_lock:
                    self._record_failure(url, DownloadStatus.INVALID_URL)
                return DownloadResult(
                    status=DownloadStatus.INVALID_URL,
                    error_message="URL failed security validation",
                ), None

            # Determine appropriate service for URL (using transformed URL if available)
            service_name, downloader = self._get_service_for_url(download_url)

            if not downloader:
                self._logger.warning(f"No suitable downloader found for URL: {download_url}")
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="No suitable downloader",
                ), service_name

            # A rejected call says nothing about this URL: no recovery and no strike against it.
            # shortcut: a breaker opening right after this check still reaches recovery,
            # upgrade if call() ever reports rejections apart from raised errors.
            if self._breaker(service_name).is_rejecting():
                self._logger.debug(f"Not trying {url} while {service_name} is unavailable")
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="Service unavailable",
                ), service_name

            result = self._download_protected(service_name, downloader, download_url, save_path)

            if result.is_success and result.local_path:
                self._logger.debug(f"Successfully downloaded {url}")
                return self._track_success(result, url), service_name

            self._logger.warning(f"Failed to download {url}: {result.error_message}")

        except Exception as e:
            self._logger.error(f"Exception during media download from {url}: {e}")
            result = DownloadResult(status=DownloadStatus.FAILED, error_message=str(e))
            service_name = self._get_service_for_url(url)[0]

        # Attempt content recovery before blacklisting
        recovered = self._try_recovery(url, result.error_message, save_path)
        if recovered:
            return recovered, service_name

        with self._url_lock:
            self._record_failure(url, result.status)
        return result, service_name if result.should_retry else None

    def _session_outcome(self, url: str) -> DownloadResult | None:
        """The result this session already settled for a URL, or None if it needs fetching."""
        with self._url_lock:
            if self._should_skip_url(url):
                self._logger.debug(f"Skipping URL (failed earlier in session): {url}")
                return DownloadResult(
                    status=DownloadStatus.SKIPPED,
                    error_message="URL failed earlier in this session",
                )

            existing_path = self._downloaded_urls.get(url)
            if existing_path is None:
                return None
            size = _saved_size(existing_path)
            if size > 0:
                self._logger.debug(f"URL already downloaded in this session: {url} -> {existing_path}")
                return DownloadResult(status=DownloadStatus.SUCCESS, local_path=existing_path, bytes_downloaded=size)
            self._logger.debug(f"Cached file no longer valid, re-downloading: {url}")
            del self._downloaded_urls[url]
            return None

    def _resolve_download_url(self, url: str) -> str | None:
        """Return the URL to fetch after transforming and validating it, or None if rejected.

        Raises:
            OSError: The host did not resolve, which may pass; treated as a failed download.
        """
        transform_result = url_transformer.transform(url)
        download_url = transform_result.url

        if transform_result.transformed:
            self._logger.info(f"Transformed URL for direct access: {transform_result.platform}")
            self._logger.debug(f"Original: {url}")
            self._logger.debug(f"Transformed: {download_url}")
            if transform_result.notes:
                self._logger.debug(f"Note: {transform_result.notes}")

        # Skip full URL validation for trusted media CDN domains
        if urlparse(download_url).netloc.lower() in TRUSTED_MEDIA_DOMAINS:
            return download_url

        validation_result = get_url_validator().validate_url(download_url)

        if not validation_result.is_valid:
            if validation_result.dns_failed:
                raise OSError(f"Could not resolve host: {validation_result.issues}")

            self._logger.error(f"URL failed security validation: {download_url}")
            self._logger.error(f"Security issues: {validation_result.issues}")
            self._logger.error(f"Risk level: {validation_result.risk_level}")
            return None

        if validation_result.cleaned_url:
            self._logger.debug(f"Using cleaned URL: {validation_result.cleaned_url}")
            return validation_result.cleaned_url
        return download_url

    def _download_protected(self, service_name: str, downloader: BaseHTTPDownloader,
                            url: str, save_path: str) -> DownloadResult:
        """Download through the service's circuit breaker."""
        result = self._breaker(service_name).call(
            lambda: downloader.download(url, save_path),
            is_failure=(None if service_name == UNSHARED_SERVICE
                        else lambda result: result.status in SERVICE_FAILURE_STATUSES),
        )
        if result is None:  # the download raised or the breaker is open
            return DownloadResult(status=DownloadStatus.FAILED, error_message="Service unavailable")
        return result

    def _track_success(self, result: DownloadResult, *urls: str) -> DownloadResult:
        """Deduplicate a finished download and remember it for these URLs in this session."""
        self._deduplicate_download(result)
        with self._url_lock:
            for url in urls:
                self._downloaded_urls[url] = result.local_path
        return result

    def _try_recovery(self, url: str, error_message: str | None,
                      save_path: str) -> DownloadResult | None:
        """Download from a recovered copy of a failed URL. Returns the result on success, else None."""
        if not self._recovery_service.is_enabled():
            return None

        self._logger.debug(f"Attempting content recovery for failed URL: {url}")
        try:
            recovery_result = self._recovery_service.attempt_recovery(url, error_message)

            if not recovery_result.success:
                self._logger.debug(f"Content recovery failed: {recovery_result.error_message}")
                return None
            recovered_url = recovery_result.recovered_url
            if not recovered_url:
                self._logger.info("Recovery produced archived metadata, not downloadable media")
                return None

            self._logger.debug(f"Recovery successful! Using recovered URL: {recovered_url}")
            final_url = self._resolve_download_url(recovered_url)
            if final_url is None:
                return None

            # Re-resolve downloader for recovery URL domain
            # (e.g., web.archive.org needs generic downloader, not reddit_media)
            service_name, downloader = self._get_service_for_url(final_url)
            if not downloader:
                self._logger.warning(f"No suitable downloader for recovered URL: {final_url}")
                return None

            result = self._download_protected(service_name, downloader, final_url, save_path)
            if result.is_success and result.local_path:
                self._logger.debug(f"Successfully downloaded from recovered URL: {recovered_url}")
                return self._track_success(result, url, recovered_url)

            self._logger.warning(f"Failed to download from recovered URL: {result.error_message}")
        except Exception as e:
            self._logger.warning(f"Recovery attempt failed for {url}: {e}")
        return None

    def _get_service_for_url(self, url: str) -> tuple[str, BaseHTTPDownloader | None]:
        """Return the (service name, downloader) that handles a URL."""
        try:
            domain = urlparse(url).netloc.lower()
        except ValueError as e:
            self._logger.warning(f"Error parsing URL {url}: {e}")
            return 'generic', self._generic_downloader

        # Reddit media domains — separate circuit breakers per subdomain
        # so video timeouts don't cascade to block image downloads
        if domain_matches(domain, 'v.redd.it'):
            return 'reddit_video', self._reddit_downloader
        elif domain_matches(domain, 'i.redd.it'):
            return 'reddit_image', self._reddit_downloader
        elif domain_matches(domain, 'preview.redd.it') or domain_matches(domain, 'external-preview.redd.it'):
            return 'reddit_preview', self._reddit_downloader
        elif domain_matches(domain, 'imgur.com'):
            return 'imgur', self._imgur_downloader
        return 'generic', self._generic_downloader

    def process_pending_retries(
        self,
        limit: int = 50,
        on_recovered: Callable[[str, str], bool] | None = None,
    ) -> dict[str, int]:
        """
        Process pending retry downloads from previous failed attempts.

        This pass owns each row's in_progress and completed transitions.

        Args:
            limit: Maximum number of retries to process in this session
            on_recovered: Called with (url, local_path) for each download that now works;
                returns whether the saved markdown now points at the file

        Returns:
            Dictionary with statistics about retry processing
        """
        stats = {"processed": 0, "successful": 0, "failed": 0, "skipped": 0, "unlinked": 0}

        if not self._media_config.is_media_enabled():
            return stats

        try:
            pending_retries = self._retry_queue.get_pending_retries(limit=limit)

            if not pending_retries:
                self._logger.info("No pending retries found")
                return stats

            # Without downloaders rows would fail and use up their attempts on a local fault.
            if not (self._generic_downloader and self._reddit_downloader and self._imgur_downloader):
                self._logger.error("Media services failed to start; leaving queued retries for a later run")
                stats["skipped"] = len(pending_retries)
                return stats

            self._logger.info(f"Processing {len(pending_retries)} pending retry downloads")

            for retry_item in pending_retries:
                self._process_retry_item(retry_item, stats, on_recovered)

            self._logger.info(f"Retry processing complete: {stats}")

        except Exception as e:
            self._logger.error(f"Error processing pending retries: {e}")

        return stats

    def _process_retry_item(
        self,
        retry_item: RetryItem,
        stats: dict[str, int],
        on_recovered: Callable[[str, str], bool] | None,
    ) -> None:
        """Retry one queued download, recording the outcome on its row and in stats."""
        url = retry_item.url
        service_name = retry_item.service_name

        with self._url_lock:
            is_blacklisted = self._should_skip_url(url)
        if is_blacklisted:
            self._logger.debug(f"Skipping retry for blacklisted URL: {url}")
            stats["skipped"] += 1
            return

        # A rejected call says nothing about this URL, so it must not use up an attempt.
        if self._breaker(service_name).is_rejecting():
            self._logger.debug(f"Skipping retry while {service_name} is unavailable: {url}")
            stats["skipped"] += 1
            return

        if not self._retry_queue.mark_retry_started(url, service_name):
            self._logger.warning(f"Failed to mark retry as started for {url}")
            return

        save_path = retry_item.metadata.get("save_path")
        if not isinstance(save_path, str) or not save_path:
            # Legacy queue rows lack a safe destination. Keep them retryable rather
            # than guessing a path and writing into the wrong export.
            self._logger.error("Legacy retry item has no persisted save path: %s", url)
            stats["failed"] += 1
            self._retry_queue.record_retry_outcome(
                url,
                service_name,
                success=False,
                error_message="Legacy retry item has no persisted save path",
            )
            return

        try:
            result, _ = self._fetch(url, save_path)
            stats["processed"] += 1

            if result.is_success and result.local_path:
                self._logger.debug(f"Successfully retried download: {url}")
                stats["successful"] += 1
                if on_recovered and not on_recovered(url, result.local_path):
                    stats["unlinked"] += 1
                self._retry_queue.record_retry_outcome(url, service_name, success=True)
            else:
                self._logger.warning(f"Retry failed for {url}")
                stats["failed"] += 1
                self._retry_queue.record_retry_outcome(
                    url, service_name, success=False, error_message=result.error_message,
                )

        except Exception as e:
            self._logger.error(f"Exception during retry of {url}: {e}")
            stats["failed"] += 1
            self._retry_queue.record_retry_outcome(url, service_name, success=False, error_message=str(e))


_media_manager = None
_media_manager_lock = threading.Lock()

def get_media_manager() -> MediaDownloadManager:
    """Get the global media download manager instance."""
    global _media_manager
    if _media_manager is None:
        # Gallery worker threads can be the first callers; each must share one session cache.
        with _media_manager_lock:
            if _media_manager is None:
                _media_manager = MediaDownloadManager()
    return _media_manager


def download_media_into(url: str, save_directory: str, file_id: str) -> DownloadResult:
    """Download into save_directory as file_id plus an extension taken from the URL.

    The catch-all of the media path: a media fault ends as a failed result, so it never
    costs the text backup of the post it belongs to.
    """
    if not url or not save_directory or not file_id:
        return DownloadResult(status=DownloadStatus.INVALID_URL, error_message="Missing URL, directory or file id")

    try:
        os.makedirs(save_directory, exist_ok=True)

        parsed_url = urlparse(url)
        path = parsed_url.path.lower()

        if '.' in path:
            extension = os.path.splitext(path)[1]
            if extension not in SAVED_MEDIA_EXTENSIONS:
                extension = '.jpg'  # Default fallback
        else:
            # Domain-aware fallback
            if domain_matches(parsed_url.hostname, 'v.redd.it'):
                extension = '.mp4'
            else:
                extension = '.jpg'

        save_path = os.path.join(save_directory, f"{file_id}{extension}")
        return get_media_manager().download_media(url, save_path)

    except Exception as e:
        logger.error(f"Failed to download media from {url}: {e}")
        return DownloadResult(status=DownloadStatus.FAILED, error_message=str(e))
