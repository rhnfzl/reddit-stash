from __future__ import annotations

import glob
import html
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from ..constants import FFMPEG_TIMEOUT_SECONDS
from ..feature_flags import get_media_config as get_media_feature_config
from ..media_quota import (
    MediaLimitExceeded, file_signature, media_size_limit,
    register_downloaded_file, stream_response_to_file,
)
from ..service_abstractions import (
    DownloadResult,
    DownloadStatus,
    MediaMetadata,
    MediaType,
    ServiceConfig,
)

try:
    import yt_dlp  # type: ignore
    YTDLP_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    yt_dlp = None
    YTDLP_AVAILABLE = False

_logger = logging.getLogger(__name__)


class RedditMediaDownloader:
    """
    Reddit media downloader supporting i.redd.it, v.redd.it, and galleries.
    The important fix is that Reddit-hosted video-like posts should use the richer
    dash/hls source when available, because fallback_url is the silent stream.
    """

    def __init__(self, config: Optional[ServiceConfig] = None):
        if config is None:
            config = ServiceConfig(
                name="Reddit",
                rate_limit_per_minute=100,
                timeout_seconds=30,
                max_file_size=209715200,
                user_agent="Reddit Stash Media Downloader/1.0",
                max_redirects=5,
                connect_timeout=5.0,
                read_timeout=30.0,
                allowed_content_types=["image/*", "video/*", "audio/*"],
                verify_ssl=True,
            )
        self.config = config
        self._media_config = get_media_feature_config()

    def _get_media_size_limit(self, media_kind: str) -> int:
        """Return a byte limit from the active MediaConfig dataclass."""
        # get_media_feature_config is an alias for feature_flags.get_media_config,
        # which already returns the typed MediaConfig dataclass.
        config = get_media_feature_config()
        self._media_config = config
        if not config.is_media_enabled():
            return int(self.config.max_file_size)
        if media_kind in ("image", "preview"):
            return int(config.max_image_size())
        return int(config.max_video_size())

    def can_handle(self, url: str) -> bool:
        """Check if this service can handle the given URL."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            reddit_domains = [
                "i.redd.it",
                "v.redd.it",
                "preview.redd.it",
                "external-preview.redd.it",
                "redgifs.com",
                "gfycat.com",
                "imgur.com",
            ]
            return any(domain.endswith(reddit_domain) for reddit_domain in reddit_domains)
        except Exception:
            return False

    def get_metadata(self, url: str) -> Optional[MediaMetadata]:
        """Get metadata for Reddit media without downloading."""
        if not self.can_handle(url):
            return None

        try:
            response = requests.head(
                url,
                timeout=(5.0, 10.0),
                allow_redirects=True,
                headers={"User-Agent": self.config.user_agent},
            )
            response.raise_for_status()
            media_type = self._determine_reddit_media_type(url, response.headers)
            file_size = int(response.headers.get("content-length", 0))
            return MediaMetadata(
                url=url,
                media_type=media_type,
                file_size=file_size,
                format=self._get_file_extension_from_headers(response.headers),
            )
        except Exception:
            return MediaMetadata(
                url=url,
                media_type=self._determine_reddit_media_type(url, {}),
                file_size=None,
            )

    def download(self, url: str, save_path: str) -> DownloadResult:
        """Download Reddit media with appropriate handling for different types."""
        if not self.can_handle(url):
            return DownloadResult(
                status=DownloadStatus.INVALID_URL,
                error_message=f"Cannot handle URL: {url}",
            )

        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            if "v.redd.it" in domain:
                return self._download_reddit_video(url, save_path)
            if "i.redd.it" in domain:
                return self._download_reddit_image(url, save_path)
            if "preview.redd.it" in domain or "external-preview.redd.it" in domain:
                return self._download_reddit_preview(url, save_path)
            if "redgifs.com" in domain or "gfycat.com" in domain:
                return self._download_reddit_video(url, save_path)

            return self._download_generic(url, save_path)
        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Reddit media download failed: {str(e)}",
            )

    def _determine_reddit_media_type(self, url: str, headers: Dict[str, str]) -> MediaType:
        """Determine media type from Reddit URL patterns."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if "v.redd.it" in domain:
            return MediaType.VIDEO
        if "redgifs.com" in domain or "gfycat.com" in domain:
            return MediaType.VIDEO
        if "i.redd.it" in domain:
            return MediaType.IMAGE
        if "preview.redd.it" in domain or "external-preview.redd.it" in domain:
            return MediaType.IMAGE

        return self._detect_media_type(headers)

    def _detect_media_type(self, headers: Dict[str, str]) -> MediaType:
        """Best-effort detection from HTTP headers."""
        content_type = (headers.get("content-type") or "").lower()

        if "image/" in content_type:
            return MediaType.IMAGE
        if "video/" in content_type:
            return MediaType.VIDEO
        if "audio/" in content_type:
            return MediaType.AUDIO

        return MediaType.UNKNOWN

    def _get_file_extension_from_headers(self, headers: Dict[str, str]) -> str:
        """Best-effort extension guess from response headers."""
        content_type = (headers.get("content-type") or "").lower()

        if "jpeg" in content_type or "jpg" in content_type:
            return ".jpg"
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "gif" in content_type:
            return ".gif"
        if "bmp" in content_type:
            return ".bmp"
        if "tiff" in content_type:
            return ".tiff"
        if "mp4" in content_type:
            return ".mp4"
        if "webm" in content_type:
            return ".webm"
        if "quicktime" in content_type or "mov" in content_type:
            return ".mov"
        if "audio" in content_type or "mpeg" in content_type or "mp3" in content_type:
            return ".mp3"

        return ""

    def _download_generic(self, url: str, save_path: str) -> DownloadResult:
        # Content-Type and URL extension determine whether image or video limits apply.
        return self._download_with_requests(url, save_path, max_file_size=None)

    def _download_reddit_image(self, url: str, save_path: str) -> DownloadResult:
        """Download Reddit hosted image (i.redd.it) with optimized headers."""
        return self._download_with_reddit_headers(
            url,
            save_path,
            max_file_size=self._get_media_size_limit("image"),
        )

    def _download_reddit_preview(self, url: str, save_path: str) -> DownloadResult:
        """Download Reddit preview image with URL decoding and optimized headers."""
        try:
            cleaned_url = html.unescape(url or "").strip()
            return self._download_with_reddit_headers(
                cleaned_url,
                save_path,
                max_file_size=self._get_media_size_limit("preview"),
            )
        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Preview download failed: {str(e)}",
            )

    def _download_reddit_video(self, url: str, save_path: str) -> DownloadResult:
        """
        Download Reddit hosted video.
        Prefer yt-dlp because it can follow dash/hls sources and merge audio when
        the source actually has it.
        """
        best_url = self._get_best_reddit_video_url_by_url(url)

        if YTDLP_AVAILABLE and best_url:
            result = self._download_with_ytdlp(best_url, save_path)
            if result.is_success:
                return result

        if best_url:
            result = self._download_with_requests(
                best_url,
                save_path,
                max_file_size=self._get_media_size_limit("video"),
            )
            if result.is_success:
                return result

        return DownloadResult(
            status=DownloadStatus.FAILED,
            error_message="Could not download Reddit video",
        )

    def _download_with_ytdlp(self, url: str, save_path: str) -> DownloadResult:
        if not YTDLP_AVAILABLE:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message="yt-dlp is not installed",
            )

        output_dir = os.path.dirname(save_path) or "."
        base_name = os.path.splitext(os.path.basename(save_path))[0]
        outtmpl = os.path.join(output_dir, f"{base_name}.%(ext)s")
        max_video_size = int(self._get_media_size_limit("video"))
        patterns = (
            os.path.join(output_dir, f"{base_name}.*"),
            os.path.join(output_dir, f"{base_name}*.mp4"),
            os.path.join(output_dir, f"{base_name}*.webm"),
            os.path.join(output_dir, f"{base_name}*.mkv"),
            os.path.join(output_dir, f"{base_name}*.mov"),
        )
        before_signatures = {
            path: file_signature(path)
            for pattern in patterns
            for path in glob.glob(pattern)
            if os.path.isfile(path)
        }
        options = {
            "outtmpl": outtmpl,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "socket_timeout": 30,
            "paths": {"home": output_dir},
            "max_filesize": max_video_size,
            "nooverwrites": True,
        }

        try:
            os.makedirs(output_dir, exist_ok=True)
            with yt_dlp.YoutubeDL(options) as ydl:  # type: ignore[attr-defined]
                ydl.extract_info(url, download=True)

            candidates = []
            for pattern in patterns:
                candidates.extend(glob.glob(pattern))
            candidates = list(dict.fromkeys(
                path for path in candidates
                if os.path.isfile(path) and not path.endswith((".part", ".ytdl"))
            ))
            if not candidates:
                if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                    size = os.path.getsize(save_path)
                    return DownloadResult(status=DownloadStatus.SUCCESS, local_path=save_path,
                                          bytes_downloaded=size)
                return DownloadResult(status=DownloadStatus.FAILED,
                                      error_message="yt-dlp completed but no output file was found")

            candidates.sort(key=lambda path: os.path.getmtime(path), reverse=True)
            final_path = candidates[0]
            was_downloaded_now = file_signature(final_path) != before_signatures.get(final_path)
            file_size = os.path.getsize(final_path)
            if file_size <= 0:
                return DownloadResult(status=DownloadStatus.FAILED,
                                      error_message="Downloaded video file was empty")

            if was_downloaded_now:
                file_size = register_downloaded_file(final_path, max_video_size)

            # Do not destroy a previously archived file. Return the actual new
            # yt-dlp path if the requested destination already exists.
            if final_path != save_path and not os.path.exists(save_path):
                try:
                    os.replace(final_path, save_path)
                    final_path = save_path
                except OSError:
                    _logger.warning("Could not move yt-dlp output to %s; retaining %s", save_path, final_path)

            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=final_path,
                bytes_downloaded=file_size,
            )
        except MediaLimitExceeded as exc:
            return DownloadResult(status=DownloadStatus.FAILED, error_message=str(exc))
        except subprocess.TimeoutExpired:
            return DownloadResult(status=DownloadStatus.FAILED,
                                  error_message="yt-dlp download timed out")
        except Exception as exc:
            return DownloadResult(status=DownloadStatus.FAILED,
                                  error_message=f"yt-dlp download failed: {exc}")

    def _download_with_requests(
        self, url: str, save_path: str, max_file_size: Optional[int] = None
    ) -> DownloadResult:
        response = None
        try:
            headers = {"User-Agent": self.config.user_agent, "Accept": "*/*"}
            response = requests.get(
                url,
                stream=True,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                headers=headers,
                allow_redirects=True,
            )
            response.raise_for_status()
            limit = int(
                max_file_size if max_file_size is not None
                else media_size_limit(url, response.headers.get("Content-Type", ""))
            )
            written = stream_response_to_file(response, save_path, limit)
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=save_path,
                bytes_downloaded=written,
            )
        except Exception as exc:
            return DownloadResult(status=DownloadStatus.FAILED, error_message=str(exc))
        finally:
            if response is not None:
                response.close()

    def _download_with_reddit_headers(
        self, url: str, save_path: str, max_file_size: Optional[int] = None
    ) -> DownloadResult:
        response = None
        headers = {
            "Accept": "image/*,*/*;q=0.8",
            "User-Agent": self.config.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
        }
        try:
            response = requests.get(
                url,
                stream=True,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                headers=headers,
                allow_redirects=True,
                verify=self.config.verify_ssl,
            )
            response.raise_for_status()
            limit = int(
                max_file_size if max_file_size is not None
                else media_size_limit(url, response.headers.get("Content-Type", "image/"))
            )
            written = stream_response_to_file(response, save_path, limit)
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=save_path,
                bytes_downloaded=written,
            )
        except Exception as exc:
            return DownloadResult(status=DownloadStatus.FAILED, error_message=str(exc))
        finally:
            if response is not None:
                response.close()

    def _is_ffmpeg_available(self) -> bool:
        """Check if ffmpeg is available in system PATH."""
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _has_audio_stream(self, file_path: str) -> bool:
        """Best-effort check for an audio stream in a media file."""
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=index",
                    "-of",
                    "csv=p=0",
                    file_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            return bool((result.stdout or "").strip())
        except Exception:
            return False

    def _merge_video_audio(self, video_path: str, audio_path: str, output_path: str) -> DownloadResult:
        """Merge video and audio files using ffmpeg."""
        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            cmd = [
                "ffmpeg",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                "-y",
                output_path,
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=FFMPEG_TIMEOUT_SECONDS,
                text=True,
            )
            if result.returncode == 0:
                file_size = os.path.getsize(output_path)
                return DownloadResult(
                    status=DownloadStatus.SUCCESS,
                    local_path=output_path,
                    bytes_downloaded=file_size,
                )
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"ffmpeg failed: {result.stderr}",
            )
        except subprocess.TimeoutExpired:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message="Video merge timed out",
            )
        except Exception as e:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Video merge failed: {str(e)}",
            )

    def _get_best_reddit_video_url_by_url(self, url: str) -> Optional[str]:
        """Best-effort URL normalization for reddit-hosted or gif-like video URLs."""
        if not url:
            return None

        cleaned = html.unescape(url).strip()
        parsed = urlparse(cleaned)
        domain = parsed.netloc.lower()

        if "v.redd.it" in domain:
            return cleaned

        return cleaned

    def get_service_name(self) -> str:
        return "Reddit"

    def is_rate_limited(self) -> bool:
        return False

    def get_rate_limit_reset_time(self) -> Optional[float]:
        return None

    @staticmethod
    def _get_best_reddit_video_url(submission) -> Optional[str]:
        """
        Return the most useful Reddit video URL for a submission.
        Prefer dash_url / hls_url first because those are the richer sources.
        fallback_url is the video-only stream and is the last resort.
        """
        try:
            for attr in ("media", "secure_media"):
                data = getattr(submission, attr, None)
                if not isinstance(data, dict):
                    continue

                reddit_video = data.get("reddit_video")
                if not isinstance(reddit_video, dict):
                    continue

                for key in ("dash_url", "hls_url", "fallback_url"):
                    candidate = reddit_video.get(key)
                    if candidate:
                        return html.unescape(candidate)

            preview = getattr(submission, "preview", None)
            if isinstance(preview, dict):
                reddit_video_preview = preview.get("reddit_video_preview")
                if isinstance(reddit_video_preview, dict):
                    for key in ("dash_url", "hls_url", "fallback_url"):
                        candidate = reddit_video_preview.get(key)
                        if candidate:
                            return html.unescape(candidate)
        except Exception:
            pass

        return getattr(submission, "url", None)

    @classmethod
    def extract_media_urls_from_submission(cls, submission) -> List[Dict[str, Any]]:
        """Extract all media URLs from a PRAW submission."""
        media_urls: List[Dict[str, Any]] = []

        try:
            # Check if it's Reddit-hosted media
            if hasattr(submission, "is_reddit_media_domain") and submission.is_reddit_media_domain:
                if hasattr(submission, "domain"):
                    if submission.domain == "i.redd.it":
                        media_urls.append({
                            "url": submission.url,
                            "type": "image",
                            "source": "reddit_direct",
                        })
                    elif submission.domain == "v.redd.it":
                        video_url = cls._get_best_reddit_video_url(submission)
                        media_urls.append({
                            "url": video_url or submission.url,
                            "type": "video",
                            "source": "reddit_direct",
                            "fallback_used": bool(video_url and video_url != submission.url),
                        })

            # Gallery posts: preserve visible order if gallery_data exists
            if hasattr(submission, "is_gallery") and submission.is_gallery:
                media_metadata = getattr(submission, "media_metadata", None) or {}
                ordered_ids = []

                gallery_data = getattr(submission, "gallery_data", None)
                if isinstance(gallery_data, dict):
                    for item in gallery_data.get("items", []):
                        if not isinstance(item, dict):
                            continue
                        media_id = item.get("media_id") or item.get("id") or item.get("mediaId")
                        if media_id:
                            ordered_ids.append(media_id)

                # Fallback to dict order only if gallery_data is unavailable
                if not ordered_ids and media_metadata:
                    ordered_ids = list(media_metadata.keys())

                for item_id in ordered_ids:
                    metadata = media_metadata.get(item_id)
                    if not isinstance(metadata, dict):
                        continue

                    source = metadata.get("s")
                    if not isinstance(source, dict):
                        continue

                    gallery_url = source.get("u")
                    if not gallery_url:
                        continue

                    media_urls.append({
                        "url": html.unescape(gallery_url),
                        "type": "image",
                        "source": "reddit_gallery",
                        "gallery_id": item_id,
                    })

            # Preview images fallback
            if hasattr(submission, "preview") and submission.preview and "images" in submission.preview:
                for image in submission.preview["images"]:
                    if "source" in image:
                        preview_url = image["source"]["url"]
                        media_urls.append({
                            "url": html.unescape(preview_url),
                            "type": "image",
                            "source": "reddit_preview",
                            "width": image["source"].get("width"),
                            "height": image["source"].get("height"),
                        })
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Error extracting media URLs from submission: {e}")

        return media_urls
