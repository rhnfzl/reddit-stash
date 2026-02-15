"""
Reddit media downloader service.

This module provides comprehensive support for downloading Reddit-hosted media
including i.redd.it images, v.redd.it videos (with audio merging), and gallery posts.
Implements the MediaDownloaderProtocol with web-researched best practices.
"""

import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

from ..service_abstractions import (
    DownloadResult, DownloadStatus,
    MediaMetadata, MediaType, ServiceConfig
)
from ..temp_file_utils import temp_files_cleanup
from ..constants import FFMPEG_TIMEOUT_SECONDS
from .base_downloader import BaseHTTPDownloader


class RedditMediaDownloader(BaseHTTPDownloader):
    """
    Reddit media downloader supporting i.redd.it, v.redd.it, and galleries.

    This service handles:
    - i.redd.it images (direct download)
    - v.redd.it videos (download video + audio, merge with ffmpeg if available)
    - Reddit galleries (multiple images)
    - Reddit preview images as fallback
    """

    def __init__(self, config: Optional[ServiceConfig] = None):
        if config is None:
            config = ServiceConfig(
                name="Reddit",
                rate_limit_per_minute=100,  # Reddit API allows ~100 requests/min
                timeout_seconds=30,
                max_file_size=209715200,  # 200MB default
                user_agent="Reddit Stash Media Downloader/1.0",
                # Security enhancements
                max_redirects=5,  # Reddit may redirect through multiple services
                connect_timeout=5.0,
                read_timeout=30.0,
                allowed_content_types=['image/*', 'video/*', 'audio/*'],  # Allow media content
                verify_ssl=True
            )
        super().__init__(config)

    def can_handle(self, url: str) -> bool:
        """Check if this service can handle the given URL."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            # Handle Reddit media domains
            reddit_domains = [
                'i.redd.it',
                'v.redd.it',
                'preview.redd.it',
                'external-preview.redd.it'
            ]

            return any(domain.endswith(reddit_domain) for reddit_domain in reddit_domains)

        except Exception:
            return False

    def get_metadata(self, url: str) -> Optional[MediaMetadata]:
        """Get metadata for Reddit media without downloading."""
        if not self.can_handle(url):
            return None

        try:
            # Respect rate limiting
            self._respect_rate_limit()

            # Make HEAD request to get metadata
            response = self._session.head(
                url,
                timeout=(5.0, 10.0),
                allow_redirects=True
            )
            response.raise_for_status()

            # Determine media type from URL and headers
            media_type = self._determine_reddit_media_type(url, response.headers)
            file_size = int(response.headers.get('content-length', 0))

            return MediaMetadata(
                url=url,
                media_type=media_type,
                file_size=file_size,
                format=self._get_file_extension_from_headers(response.headers)
            )

        except Exception:
            # Return basic metadata even if HEAD request fails
            return MediaMetadata(
                url=url,
                media_type=self._determine_reddit_media_type(url, {}),
                file_size=None
            )

    def download(self, url: str, save_path: str) -> DownloadResult:
        """Download Reddit media with appropriate handling for different types."""
        if not self.can_handle(url):
            return DownloadResult(
                status=DownloadStatus.INVALID_URL,
                error_message=f"Cannot handle URL: {url}"
            )

        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            if 'v.redd.it' in domain:
                return self._download_reddit_video(url, save_path)
            elif 'i.redd.it' in domain:
                return self._download_reddit_image(url, save_path)
            elif 'preview.redd.it' in domain or 'external-preview.redd.it' in domain:
                return self._download_reddit_preview(url, save_path)
            else:
                # Fallback to generic download
                return self.download_file(url, save_path)

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Reddit media download failed: {str(e)}"
            )

    def _determine_reddit_media_type(self, url: str, headers: Dict[str, str]) -> MediaType:
        """Determine media type from Reddit URL patterns."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if 'v.redd.it' in domain:
            return MediaType.VIDEO
        elif 'i.redd.it' in domain:
            return MediaType.IMAGE
        else:
            # Use headers if available
            return self._detect_media_type(headers)

    def _download_reddit_image(self, url: str, save_path: str) -> DownloadResult:
        """Download Reddit hosted image (i.redd.it) with optimized headers."""
        return self._download_with_reddit_headers(url, save_path)

    def _download_reddit_preview(self, url: str, save_path: str) -> DownloadResult:
        """Download Reddit preview image with URL decoding and optimized headers."""
        try:
            # Reddit preview URLs often have encoding issues, clean them up
            cleaned_url = url.replace('amp;', '')
            return self._download_with_reddit_headers(cleaned_url, save_path)
        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Preview download failed: {str(e)}"
            )

    def _download_reddit_video(self, url: str, save_path: str) -> DownloadResult:
        """
        Download Reddit hosted video (v.redd.it) with audio merging.

        Reddit stores video and audio separately. This method downloads both
        concurrently and attempts to merge them using ffmpeg if available.

        Short redirect URLs (no DASH_ in path) are detected and attempted once;
        on 403 they fail immediately without retries.
        """
        import logging
        _logger = logging.getLogger(__name__)

        try:
            # Detect short v.redd.it redirect URLs (e.g., https://v.redd.it/abc123)
            # These lack a DASH_ segment and usually 403 on direct access
            parsed = urlparse(url)
            is_short_url = 'DASH_' not in parsed.path

            if is_short_url:
                _logger.info(f"Short v.redd.it URL detected (no DASH_ segment): {url}")
                # Attempt a single download — if 403, fail immediately
                video_result = self.download_file(url, save_path)
                if not video_result.is_success:
                    error = video_result.error_message or ""
                    if '403' in error or 'Forbidden' in error:
                        _logger.warning(f"Short v.redd.it URL returned 403, failing fast: {url}")
                    return video_result
                return video_result

            # Check prerequisites before downloading
            has_ffmpeg = self._is_ffmpeg_available()
            audio_url = self._get_audio_url_from_video_url(url) if has_ffmpeg else None

            if audio_url:
                # Download video and audio concurrently
                with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_audio:
                    temp_audio_path = temp_audio.name

                with temp_files_cleanup(temp_audio_path):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        video_future = pool.submit(self.download_file, url, save_path)
                        audio_future = pool.submit(self.download_file, audio_url, temp_audio_path)
                        video_result = video_future.result()
                        audio_result = audio_future.result()

                    if not video_result.is_success:
                        # If 404, try alternative DASH quality tiers
                        if 'DASH_' in url:
                            error = video_result.error_message or ""
                            if '404' in error or 'Not Found' in error:
                                _logger.info("Video DASH quality failed with 404, trying alternatives")
                                video_result = self._try_dash_qualities(url, save_path)
                        if not video_result.is_success:
                            return video_result

                    if audio_result.is_success:
                        merged_result = self._merge_video_audio(
                            video_result.local_path,
                            audio_result.local_path,
                            save_path
                        )
                        if merged_result.is_success:
                            return replace(
                                video_result,
                                local_path=merged_result.local_path,
                                bytes_downloaded=video_result.bytes_downloaded + audio_result.bytes_downloaded
                            )

                    # Audio failed or merge failed, return video-only
                    return video_result
            else:
                # No audio URL or no ffmpeg, just download video
                video_result = self.download_file(url, save_path)
                # If 404, try alternative DASH quality tiers
                if not video_result.is_success and 'DASH_' in url:
                    error = video_result.error_message or ""
                    if '404' in error or 'Not Found' in error:
                        _logger.info("Initial DASH quality failed with 404, trying alternatives")
                        video_result = self._try_dash_qualities(url, save_path)
                if video_result.is_success and not has_ffmpeg:
                    return replace(video_result, error_message="Audio track not merged (ffmpeg not available)")
                return video_result

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Reddit video download failed: {str(e)}"
            )

    def _get_audio_url_from_video_url(self, video_url: str) -> Optional[str]:
        """
        Generate audio URL from video URL.

        Reddit stores audio at the same base URL but with different filenames
        depending on upload era:
        - Post-July 2023: DASH_AUDIO_128.mp4 (high quality) or DASH_AUDIO_64.mp4
        - Pre-July 2023: DASH_audio.mp4

        Returns the first audio URL that responds successfully, or None.
        """
        import logging
        _logger = logging.getLogger(__name__)

        try:
            parsed = urlparse(video_url)
            path_parts = parsed.path.split('/')

            # Find the video filename index (e.g., DASH_1080.mp4)
            dash_index = None
            for i, part in enumerate(path_parts):
                if 'DASH_' in part and '.mp4' in part:
                    dash_index = i
                    break

            if dash_index is None:
                return None

            # Try multiple audio filename patterns (most common first to minimize HEAD requests)
            audio_filenames = ['DASH_audio.mp4', 'DASH_AUDIO_128.mp4', 'DASH_AUDIO_64.mp4']

            for audio_filename in audio_filenames:
                candidate_parts = path_parts.copy()
                candidate_parts[dash_index] = audio_filename
                audio_path = '/'.join(candidate_parts)
                audio_url = f"{parsed.scheme}://{parsed.netloc}{audio_path}"
                if parsed.query:
                    audio_url += f"?{parsed.query}"

                # HEAD request to check if audio file exists
                try:
                    resp = self._session.head(
                        audio_url,
                        timeout=(3.0, 5.0),
                        allow_redirects=True
                    )
                    if resp.status_code == 200:
                        _logger.debug(f"Found audio track: {audio_filename}")
                        return audio_url
                except Exception:
                    continue

            _logger.debug(f"No audio track found for video: {video_url}")
            return None

        except Exception:
            return None

    def _try_dash_qualities(self, url: str, save_path: str) -> DownloadResult:
        """Try multiple DASH quality tiers when the initial quality returns 404.

        Replaces DASH_NNN in the URL with progressively lower quality tiers.
        """
        import logging
        _logger = logging.getLogger(__name__)

        qualities = [720, 480, 360, 240]
        last_result = None

        for quality in qualities:
            candidate = re.sub(r'DASH_\d+', f'DASH_{quality}', url)
            if candidate == url:
                continue  # Already tried this quality
            _logger.debug(f"Trying DASH quality fallback: DASH_{quality}")
            result = self.download_file(candidate, save_path)
            last_result = result
            if result.is_success:
                _logger.info(f"DASH quality fallback succeeded at {quality}p")
                return result
            error = result.error_message or ""
            if '404' not in error and 'Not Found' not in error:
                break  # Non-404 error, stop trying

        return last_result or DownloadResult(
            status=DownloadStatus.FAILED,
            error_message="All DASH quality tiers failed"
        )

    def _download_with_reddit_headers(self, url: str, save_path: str) -> DownloadResult:
        """
        Download Reddit images with optimal headers to prevent HTML wrapper pages.

        Reddit uses content negotiation - when it sees browser-like Accept headers
        prioritizing text/html, it serves HTML wrapper pages instead of raw images.
        Per-request headers are passed via extra_headers to avoid mutating the shared
        session (which is unsafe under concurrent ThreadPoolExecutor downloads).
        """
        reddit_headers = {
            'Accept': 'image/*,*/*;q=0.8',
            'User-Agent': self.config.user_agent,
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache'
        }
        return self.download_file(url, save_path, extra_headers=reddit_headers)

    def _is_ffmpeg_available(self) -> bool:
        """Check if ffmpeg is available in system PATH."""
        try:
            subprocess.run(['ffmpeg', '-version'],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _merge_video_audio(self, video_path: str, audio_path: str, output_path: str) -> DownloadResult:
        """
        Merge video and audio files using ffmpeg.

        Args:
            video_path: Path to video file
            audio_path: Path to audio file
            output_path: Path for merged output

        Returns:
            DownloadResult indicating success/failure
        """
        try:
            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Use ffmpeg to merge video and audio
            cmd = [
                'ffmpeg',
                '-i', video_path,
                '-i', audio_path,
                '-c:v', 'copy',  # Copy video stream without re-encoding
                '-c:a', 'aac',   # Encode audio as AAC
                '-shortest',     # Stop when shortest stream ends
                '-y',            # Overwrite output file
                output_path
            ]

            # Run ffmpeg with timeout
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=FFMPEG_TIMEOUT_SECONDS,  # Configurable timeout
                text=True
            )

            if result.returncode == 0:
                # Get file size of merged file
                file_size = os.path.getsize(output_path)

                return DownloadResult(
                    status=DownloadStatus.SUCCESS,
                    local_path=output_path,
                    bytes_downloaded=file_size
                )
            else:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message=f"ffmpeg failed: {result.stderr}"
                )

        except subprocess.TimeoutExpired:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message="Video merge timed out"
            )
        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Video merge failed: {str(e)}"
            )

    def get_service_name(self) -> str:
        """Get the name of this service."""
        return "Reddit"

    def is_rate_limited(self) -> bool:
        """Check if the service is currently rate limited."""
        # Reddit rate limiting is handled by PRAW at the API level
        # For direct media downloads, we use our own rate limiting
        return False

    def get_rate_limit_reset_time(self) -> Optional[float]:
        """Get the time when rate limit resets."""
        return None

    @classmethod
    def extract_media_urls_from_submission(cls, submission) -> List[Dict[str, Any]]:
        """
        Extract all media URLs from a PRAW submission.

        Args:
            submission: PRAW submission object

        Returns:
            List of dictionaries containing URL and metadata
        """
        media_urls = []

        try:
            # Check if it's Reddit-hosted media
            if hasattr(submission, 'is_reddit_media_domain') and submission.is_reddit_media_domain:
                if hasattr(submission, 'domain'):
                    if submission.domain == 'i.redd.it':
                        # Reddit image
                        media_urls.append({
                            'url': submission.url,
                            'type': 'image',
                            'source': 'reddit_direct'
                        })
                    elif submission.domain == 'v.redd.it':
                        # Reddit video
                        media_urls.append({
                            'url': submission.url,
                            'type': 'video',
                            'source': 'reddit_direct'
                        })

            # Check for gallery posts
            if hasattr(submission, 'is_gallery') and submission.is_gallery:
                if hasattr(submission, 'media_metadata') and submission.media_metadata:
                    for item_id, metadata in submission.media_metadata.items():
                        if 's' in metadata and 'u' in metadata['s']:
                            # Decode URL (Reddit URLs are often HTML encoded)
                            gallery_url = metadata['s']['u'].replace('&amp;', '&')
                            media_urls.append({
                                'url': gallery_url,
                                'type': 'image',
                                'source': 'reddit_gallery',
                                'gallery_id': item_id
                            })

            # Check for preview images (fallback for external links)
            if hasattr(submission, 'preview') and submission.preview and 'images' in submission.preview:
                for image in submission.preview['images']:
                    if 'source' in image:
                        preview_url = image['source']['url'].replace('&amp;', '&')
                        media_urls.append({
                            'url': preview_url,
                            'type': 'image',
                            'source': 'reddit_preview',
                            'width': image['source'].get('width'),
                            'height': image['source'].get('height')
                        })

        except Exception as e:
            # Log error but don't fail completely
            import logging
            logging.getLogger(__name__).warning(f"Error extracting media URLs from submission: {e}")

        return media_urls