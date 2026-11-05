"""
Reddit media downloader service.

Downloads Reddit-hosted media: i.redd.it images, v.redd.it videos (with audio
merging), and gallery posts.
"""

import logging
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urlparse

from praw.models import Submission

from utils.media_services.media_types import (
    DownloadResult, DownloadStatus, GalleryImage, ServiceConfig
)
from utils.media_services.temp_file_utils import temp_files_cleanup
from utils.constants import FFMPEG_PROBE_TIMEOUT_SECONDS, FFMPEG_TIMEOUT_SECONDS
from utils.domain_matching import domain_matches
from utils.media_services.base_downloader import BaseHTTPDownloader

logger = logging.getLogger(__name__)


class RedditMediaDownloader(BaseHTTPDownloader):
    """
    Reddit media downloader supporting i.redd.it, v.redd.it, and galleries.

    This service handles:
    - i.redd.it images (direct download)
    - v.redd.it videos (download video + audio, merge with ffmpeg if available)
    - Reddit galleries (multiple images)
    - Reddit preview images as fallback
    """

    def __init__(self, config: ServiceConfig | None = None):
        if config is None:
            config = ServiceConfig(
                name="Reddit",
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

            return any(domain_matches(domain, reddit_domain) for reddit_domain in reddit_domains)

        except ValueError:
            return False

    def download(self, url: str, save_path: str) -> DownloadResult:
        """Download Reddit media with appropriate handling for different types."""
        if not self.can_handle(url):
            return DownloadResult(
                status=DownloadStatus.INVALID_URL,
                error_message=f"Cannot handle URL: {url}"
            )

        try:
            domain = urlparse(url).hostname

            if domain_matches(domain, 'v.redd.it'):
                return self._download_reddit_video(url, save_path)
            elif domain_matches(domain, 'i.redd.it'):
                return self._download_with_reddit_headers(url, save_path)
            # can_handle admits only preview hosts beyond these
            return self._download_reddit_preview(url, save_path)

        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Reddit media download failed: {str(e)}"
            )

    def _download_reddit_preview(self, url: str, save_path: str) -> DownloadResult:
        """Download Reddit preview image with URL decoding and Reddit headers."""
        # Reddit preview URLs often have encoding issues, clean them up
        return self._download_with_reddit_headers(url.replace('amp;', ''), save_path)

    def _download_reddit_video(self, url: str, save_path: str) -> DownloadResult:
        """
        Download Reddit hosted video (v.redd.it) with audio merging.

        Reddit stores video and audio separately. This method downloads both
        concurrently and attempts to merge them using ffmpeg if available.

        Short redirect URLs (no DASH_ in path) are detected and attempted once;
        on 403 they fail immediately without retries.
        """
        # Detect short v.redd.it redirect URLs (e.g., https://v.redd.it/abc123)
        # These lack a DASH_ segment and usually 403 on direct access
        if 'DASH_' not in urlparse(url).path:
            self._logger.debug(f"Short v.redd.it URL detected (no DASH_ segment): {url}")
            # Attempt a single download — if 403, fail immediately
            video_result = self.download_file(url, save_path)
            if video_result.status == DownloadStatus.FORBIDDEN:
                self._logger.warning(f"Short v.redd.it URL returned 403, failing fast: {url}")
            return video_result

        has_ffmpeg = self._is_ffmpeg_available()
        audio_url = self._get_audio_url_from_video_url(url) if has_ffmpeg else None

        if audio_url:
            return self._download_video_with_audio(url, audio_url, save_path)

        # No audio URL or no ffmpeg, just download video
        video_result = self.download_file(url, save_path)
        if video_result.status == DownloadStatus.NOT_FOUND:
            self._logger.info("Initial DASH quality failed with 404, trying alternatives")
            video_result = self._try_dash_qualities(url, save_path)
        if video_result.is_success and not has_ffmpeg:
            self._logger.warning(f"ffmpeg not found, saved video without its audio track: {url}")
            return replace(video_result, error_message="Audio track not merged (ffmpeg not available)")
        return video_result

    def _download_video_with_audio(self, url: str, audio_url: str, save_path: str) -> DownloadResult:
        """Download video and audio concurrently, then merge them if the audio arrived."""
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_audio:
            temp_audio_path = temp_audio.name

        with temp_files_cleanup(temp_audio_path):
            with ThreadPoolExecutor(max_workers=2) as pool:
                video_future = pool.submit(self.download_file, url, save_path)
                audio_future = pool.submit(self.download_file, audio_url, temp_audio_path)
                video_result = video_future.result()
                audio_result = audio_future.result()

            if video_result.status == DownloadStatus.NOT_FOUND:
                self._logger.info("Video DASH quality failed with 404, trying alternatives")
                video_result = self._try_dash_qualities(url, save_path)
            if not video_result.is_success:
                return video_result

            if audio_result.is_success and video_result.local_path:
                return self._merge_audio_into(video_result, audio_result)

            # Audio failed, return video-only
            return video_result

    def _merge_audio_into(self, video_result: DownloadResult, audio_result: DownloadResult) -> DownloadResult:
        """Replace the video file with its merge with the audio, keeping the video if that fails."""
        audio_size = (
            os.path.getsize(audio_result.local_path)
            if audio_result.local_path and os.path.exists(audio_result.local_path)
            else audio_result.bytes_downloaded
        )
        merged_size_estimate = os.path.getsize(video_result.local_path) + audio_size
        if not self._check_disk_space(merged_size_estimate, video_result.local_path):
            return replace(
                video_result,
                error_message="Audio track not merged (insufficient disk space)",
            )

        with tempfile.NamedTemporaryFile(
            dir=os.path.dirname(video_result.local_path) or '.',
            suffix='.mp4',
            delete=False,
        ) as temp_merged:
            temp_merged_path = temp_merged.name

        with temp_files_cleanup(temp_merged_path):
            merged_result = self._merge_video_audio(
                video_result.local_path,
                audio_result.local_path,
                temp_merged_path,
            )
            if not merged_result.is_success:
                return video_result

            try:
                os.replace(temp_merged_path, video_result.local_path)
            except OSError as error:
                self._logger.warning(
                    "Could not promote merged Reddit video, keeping video-only file: %s",
                    error,
                )
                return video_result
            return replace(
                video_result,
                # The merged file differs from the downloaded video stream.
                content_hash=None,
                bytes_downloaded=merged_result.bytes_downloaded,
            )

    def _get_audio_url_from_video_url(self, video_url: str) -> str | None:
        """
        Generate audio URL from video URL.

        Reddit stores audio at the same base URL but with different filenames
        depending on upload era:
        - Post-July 2023: DASH_AUDIO_128.mp4 (high quality) or DASH_AUDIO_64.mp4
        - Pre-July 2023: DASH_audio.mp4

        Returns the first audio URL that responds successfully, or None.
        """
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
                resp, _ = self._get_with_safe_redirects(
                    audio_url,
                    {"timeout": (3.0, 5.0)},
                    method="head",
                )
                if resp.status_code == 200:
                    self._logger.debug(f"Found audio track: {audio_filename}")
                    return audio_url
            except Exception as e:
                self._logger.debug(f"Audio probe failed for {audio_filename}: {e}")

        self._logger.debug(f"No audio track found for video: {video_url}")
        return None

    def _try_dash_qualities(self, url: str, save_path: str) -> DownloadResult:
        """Try multiple DASH quality tiers when the initial quality returns 404.

        Replaces DASH_NNN in the URL with progressively lower quality tiers.
        """
        qualities = [720, 480, 360, 240]
        last_result = None

        for quality in qualities:
            candidate = re.sub(r'DASH_\d+', f'DASH_{quality}', url)
            if candidate == url:
                continue  # Already tried this quality
            self._logger.debug(f"Trying DASH quality fallback: DASH_{quality}")
            result = self.download_file(candidate, save_path)
            last_result = result
            if result.is_success:
                self._logger.info(f"DASH quality fallback succeeded at {quality}p")
                return result
            if result.status != DownloadStatus.NOT_FOUND:
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
                         check=True,
                         timeout=FFMPEG_PROBE_TIMEOUT_SECONDS)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
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
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

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

            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=FFMPEG_TIMEOUT_SECONDS,  # Configurable timeout
                text=True
            )

            if result.returncode == 0:
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

    @classmethod
    def extract_media_urls_from_submission(cls, submission: Submission) -> list[GalleryImage]:
        """
        Extract the gallery image URLs from a PRAW submission.

        Args:
            submission: PRAW submission object

        Returns:
            The gallery's images, empty when it is not a gallery or has none to download
        """
        media_urls = []

        try:
            if getattr(submission, 'is_gallery', False):
                for item_id, metadata in (getattr(submission, 'media_metadata', None) or {}).items():
                    if 's' in metadata and 'u' in metadata['s']:
                        # Decode URL (Reddit URLs are often HTML encoded)
                        gallery_url = metadata['s']['u'].replace('&amp;', '&')
                        media_urls.append(GalleryImage(url=gallery_url, gallery_id=item_id))

        except Exception as e:
            # Log error but don't fail completely
            logger.warning(f"Error extracting media URLs from submission: {e}")

        return media_urls
