"""
Media Download Manager - Central coordinator for all media services.

This module provides a unified interface for downloading media from various sources
while applying appropriate rate limiting, error handling, and service-specific logic.
Integrates Reddit, Imgur, and generic HTTP downloaders with the existing codebase.
"""

import os
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from .service_abstractions import DownloadResult, DownloadStatus, MediaType
from .media_services.reddit_media import RedditMediaDownloader
from .media_services.imgur_media import ImgurMediaDownloader
from .media_services.base_downloader import BaseHTTPDownloader
from .rate_limiter import rate_limit_manager
from .error_isolation import get_service_manager
from .feature_flags import get_media_config
from .retry_queue import get_retry_queue


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

        # Session-level blacklist to prevent retry loops
        self._failed_urls = set()

        # Persistent retry queue for cross-run recovery
        self._retry_queue = get_retry_queue()

        # Initialize services if media downloads are enabled
        if self._media_config.is_images_enabled():
            self._initialize_services()

    def _initialize_services(self):
        """Initialize all media download services."""
        try:
            # Initialize Reddit media downloader
            self._reddit_downloader = RedditMediaDownloader()
            rate_limit_manager.register_service_from_config(
                'reddit_media',
                self._reddit_downloader.config
            )

            # Initialize Imgur downloader
            self._imgur_downloader = ImgurMediaDownloader()
            rate_limit_manager.register_service_from_config(
                'imgur',
                self._imgur_downloader.config
            )

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

        # Check session-level blacklist to prevent retry loops
        if url in self._failed_urls:
            self._logger.debug(f"Skipping blacklisted URL (failed earlier in session): {url}")
            return None

        try:
            # Determine appropriate service for URL
            service_name, downloader = self._get_service_for_url(url)

            if not downloader:
                self._logger.warning(f"No suitable downloader found for URL: {url}")
                return None

            # Execute download with error isolation
            result = self._service_manager.execute_with_protection(
                service_name,
                lambda: downloader.download(url, save_path),
                fallback_value=DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="Service unavailable"
                )
            )

            if result and result.is_success and result.local_path:
                self._logger.debug(f"Successfully downloaded {url} to {result.local_path}")
                # Mark as successful in retry queue if it was a retry
                self._retry_queue.mark_retry_completed(url, success=True)
                return result.local_path
            else:
                error_msg = result.error_message if result else "Unknown error"
                self._logger.warning(f"Failed to download {url}: {error_msg}")
                # Add to session blacklist to prevent retry loops
                self._failed_urls.add(url)
                # Add to persistent retry queue for cross-run recovery
                self._retry_queue.add_failed_download(url, error_msg, service_name)
                return None

        except Exception as e:
            self._logger.error(f"Exception during media download from {url}: {e}")
            # Add to session blacklist to prevent retry loops
            self._failed_urls.add(url)
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
            parsed = urlparse(url.lower())
            domain = parsed.netloc.lower()

            # Reddit media domains
            if any(domain.endswith(reddit_domain) for reddit_domain in [
                'i.redd.it', 'v.redd.it', 'preview.redd.it', 'external-preview.redd.it'
            ]):
                return 'reddit_media', self._reddit_downloader

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
                if url in self._failed_urls:
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
            extension = '.jpg'  # Default fallback

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