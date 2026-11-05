"""
Imgur media downloader service.

Downloads Imgur-hosted images, albums and galleries through Imgur's direct API.
"""

import os
import re
import threading
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from utils.media_services.media_types import (
    DownloadResult, DownloadStatus,
    MediaMetadata, MediaType, SERVICE_FAILURE_STATUSES, ServiceConfig
)
from utils.domain_matching import domain_matches
from utils.rate_limiter import RateLimitTimeout, parse_retry_after, rate_limit_manager
from utils.media_services.base_downloader import BaseHTTPDownloader

IMGUR_API_BASE = 'https://api.imgur.com/3'
IMGUR_DIRECT_BASE = 'https://i.imgur.com'
IMGUR_WEB_BASE = 'https://imgur.com'


def _status_of_empty_album(image_statuses: list[DownloadStatus]) -> DownloadStatus:
    """Classify an album that saved no image. Only failures on the service's side count against Imgur."""
    if SERVICE_FAILURE_STATUSES.intersection(image_statuses):
        return DownloadStatus.FAILED
    if all(status == DownloadStatus.NOT_FOUND for status in image_statuses):
        return DownloadStatus.NOT_FOUND
    return DownloadStatus.REJECTED


class ImgurMediaDownloader(BaseHTTPDownloader):
    """
    Imgur media downloader supporting images, albums, and galleries.

    This service handles:
    - Single images (i.imgur.com/ID.ext or imgur.com/ID)
    - Albums (imgur.com/a/ID)
    - Galleries (imgur.com/gallery/ID)
    - Direct links (i.imgur.com/ID.ext)

    Rate limited by the 'imgur' entry in utils.rate_limiter.
    """

    def __init__(self, config: ServiceConfig | None = None):
        if config is None:
            config = ServiceConfig(
                name="Imgur",
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

        self._client_ids = []
        self._current_client_index = 0
        self._client_lock = threading.Lock()

    def can_handle(self, url: str) -> bool:
        """Check if this service can handle the given URL."""
        try:
            return domain_matches(urlparse(url).hostname, 'imgur.com')
        except ValueError:
            return False

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
                return self._download_album_direct(imgur_id, save_path)
            elif media_type == 'gallery':
                return self._download_album_direct(imgur_id, save_path, 'gallery')
            else:
                return self._download_single_image(imgur_id, save_path)

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Imgur download failed: {str(e)}"
            )

    def _extract_imgur_info(self, url: str) -> tuple[str | None, str]:
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

    def _api_get(self, url: str, timeout: tuple[float, float]) -> tuple[Any | None, int | None]:
        """Fetch an Imgur API endpoint, rotating client IDs after a 429 response.

        Returns:
            The response and None, or None and the Retry-After seconds once every client ID is limited.
        """
        retry_after = None
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
                rate_limit_manager.report_response(self.config.name.lower(), response.status_code)
                return response, None

            retry_after = parse_retry_after(response)
            response.close()

            self._logger.debug(
                "Imgur API rate limited (429), rotating client ID (%s/%s)",
                rotation + 1,
                len(self._client_ids),
            )
            self._rotate_client_id()

        rate_limit_manager.report_response(self.config.name.lower(), 429, retry_after)
        return None, retry_after

    def _download_single_image(self, imgur_id: str, save_path: str) -> DownloadResult:
        """Download a single Imgur image through the API, then direct HTTP."""
        if self._client_ids:
            self._logger.debug(f"Attempting Imgur API download for {imgur_id}")
            result = self._download_image_via_api(imgur_id, save_path)
            if result.status in (DownloadStatus.SUCCESS, DownloadStatus.NOT_FOUND):
                self._logger.debug(f"Imgur API finished {imgur_id} with {result.status.value}")
                return result
            self._logger.debug(f"Imgur API failed for {imgur_id}, trying direct fallback: {result.error_message}")

        self._logger.debug(f"Attempting direct download for {imgur_id}")
        result = self._download_image_direct(imgur_id, save_path)

        if result.status == DownloadStatus.SUCCESS:
            self._logger.debug(f"Direct download successful for {imgur_id}")
        else:
            self._logger.warning(f"All download methods failed for {imgur_id}: {result.error_message}")

        return result

    def _fetch_api_data(self, url: str, timeout: tuple[float, float],
                        not_found_message: str) -> tuple[dict | None, DownloadResult | None]:
        """Fetch an Imgur API endpoint. Returns its JSON body, or the result to return instead."""
        response, retry_after = self._api_get(url, timeout)
        if response is None:
            return None, DownloadResult(
                status=DownloadStatus.RATE_LIMITED,
                error_message="Imgur API rate limit exceeded (all client IDs exhausted)",
                retry_after=retry_after,
            )

        if response.status_code == 404:
            return None, DownloadResult(
                status=DownloadStatus.NOT_FOUND,
                error_message=not_found_message,
            )

        response.raise_for_status()
        return response.json(), None

    def _download_image_via_api(self, imgur_id: str, save_path: str) -> DownloadResult:
        """Download image using direct Imgur API v3 calls."""
        try:
            data, failure = self._fetch_api_data(
                f'{IMGUR_API_BASE}/image/{imgur_id}',
                (5.0, 10.0),
                "Image not found via Imgur API",
            )
            if failure:
                return failure

            if not data.get('success') or not data.get('data'):
                return DownloadResult(status=DownloadStatus.FAILED, error_message="Invalid response from Imgur API")

            image_data = data['data']
            image_url = image_data.get('link')

            if not image_url:
                return DownloadResult(status=DownloadStatus.FAILED, error_message="No image URL in Imgur API response")

            # Download the actual image file
            download_result = self.download_file(image_url, save_path)

            # Enhance result with API metadata if download was successful
            if download_result.is_success and download_result.metadata:
                return replace(download_result, metadata=replace(
                    download_result.metadata,
                    url=image_url,
                    width=image_data.get('width'),
                    height=image_data.get('height'),
                    format=image_data.get('type'),
                    title=image_data.get('title'),
                    description=image_data.get('description'),
                ))

            return download_result

        except RateLimitTimeout:
            self._logger.debug(f"Imgur API rate limit wait timed out for {imgur_id} (expected behavior)")
            return DownloadResult(status=DownloadStatus.RATE_LIMITED, error_message="Imgur API rate limit exceeded")
        except Exception as e:
            # A failed result sends _download_single_image on to direct download; raising would skip it.
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Imgur API download failed: {str(e)}"
            )

    def _download_image_direct(self, imgur_id: str, save_path: str) -> DownloadResult:
        """Download image using direct URL."""
        # Construct direct image URL
        direct_url = f"{IMGUR_DIRECT_BASE}/{imgur_id}.jpg"  # Try .jpg first

        # Try common extensions
        for ext in ['.jpg', '.png', '.gif', '.webp']:
            test_url = f"{IMGUR_DIRECT_BASE}/{imgur_id}{ext}"

            try:
                self._respect_rate_limit()

                # Test if URL exists with HEAD request
                response = self._session.head(test_url, timeout=(5.0, 5.0), allow_redirects=False)
                status_code = response.status_code
                rate_limit_manager.report_response(
                    self.config.name.lower(), status_code, parse_retry_after(response)
                )
                response.close()
                if status_code == 200:
                    direct_url = test_url
                    break
                if status_code == 429:
                    # Earlier probes may have ruled out the default URL, so no guessing from here.
                    return DownloadResult(
                        status=DownloadStatus.RATE_LIMITED,
                        error_message=f"Imgur rate limited the probe for {imgur_id}",
                    )
            except RateLimitTimeout as e:
                return DownloadResult(status=DownloadStatus.RATE_LIMITED, error_message=str(e))
            except Exception as e:
                self._logger.debug(f"Imgur probe failed for {test_url}: {e}")

        # Download the file
        return self.download_file(direct_url, save_path)

    def _download_album_direct(self, album_id: str, save_path: str, endpoint: str = 'album') -> DownloadResult:
        """Download an Imgur album or gallery through its API endpoint."""
        if not self._client_ids:
            # Local config, not service health: must not count against the breaker.
            return DownloadResult(status=DownloadStatus.REJECTED, error_message="No Imgur client IDs configured")

        data, failure = self._fetch_api_data(
            f'{IMGUR_API_BASE}/{endpoint}/{album_id}',
            (5.0, 15.0),
            "Album not found via Imgur API",
        )
        if failure:
            return failure

        if not data.get('success') or not data.get('data'):
            return DownloadResult(
                status=DownloadStatus.NOT_FOUND,
                error_message="Album not found or invalid response"
            )

        album_data = data['data']

        album_dir = os.path.splitext(save_path)[0] + "_album"
        os.makedirs(album_dir, exist_ok=True)

        downloaded_files, total_downloaded, image_statuses = self._download_album_images(
            album_data.get('images', []), album_dir
        )

        if not downloaded_files:
            return DownloadResult(
                status=_status_of_empty_album(image_statuses),
                error_message="No images could be downloaded from album"
            )

        web_path = 'gallery' if endpoint == 'gallery' else 'a'
        return DownloadResult(
            status=DownloadStatus.SUCCESS,
            local_path=album_dir,
            metadata=MediaMetadata(
                url=f"{IMGUR_WEB_BASE}/{web_path}/{album_id}",
                media_type=MediaType.ALBUM,
                title=album_data.get('title'),
                description=album_data.get('description')
            ),
            bytes_downloaded=total_downloaded
        )

    def _download_album_images(self, images: list[dict], album_dir: str) -> tuple[list[str], int, list[DownloadStatus]]:
        """Download each album image. Returns (saved paths, total bytes, every image's status)."""
        total_downloaded = 0
        downloaded_files = []
        image_statuses = []

        for i, image_data in enumerate(images):
            image_url = image_data.get('link')
            if not image_url:
                continue

            # Generate filename
            file_ext = os.path.splitext(image_url)[1] or '.jpg'
            filename = f"image_{i+1:03d}{file_ext}"

            result = self.download_file(image_url, os.path.join(album_dir, filename))
            image_statuses.append(result.status)
            if result.is_success:
                total_downloaded += result.bytes_downloaded
                downloaded_files.append(result.local_path)

        return downloaded_files, total_downloaded, image_statuses

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

    def set_client_credentials(self, client_ids: list[str]):
        """Set Imgur client IDs for API access."""
        self._client_ids = client_ids
