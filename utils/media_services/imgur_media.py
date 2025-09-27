"""
Imgur media downloader service.

This module provides comprehensive support for downloading Imgur-hosted media
including single images, albums, and galleries. Uses PyImgur library as primary
method with fallback to direct API calls using requests.
Implements web-researched best practices for 2024.
"""

import os
import re
from typing import Optional, List
from urllib.parse import urlparse

from ..service_abstractions import (
    DownloadResult, DownloadStatus,
    MediaMetadata, MediaType, ServiceConfig
)
from .base_downloader import BaseHTTPDownloader


class ImgurMediaDownloader(BaseHTTPDownloader):
    """
    Imgur media downloader supporting images, albums, and galleries.

    This service handles:
    - Single images (i.imgur.com/ID.ext or imgur.com/ID)
    - Albums (imgur.com/a/ID)
    - Galleries (imgur.com/gallery/ID)
    - Direct links (i.imgur.com/ID.ext)

    Uses PyImgur when available, falls back to direct API calls.
    Respects Imgur's strict rate limits (~8 requests/minute).
    """

    def __init__(self, config: Optional[ServiceConfig] = None):
        if config is None:
            config = ServiceConfig(
                name="Imgur",
                rate_limit_per_minute=8,  # Imgur allows ~8 requests/minute
                timeout_seconds=30,
                max_file_size=209715200,  # 200MB default
                user_agent="Reddit Stash Media Downloader/1.0"
            )
        super().__init__(config)

        # Try to import PyImgur, fall back to direct API if not available
        self._pyimgur_client = None
        self._client_ids = []
        self._current_client_index = 0
        self._setup_clients()

    def _setup_clients(self):
        """Setup PyImgur client and/or direct API credentials."""
        # Try to import PyImgur
        try:
            import pyimgur
            self._pyimgur_available = True

            # Get client IDs from config (we'll set these up later via configuration)
            client_ids = self.config.api_keys.get('client_ids', []) if self.config.api_keys else []
            client_secrets = self.config.api_keys.get('client_secrets', []) if self.config.api_keys else []

            if client_ids and len(client_ids) > 0:
                # Use first client ID for PyImgur
                client_secret = client_secrets[0] if client_secrets else None
                try:
                    self._pyimgur_client = pyimgur.Imgur(client_ids[0], client_secret)
                    self._client_ids = client_ids
                except Exception as e:
                    print(f"Warning: Failed to initialize PyImgur client: {e}")
                    self._pyimgur_client = None

        except ImportError:
            self._pyimgur_available = False
            print("Warning: PyImgur not available, using direct API calls only")

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

        return any(domain.endswith(imgur_domain) for imgur_domain in imgur_domains)

    def get_metadata(self, url: str) -> Optional[MediaMetadata]:
        """Get metadata for Imgur media without downloading."""
        if not self.can_handle(url):
            return None

        try:
            imgur_id, media_type = self._extract_imgur_info(url)
            if not imgur_id:
                return None

            # Use PyImgur if available, otherwise direct API
            if self._pyimgur_client:
                return self._get_metadata_pyimgur(imgur_id, media_type, url)
            else:
                return self._get_metadata_direct_api(imgur_id, media_type, url)

        except Exception as e:
            print(f"Warning: Failed to get Imgur metadata for {url}: {e}")
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

    def _get_metadata_pyimgur(self, imgur_id: str, media_type: str, original_url: str) -> Optional[MediaMetadata]:
        """Get metadata using PyImgur library."""
        try:
            self._respect_rate_limit()

            if media_type == 'album':
                album = self._pyimgur_client.get_album(imgur_id)
                return MediaMetadata(
                    url=original_url,
                    media_type=MediaType.ALBUM,
                    title=album.title,
                    description=album.description
                )
            elif media_type == 'gallery':
                # Gallery items can be albums or images
                gallery = self._pyimgur_client.get_gallery_item(imgur_id)
                return MediaMetadata(
                    url=original_url,
                    media_type=MediaType.ALBUM if hasattr(gallery, 'images') else MediaType.IMAGE,
                    title=getattr(gallery, 'title', None),
                    description=getattr(gallery, 'description', None)
                )
            else:
                # Single image
                image = self._pyimgur_client.get_image(imgur_id)
                return MediaMetadata(
                    url=original_url,
                    media_type=MediaType.IMAGE,
                    file_size=getattr(image, 'size', None),
                    width=getattr(image, 'width', None),
                    height=getattr(image, 'height', None),
                    format=getattr(image, 'type', None),
                    title=getattr(image, 'title', None),
                    description=getattr(image, 'description', None)
                )

        except Exception as e:
            print(f"Warning: PyImgur metadata extraction failed: {e}")
            return None

    def _get_metadata_direct_api(self, imgur_id: str, media_type: str, original_url: str) -> Optional[MediaMetadata]:
        """Get metadata using direct API calls."""
        try:
            if not self._client_ids:
                return None

            self._respect_rate_limit()

            # Use current client ID with rotation
            client_id = self._get_current_client_id()
            headers = {
                'Authorization': f'Client-ID {client_id}',
                'User-Agent': self.config.user_agent
            }

            if media_type == 'album':
                url = f'https://api.imgur.com/3/album/{imgur_id}'
            elif media_type == 'gallery':
                url = f'https://api.imgur.com/3/gallery/{imgur_id}'
            else:
                url = f'https://api.imgur.com/3/image/{imgur_id}'

            response = self._session.get(url, headers=headers, timeout=(5.0, 10.0))

            if response.status_code == 429:
                # Rate limited, try next client ID
                self._rotate_client_id()
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
            print(f"Warning: Direct API metadata extraction failed: {e}")
            return None

    def _download_single_image(self, imgur_id: str, save_path: str, original_url: str) -> DownloadResult:
        """Download a single Imgur image."""
        try:
            # Try PyImgur first if available
            if self._pyimgur_client:
                return self._download_image_pyimgur(imgur_id, save_path)
            else:
                return self._download_image_direct(imgur_id, save_path, original_url)

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Single image download failed: {str(e)}"
            )

    def _download_image_pyimgur(self, imgur_id: str, save_path: str) -> DownloadResult:
        """Download image using PyImgur."""
        try:
            self._respect_rate_limit()

            image = self._pyimgur_client.get_image(imgur_id)

            # Create directory if needed
            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            # Use PyImgur's download method
            filename = image.download(path=os.path.dirname(save_path),
                                    name=os.path.basename(save_path))

            # Get file size
            file_size = os.path.getsize(filename) if os.path.exists(filename) else 0

            # Create metadata
            metadata = MediaMetadata(
                url=image.link,
                media_type=MediaType.IMAGE,
                file_size=file_size,
                width=getattr(image, 'width', None),
                height=getattr(image, 'height', None),
                format=getattr(image, 'type', None),
                title=getattr(image, 'title', None)
            )

            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=filename,
                metadata=metadata,
                bytes_downloaded=file_size
            )

        except Exception as e:
            if "rate limit" in str(e).lower():
                return DownloadResult(
                    status=DownloadStatus.RATE_LIMITED,
                    error_message="Imgur rate limit exceeded",
                    retry_after=60
                )
            else:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message=f"PyImgur download failed: {str(e)}"
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
            # Get album information
            if self._pyimgur_client:
                return self._download_album_pyimgur(album_id, save_path)
            else:
                return self._download_album_direct(album_id, save_path)

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Album download failed: {str(e)}"
            )

    def _download_album_pyimgur(self, album_id: str, save_path: str) -> DownloadResult:
        """Download album using PyImgur."""
        try:
            self._respect_rate_limit()

            album = self._pyimgur_client.get_album(album_id)

            # Create album directory
            album_dir = os.path.splitext(save_path)[0] + "_album"
            os.makedirs(album_dir, exist_ok=True)

            total_downloaded = 0
            downloaded_files = []

            for i, image in enumerate(album.images):
                # Generate filename
                file_ext = os.path.splitext(image.link)[1] or '.jpg'
                filename = f"image_{i+1:03d}{file_ext}"

                try:
                    self._respect_rate_limit()
                    downloaded_path = image.download(path=album_dir, name=filename)

                    if os.path.exists(downloaded_path):
                        file_size = os.path.getsize(downloaded_path)
                        total_downloaded += file_size
                        downloaded_files.append(downloaded_path)

                except Exception as e:
                    print(f"Warning: Failed to download album image {i+1}: {e}")
                    continue

            if downloaded_files:
                metadata = MediaMetadata(
                    url=f"https://imgur.com/a/{album_id}",
                    media_type=MediaType.ALBUM,
                    title=getattr(album, 'title', None),
                    description=getattr(album, 'description', None)
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
                error_message=f"PyImgur album download failed: {str(e)}"
            )

    def _download_album_direct(self, album_id: str, save_path: str) -> DownloadResult:
        """Download album using direct API calls."""
        try:
            if not self._client_ids:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="No Imgur client IDs configured"
                )

            self._respect_rate_limit()

            # Get album data
            client_id = self._get_current_client_id()
            headers = {
                'Authorization': f'Client-ID {client_id}',
                'User-Agent': self.config.user_agent
            }

            response = self._session.get(
                f'https://api.imgur.com/3/album/{album_id}',
                headers=headers,
                timeout=(5.0, 15.0)
            )

            if response.status_code == 429:
                self._rotate_client_id()
                return DownloadResult(
                    status=DownloadStatus.RATE_LIMITED,
                    error_message="Imgur rate limit exceeded",
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
                    print(f"Warning: Failed to download album image {i+1}: {e}")
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
        """Get current client ID for API calls."""
        if not self._client_ids:
            raise ValueError("No Imgur client IDs configured")
        return self._client_ids[self._current_client_index % len(self._client_ids)]

    def _rotate_client_id(self):
        """Rotate to next client ID to handle rate limits."""
        if len(self._client_ids) > 1:
            self._current_client_index = (self._current_client_index + 1) % len(self._client_ids)
            print(f"Rotated to client ID {self._current_client_index + 1}")

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

    def set_client_credentials(self, client_ids: List[str], client_secrets: Optional[List[str]] = None):
        """
        Set Imgur client credentials for API access.

        Args:
            client_ids: List of Imgur client IDs for rotation
            client_secrets: Optional list of client secrets
        """
        self._client_ids = client_ids

        # Try to setup PyImgur with first client ID
        if self._pyimgur_available and client_ids:
            try:
                import pyimgur
                client_secret = client_secrets[0] if client_secrets else None
                self._pyimgur_client = pyimgur.Imgur(client_ids[0], client_secret)
                print(f"PyImgur client initialized with {len(client_ids)} client ID(s)")
            except Exception as e:
                print(f"Warning: Failed to initialize PyImgur with credentials: {e}")
                self._pyimgur_client = None