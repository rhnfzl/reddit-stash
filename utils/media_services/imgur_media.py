"""
Imgur media downloader service.

This module provides comprehensive support for downloading Imgur-hosted media
including single images, albums, and galleries through Imgur's direct API.
Implements web-researched best practices for 2024.
"""

import os
import re
import logging
import threading
from typing import Optional, List
from urllib.parse import urlparse

from ..service_abstractions import (
    DownloadResult, DownloadStatus,
    MediaMetadata, MediaType, ServiceConfig
)
from ..domain_matching import domain_matches
from .base_downloader import BaseHTTPDownloader


class ImgurMediaDownloader(BaseHTTPDownloader):
    """
    Imgur media downloader supporting images, albums, and galleries.

    This service handles:
    - Single images (i.imgur.com/ID.ext or imgur.com/ID)
    - Albums (imgur.com/a/ID)
    - Galleries (imgur.com/gallery/ID)
    - Direct links (i.imgur.com/ID.ext)

    Respects Imgur's strict rate limits (~8 requests/minute).
    """

    def __init__(self, config: Optional[ServiceConfig] = None):
        if config is None:
            config = ServiceConfig(
                name="Imgur",
                rate_limit_per_minute=4,  # Conservative rate for IP limits (240/hour leaves 260 buffer)
                timeout_seconds=90,  # Increased to handle rate limiting delays
                max_file_size=209715200,  # 200MB default
                user_agent="Reddit Stash Media Downloader/1.0",
                # Security enhancements
                max_redirects=3,  # Imgur rarely redirects more than once
                connect_timeout=5.0,
                read_timeout=90.0,  # Imgur can be slow due to rate limiting
                allowed_content_types=['image/*', 'video/*'],  # Only allow media content
                verify_ssl=True
            )
        super().__init__(config)

        # Setup logging
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        self._client_ids = list(
            self.config.api_keys.get('client_ids', []) if self.config.api_keys else []
        )
        self._current_client_index = 0
        self._client_lock = threading.Lock()

    def can_handle(self, url: str) -> bool:
        """Check if this service can handle the given URL."""
        try:
            return self._is_imgur_url(url)
        except Exception:
            return False

    def _is_imgur_url(self, url: str) -> bool:
        """Check if URL is an Imgur URL."""
        parsed = urlparse(url.lower())
        domain = parsed.netloc

        # Handle various Imgur domains
        imgur_domains = [
            'imgur.com',
            'www.imgur.com',
            'i.imgur.com',
            'm.imgur.com'
        ]

        return any(domain_matches(domain, imgur_domain) for imgur_domain in imgur_domains)

    def get_metadata(self, url: str) -> Optional[MediaMetadata]:
        """Get metadata for Imgur media without downloading."""
        if not self.can_handle(url):
            return None

        try:
            imgur_id, media_type = self._extract_imgur_info(url)
            if not imgur_id:
                return None

            return self._get_metadata_direct_api(imgur_id, media_type, url)

        except Exception as e:
            self._logger.debug(f"Failed to get Imgur metadata for {url}: {e}")
            return MediaMetadata(
                url=url,
                media_type=MediaType.UNKNOWN,
                file_size=None
            )

    def download(self, url: str, save_path: str) -> DownloadResult:
        """Download Imgur media with appropriate handling for different types."""
        if not self.can_handle(url):
            return DownloadResult(
                status=DownloadStatus.INVALID_URL,
                error_message=f"Cannot handle URL: {url}"
            )

        try:
            imgur_id, media_type = self._extract_imgur_info(url)
            if not imgur_id:
                return DownloadResult(
                    status=DownloadStatus.INVALID_URL,
                    error_message=f"Could not extract Imgur ID from URL: {url}"
                )

            # Handle different media types
            if media_type == 'album':
                return self._download_album(imgur_id, save_path)
            elif media_type == 'gallery':
                return self._download_gallery(imgur_id, save_path)
            else:
                return self._download_single_image(imgur_id, save_path, url)

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Imgur download failed: {str(e)}"
            )

    def _extract_imgur_info(self, url: str) -> tuple[Optional[str], str]:
        """
        Extract Imgur ID and determine media type from URL.

        Returns:
            Tuple of (imgur_id, media_type) where media_type is 'image', 'album', or 'gallery'
        """
        patterns = [
            # Album: imgur.com/a/albumID
            (r'imgur\.com/a/([a-zA-Z0-9]+)', 'album'),
            # Gallery: imgur.com/gallery/galleryID
            (r'imgur\.com/gallery/([a-zA-Z0-9]+)', 'gallery'),
            # Direct image: i.imgur.com/imageID.ext
            (r'i\.imgur\.com/([a-zA-Z0-9]+)(?:\.[a-zA-Z]{3,4})?', 'image'),
            # Image page: imgur.com/imageID
            (r'imgur\.com/([a-zA-Z0-9]+)(?:\.[a-zA-Z]{3,4})?$', 'image'),
        ]

        for pattern, media_type in patterns:
            match = re.search(pattern, url, re.IGNORECASE)
            if match:
                return match.group(1), media_type

        return None, 'unknown'

    def _api_get(self, url: str, timeout: tuple[float, float]):
        """Fetch an Imgur API endpoint, rotating client IDs after a 429 response."""
        for rotation in range(len(self._client_ids)):
            self._respect_rate_limit()

            client_id = self._get_current_client_id()
            response = self._session.get(
                url,
                headers={
                    'Authorization': f'Client-ID {client_id}',
                    'User-Agent': self.config.user_agent,
                },
                timeout=timeout,
            )

            if response.status_code != 429:
                return response

            self._logger.debug(
                "Imgur API rate limited (429), rotating client ID (%s/%s)",
                rotation + 1,
                len(self._client_ids),
            )
            self._rotate_client_id()

        return None

    def _get_metadata_direct_api(self, imgur_id: str, media_type: str, original_url: str) -> Optional[MediaMetadata]:
        """Get metadata using direct API calls."""
        try:
            if not self._client_ids:
                return None

            if media_type == 'album':
                url = f'https://api.imgur.com/3/album/{imgur_id}'
            elif media_type == 'gallery':
                url = f'https://api.imgur.com/3/gallery/{imgur_id}'
            else:
                url = f'https://api.imgur.com/3/image/{imgur_id}'

            response = self._api_get(url, (5.0, 10.0))
            if response is None:
                return None

            response.raise_for_status()
            data = response.json()

            if data.get('success') and data.get('data'):
                item_data = data['data']

                if media_type in ['album', 'gallery']:
                    return MediaMetadata(
                        url=original_url,
                        media_type=MediaType.ALBUM,
                        title=item_data.get('title'),
                        description=item_data.get('description')
                    )
                else:
                    return MediaMetadata(
                        url=original_url,
                        media_type=MediaType.IMAGE,
                        file_size=item_data.get('size'),
                        width=item_data.get('width'),
                        height=item_data.get('height'),
                        format=item_data.get('type'),
                        title=item_data.get('title'),
                        description=item_data.get('description')
                    )

            return None

        except Exception as e:
            self._logger.debug(f"Direct API metadata extraction failed: {e}")
            return None

    def _download_single_image(self, imgur_id: str, save_path: str, original_url: str) -> DownloadResult:
        """Download a single Imgur image through the API, then direct HTTP."""
        try:
            if self._client_ids:
                self._logger.debug(f"Attempting Imgur API download for {imgur_id}")
                result = self._download_image_via_api(imgur_id, save_path)
                if result.status == DownloadStatus.SUCCESS:
                    self._logger.debug(f"Imgur API download successful for {imgur_id}")
                    return result
                elif result.status == DownloadStatus.RATE_LIMITED:
                    self._logger.debug(f"Imgur API rate limited for {imgur_id}, trying direct fallback")
                elif result.status == DownloadStatus.NOT_FOUND:
                    self._logger.debug(f"Imgur API content not found for {imgur_id}: {result.error_message}")
                    return result  # Don't retry for NOT_FOUND
                else:
                    self._logger.debug(f"Imgur API failed for {imgur_id}, trying direct fallback: {result.error_message}")

            self._logger.debug(f"Attempting direct download for {imgur_id}")
            result = self._download_image_direct(imgur_id, save_path, original_url)

            if result.status == DownloadStatus.SUCCESS:
                self._logger.debug(f"Direct download successful for {imgur_id}")
            else:
                self._logger.warning(f"All download methods failed for {imgur_id}: {result.error_message}")

            return result

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"All download methods failed: {str(e)}"
            )

    def _download_image_via_api(self, imgur_id: str, save_path: str) -> DownloadResult:
        """Download image using direct Imgur API v3 calls."""
        try:
            if not self._client_ids:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="No Imgur client IDs available for API access"
                )

            response = self._api_get(
                f'https://api.imgur.com/3/image/{imgur_id}',
                (5.0, 10.0),
            )
            if response is None:
                return DownloadResult(
                    status=DownloadStatus.RATE_LIMITED,
                    error_message="Imgur API rate limit exceeded (all client IDs exhausted)",
                    retry_after=60
                )

            if response.status_code == 404:
                return DownloadResult(
                    status=DownloadStatus.NOT_FOUND,
                    error_message="Image not found via Imgur API"
                )

            response.raise_for_status()
            data = response.json()

            if not data.get('success') or not data.get('data'):
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="Invalid response from Imgur API"
                )

            image_data = data['data']
            image_url = image_data.get('link')

            if not image_url:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="No image URL in Imgur API response"
                )

            # Download the actual image file
            download_result = self.download_file(image_url, save_path)

            # Enhance result with API metadata if download was successful
            if download_result.is_success and download_result.metadata:
                enhanced_metadata = MediaMetadata(
                    url=image_url,
                    media_type=download_result.metadata.media_type,
                    file_size=download_result.metadata.file_size,
                    width=image_data.get('width'),
                    height=image_data.get('height'),
                    format=image_data.get('type'),
                    title=image_data.get('title'),
                    description=image_data.get('description')
                )
                return DownloadResult(
                    status=download_result.status,
                    local_path=download_result.local_path,
                    metadata=enhanced_metadata,
                    bytes_downloaded=download_result.bytes_downloaded,
                    download_time=download_result.download_time
                )

            return download_result

        except Exception as e:
            error_str = str(e).lower()
            if "rate limit" in error_str:
                self._logger.debug(f"Imgur API rate limit in exception for {imgur_id} (expected behavior)")
                return DownloadResult(
                    status=DownloadStatus.RATE_LIMITED,
                    error_message="Imgur API rate limit exceeded",
                    retry_after=60
                )
            elif "not found" in error_str or "404" in error_str:
                return DownloadResult(
                    status=DownloadStatus.NOT_FOUND,
                    error_message="Image not found via Imgur API"
                )
            else:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message=f"Imgur API download failed: {str(e)}"
                )

    def _download_image_direct(self, imgur_id: str, save_path: str, original_url: str) -> DownloadResult:
        """Download image using direct URL."""
        try:
            # Construct direct image URL
            direct_url = f"https://i.imgur.com/{imgur_id}.jpg"  # Try .jpg first

            # Try common extensions
            for ext in ['.jpg', '.png', '.gif', '.webp']:
                test_url = f"https://i.imgur.com/{imgur_id}{ext}"

                try:
                    self._respect_rate_limit()

                    # Test if URL exists with HEAD request
                    response = self._session.head(test_url, timeout=(5.0, 5.0))
                    if response.status_code == 200:
                        direct_url = test_url
                        break
                except Exception:
                    continue

            # Download the file
            return self.download_file(direct_url, save_path)

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Direct download failed: {str(e)}"
            )

    def _download_album(self, album_id: str, save_path: str) -> DownloadResult:
        """Download an Imgur album."""
        try:
            return self._download_album_direct(album_id, save_path)

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Album download failed: {str(e)}"
            )

    def _download_album_direct(self, album_id: str, save_path: str) -> DownloadResult:
        """Download album using direct API calls."""
        try:
            if not self._client_ids:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="No Imgur client IDs configured"
                )

            response = self._api_get(
                f'https://api.imgur.com/3/album/{album_id}',
                (5.0, 15.0),
            )
            if response is None:
                return DownloadResult(
                    status=DownloadStatus.RATE_LIMITED,
                    error_message="Imgur rate limit exceeded (all client IDs exhausted)",
                    retry_after=60
                )

            response.raise_for_status()
            data = response.json()

            if not data.get('success') or not data.get('data'):
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="Album not found or invalid response"
                )

            album_data = data['data']
            images = album_data.get('images', [])

            # Create album directory
            album_dir = os.path.splitext(save_path)[0] + "_album"
            os.makedirs(album_dir, exist_ok=True)

            total_downloaded = 0
            downloaded_files = []

            for i, image_data in enumerate(images):
                image_url = image_data.get('link')
                if not image_url:
                    continue

                # Generate filename
                file_ext = os.path.splitext(image_url)[1] or '.jpg'
                filename = f"image_{i+1:03d}{file_ext}"
                image_path = os.path.join(album_dir, filename)

                try:
                    result = self.download_file(image_url, image_path)
                    if result.is_success:
                        total_downloaded += result.bytes_downloaded
                        downloaded_files.append(result.local_path)
                except Exception as e:
                    self._logger.warning(f"Failed to download album image {i+1}/{len(images)}: {e}")
                    continue

            if downloaded_files:
                metadata = MediaMetadata(
                    url=f"https://imgur.com/a/{album_id}",
                    media_type=MediaType.ALBUM,
                    title=album_data.get('title'),
                    description=album_data.get('description')
                )

                return DownloadResult(
                    status=DownloadStatus.SUCCESS,
                    local_path=album_dir,
                    metadata=metadata,
                    bytes_downloaded=total_downloaded
                )
            else:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="No images could be downloaded from album"
                )

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Direct album download failed: {str(e)}"
            )

    def _download_gallery(self, gallery_id: str, save_path: str) -> DownloadResult:
        """Download an Imgur gallery (similar to album)."""
        # Gallery handling is similar to album, just different API endpoint
        return self._download_album(gallery_id, save_path)

    def _get_current_client_id(self) -> str:
        """Get current client ID for API calls (thread-safe)."""
        with self._client_lock:
            if not self._client_ids:
                raise ValueError("No Imgur client IDs configured")
            return self._client_ids[self._current_client_index % len(self._client_ids)]

    def _rotate_client_id(self):
        """Rotate to next client ID to handle rate limits (thread-safe)."""
        with self._client_lock:
            if len(self._client_ids) > 1:
                old_index = self._current_client_index
                self._current_client_index = (self._current_client_index + 1) % len(self._client_ids)
                self._logger.debug(f"Rotated from client ID {old_index + 1} to {self._current_client_index + 1} (of {len(self._client_ids)} total)")
            else:
                self._logger.debug("Rate limit encountered but only 1 client ID available, waiting for reset")

    def get_service_name(self) -> str:
        """Get the name of this service."""
        return "Imgur"

    def is_rate_limited(self) -> bool:
        """Check if the service is currently rate limited."""
        # Could implement more sophisticated rate limit tracking here
        return False

    def get_rate_limit_reset_time(self) -> Optional[float]:
        """Get the time when rate limit resets."""
        return None

    def set_client_credentials(self, client_ids: List[str]):
        """Set Imgur client IDs for API access."""
        self._client_ids = client_ids
