from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from utils.feature_flags import get_media_config
from utils.media_quota import (
    MediaLimitExceeded, file_signature, media_size_limit,
    register_downloaded_file, stream_response_to_file,
)

from .media_services.reddit_media import RedditMediaDownloader

logger = logging.getLogger(__name__)

try:
    import yt_dlp  # type: ignore
    YT_DLP_AVAILABLE = True
except Exception:
    yt_dlp = None
    YT_DLP_AVAILABLE = False

REDGIFS_TRACE = os.getenv("REDGIFS_TRACE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

REDDIT_MEDIA_HOSTS = (
    "i.redd.it",
    "v.redd.it",
    "preview.redd.it",
    "external-preview.redd.it",
)

REDGIFS_HOSTS = (
    "redgifs.com",
)

VIDEO_PAGE_HOSTS = (
    "gfycat.com",
    "giphy.com",
    "streamable.com",
    "imgur.com",
)


def _trace(message: str) -> None:
    if REDGIFS_TRACE:
        print(f"[RedGifs] {message}", flush=True)
    logger.info(message)


def _host(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


def _path(url: str) -> str:
    try:
        return urlparse(url or "").path.lower()
    except Exception:
        return ""


def _normalize(url: Optional[str]) -> str:
    if not url:
        return ""
    return str(url).replace("\\/", "/").replace("&", "&").strip()


def _is_reddit_media(url: str) -> bool:
    domain = _host(url)
    return any(domain.endswith(host) for host in REDDIT_MEDIA_HOSTS)


def _is_redgifs(url: str) -> bool:
    domain = _host(url)
    return any(domain.endswith(host) for host in REDGIFS_HOSTS)


def _is_video_page(url: str) -> bool:
    domain = _host(url)
    path = _path(url)
    if not domain:
        return False
    if any(domain.endswith(host) for host in VIDEO_PAGE_HOSTS):
        return True
    if path.endswith(".gifv"):
        return True
    return False


def _is_direct_image(url: str) -> bool:
    domain = _host(url)
    path = _path(url)
    if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff")):
        return True
    if any(
        domain.endswith(host)
        for host in ("i.redd.it", "i.imgur.com", "preview.redd.it", "external-preview.redd.it")
    ):
        return True
    return False


def _infer_media_extension(url: str) -> str:
    """
    Pick a sensible extension for the initial save path.
    """
    try:
        parsed = urlparse(url or "")
        domain = parsed.netloc.lower()
        path = parsed.path.lower()
        filename = os.path.basename(path)
        _, ext = os.path.splitext(filename)

        if ext == ".gifv":
            return ".mp4"

        if ext in {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".bmp",
            ".tiff",
            ".mp4",
            ".webm",
            ".mov",
            ".mkv",
        }:
            return ext

        if "v.redd.it" in domain:
            return ".mp4"

        if domain.endswith(("redgifs.com", "gfycat.com", "giphy.com", "streamable.com")):
            return ".mp4"

        if domain.endswith(("i.redd.it", "preview.redd.it", "external-preview.redd.it", "i.imgur.com")):
            return ".jpg"

        return ".jpg"
    except Exception:
        return ".jpg"


def _extract_redgifs_id(url: str) -> Optional[str]:
    """
    Extract the RedGifs id from common watch / iframe URLs.
    """
    if not url:
        return None

    cleaned = url.split("?")[0].split("#")[0].strip()
    match = re.search(r"/(?:watch|ifr)/([A-Za-z0-9]+)", cleaned, re.IGNORECASE)
    if match:
        return match.group(1)

    path = urlparse(cleaned).path.strip("/")
    if path:
        last = path.split("/")[-1]
        if last and last.lower() not in {"watch", "ifr"}:
            return last

    return None


def _extract_urls_from_text(text: str) -> List[str]:
    if not text:
        return []

    text = _normalize(text)
    urls = []
    for match in re.finditer(r"https?://[^\"'\s<>]+", text):
        url = match.group(0).rstrip(").,!?]}>\"'")
        if url:
            urls.append(url)
    return urls


def _fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    try:
        response = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        response.raise_for_status()
        return response.text or ""
    except Exception as exc:
        _trace(f"HTML fetch failed for {url}: {exc}")
        return ""


def _html_has_audio_hint(html_text: str) -> Optional[bool]:
    if not html_text:
        return None

    if re.search(r'"has[_-]?audio"\s*:\s*true', html_text, re.IGNORECASE):
        return True
    if re.search(r'"has[_-]?audio"\s*:\s*false', html_text, re.IGNORECASE):
        return False
    return None


def _extract_redgifs_candidates_from_html(html_text: str) -> Dict[str, List[str]]:
    pages: List[str] = []
    manifests: List[str] = []
    directs: List[str] = []

    if not html_text:
        return {"pages": pages, "manifests": manifests, "directs": directs}

    patterns = [
        r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)["\']',
        r'"web_url"\s*:\s*"([^"]+)"',
        r'"embed_url"\s*:\s*"([^"]+)"',
        r'"file_url"\s*:\s*"([^"]+)"',
        r'"hd"\s*:\s*"([^"]+)"',
        r'"sd"\s*:\s*"([^"]+)"',
        r'"dash_url"\s*:\s*"([^"]+)"',
        r'"hls_url"\s*:\s*"([^"]+)"',
        r'"contentUrl"\s*:\s*"([^"]+)"',
        r'"videoUrl"\s*:\s*"([^"]+)"',
        r'"video_url"\s*:\s*"([^"]+)"',
    ]

    raw_urls: List[str] = []
    for pattern in patterns:
        raw_urls.extend(re.findall(pattern, html_text, flags=re.IGNORECASE | re.DOTALL))

    raw_urls.extend(_extract_urls_from_text(html_text))

    for url in raw_urls:
        cleaned = _normalize(url)
        if not cleaned:
            continue
        lower = cleaned.lower()

        if any(token in lower for token in ("/watch/", "/ifr/")):
            pages.append(cleaned)
        elif any(token in lower for token in (".m3u8", ".mpd")):
            manifests.append(cleaned)
        elif any(token in lower for token in (".mp4", ".webm", ".mov", ".mkv")):
            directs.append(cleaned)
        elif any(host in lower for host in REDGIFS_HOSTS):
            pages.append(cleaned)

        if "-silent.mp4" in lower:
            directs.append(cleaned.replace("-silent.mp4", ".mp4"))
        if "-silent" in lower and lower.endswith(".mp4"):
            directs.append(re.sub(r"-silent(?=\.mp4$)", "", cleaned, flags=re.IGNORECASE))

    return {
        "pages": list(dict.fromkeys(pages)),
        "manifests": list(dict.fromkeys(manifests)),
        "directs": list(dict.fromkeys(directs)),
    }


def _redgifs_page_candidates(url: str) -> Dict[str, Any]:
    """
    Build candidate URL buckets for RedGifs.
    """
    page_candidates: List[str] = []
    manifest_candidates: List[str] = []
    direct_candidates: List[str] = []
    has_audio: Optional[bool] = None

    clean_url = _normalize(url)
    gif_id = _extract_redgifs_id(clean_url)

    if clean_url:
        page_candidates.append(clean_url)

    if gif_id:
        page_candidates.extend(
            [
                f"https://www.redgifs.com/watch/{gif_id}",
                f"https://www.redgifs.com/ifr/{gif_id}",
            ]
        )

    for page_url in list(dict.fromkeys(page_candidates)):
        _trace(f"Fetching RedGifs page: {page_url}")
        html_text = _fetch_html(page_url)
        if not html_text:
            continue

        audio_hint = _html_has_audio_hint(html_text)
        if audio_hint is True:
            has_audio = True
        elif audio_hint is False and has_audio is None:
            has_audio = False

        parsed = _extract_redgifs_candidates_from_html(html_text)
        page_candidates.extend(parsed["pages"])
        manifest_candidates.extend(parsed["manifests"])
        direct_candidates.extend(parsed["directs"])

    if clean_url and "-silent.mp4" in clean_url.lower():
        direct_candidates.append(clean_url.replace("-silent.mp4", ".mp4"))
        direct_candidates.append(re.sub(r"-silent(?=\.mp4$)", "", clean_url, flags=re.IGNORECASE))

    return {
        "has_audio": has_audio,
        "pages": list(dict.fromkeys(page_candidates)),
        "manifests": list(dict.fromkeys(manifest_candidates)),
        "directs": list(dict.fromkeys(direct_candidates)),
    }


class MediaDownloadManager:
    """
    Central coordinator for media downloads.

    Reddit-hosted media goes through RedditMediaDownloader.
    RedGifs is resolved via page/manifest URLs first, then verified with ffprobe
    so we only accept a file as audio-bearing when it really contains audio.
    """

    def __init__(self):
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._url_lock = threading.Lock()
        self._downloaded_urls: Dict[str, str] = {}
        self._failed_urls: Dict[str, int] = {}
        self._reddit_downloader = RedditMediaDownloader()
        self._download_semaphore = threading.BoundedSemaphore(
            max(1, int(get_media_config().max_concurrent_downloads()))
        )

    def _should_skip_url(self, url: str) -> bool:
        return self._failed_urls.get(url, 0) >= 2

    def _record_failure(self, url: str) -> None:
        self._failed_urls[url] = self._failed_urls.get(url, 0) + 1

    def _download_with_requests(self, url: str, save_path: str) -> Optional[str]:
        response = None
        try:
            response = requests.get(
                url,
                stream=True,
                timeout=(10, int(get_media_config().download_timeout())),
                headers={"User-Agent": "Reddit Stash Media Downloader/1.0"},
            )
            response.raise_for_status()
            limit = int(media_size_limit(url, response.headers.get("Content-Type", "")))
            stream_response_to_file(response, save_path, limit)
            return save_path
        except Exception as exc:
            self._logger.warning("Media download rejected/failed for %s: %s", url, exc)
            return None
        finally:
            if response is not None:
                response.close()

    def _download_with_ytdlp(self, url: str, save_path: str) -> Optional[str]:
        if not YT_DLP_AVAILABLE:
            return None
        output_dir = os.path.dirname(save_path) or "."
        base_name = os.path.splitext(os.path.basename(save_path))[0]
        outtmpl = os.path.join(output_dir, f"{base_name}.%(ext)s")
        max_video_size = int(get_media_config().max_video_size())
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
                    return save_path  # existing file: not charged again
                return None

            candidates.sort(key=lambda path: os.path.getmtime(path), reverse=True)
            final_path = candidates[0]
            was_downloaded_now = file_signature(final_path) != before_signatures.get(final_path)
            size = os.path.getsize(final_path)
            if size <= 0:
                return None
            if was_downloaded_now:
                size = register_downloaded_file(final_path, max_video_size)

            # Preserve an existing requested file. Otherwise publish the new
            # output at the requested path; a move failure leaves a valid file.
            if final_path != save_path and not os.path.exists(save_path):
                try:
                    os.replace(final_path, save_path)
                    final_path = save_path
                except OSError:
                    self._logger.warning("Could not move yt-dlp output to %s; retaining %s", save_path, final_path)
            return final_path
        except MediaLimitExceeded as exc:
            self._logger.warning("yt-dlp output rejected for %s: %s", url, exc)
            return None
        except Exception as exc:
            self._logger.debug("yt-dlp download failed for %s: %s", url, exc)
            return None

    def _has_audio_stream(self, file_path: str) -> bool:
        """
        Best-effort check for an audio stream in a downloaded media file.
        """
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

    def _download_redgifs(self, url: str, save_path: str) -> Optional[str]:
        """
        Try RedGifs page URLs first, then manifests, then direct media.

        The goal is to keep only candidates that really have an audio stream.
        """
        info = _redgifs_page_candidates(url)
        has_audio = info.get("has_audio", None)

        _trace(f"Input URL: {url}")
        _trace(f"Has audio hint: {has_audio}")
        _trace(f"Page candidates: {info['pages']}")
        _trace(f"Manifest candidates: {info['manifests']}")
        _trace(f"Direct candidates: {info['directs']}")

        tried: set[str] = set()
        silent_candidate: Optional[str] = None

        def _try_candidate(candidate: str, source_kind: str) -> Optional[str]:
            if not candidate or candidate in tried:
                return None

            tried.add(candidate)
            _trace(f"Trying {source_kind} candidate: {candidate}")

            local_path = self._download_with_ytdlp(candidate, save_path)
            if not local_path:
                _trace(f"{source_kind} candidate failed: {candidate}")
                return None

            audio = self._has_audio_stream(local_path)
            _trace(f"Downloaded {local_path}; audio={audio}")

            if audio:
                _trace(f"Accepted audio-bearing file from: {candidate}")
                return local_path

            return local_path

        for candidate in info["pages"]:
            local_path = _try_candidate(candidate, "page")
            if local_path:
                if self._has_audio_stream(local_path):
                    return local_path
                if silent_candidate is None:
                    silent_candidate = local_path

        for candidate in info["manifests"]:
            local_path = _try_candidate(candidate, "manifest")
            if local_path:
                if self._has_audio_stream(local_path):
                    return local_path
                if silent_candidate is None:
                    silent_candidate = local_path

        for candidate in info["directs"]:
            local_path = _try_candidate(candidate, "direct")
            if local_path:
                if self._has_audio_stream(local_path):
                    return local_path
                if silent_candidate is None:
                    silent_candidate = local_path

        if silent_candidate:
            if has_audio:
                _trace(
                    "No audio-bearing RedGifs file was found even though the clip appears to have audio."
                )
            else:
                _trace("Saving silent RedGifs fallback because no audio-bearing source was found.")
            return silent_candidate

        return None

    def _ensure_requested_path(self, cached_path: str, save_path: str) -> Optional[str]:
        """
        Ensure the caller receives the exact filename it asked for.

        If the same URL is reused for a different gallery item, copy the cached
        file into the requested path so gallery naming stays consistent.
        """
        try:
            if not cached_path or not os.path.exists(cached_path) or os.path.getsize(cached_path) <= 0:
                return None

            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)

            if os.path.abspath(cached_path) == os.path.abspath(save_path):
                return cached_path

            if os.path.exists(save_path):
                if os.path.getsize(save_path) > 0:
                    return save_path
                try:
                    os.remove(save_path)
                except Exception:
                    pass

            shutil.copy2(cached_path, save_path)
            return save_path
        except Exception as exc:
            self._logger.debug(
                f"Failed to mirror cached file {cached_path} to {save_path}: {exc}"
            )
            return None

    def download_media(self, url: str, save_path: str) -> Optional[str]:
        """Bound concurrent network transfers across all media providers."""
        with self._download_semaphore:
            return self._download_media_unlocked(url, save_path)

    def _download_media_unlocked(self, url: str, save_path: str) -> Optional[str]:
        """
        Download media and return the saved file path, or None on failure.
        """
        if not url or not save_path:
            return None

        with self._url_lock:
            if self._should_skip_url(url):
                return None

            if url in self._downloaded_urls:
                cached = self._downloaded_urls[url]
                if os.path.exists(cached) and os.path.getsize(cached) > 0:
                    materialized = self._ensure_requested_path(cached, save_path)
                    if materialized:
                        return materialized
                self._downloaded_urls.pop(url, None)

        try:
            if _is_reddit_media(url):
                result = self._reddit_downloader.download(url, save_path)
                if getattr(result, "is_success", False) and getattr(result, "local_path", None):
                    local_path = result.local_path
                    with self._url_lock:
                        self._downloaded_urls[url] = local_path
                    return local_path

                self._record_failure(url)
                return None

            if _is_redgifs(url):
                local_path = self._download_redgifs(url, save_path)
                if local_path:
                    with self._url_lock:
                        self._downloaded_urls[url] = local_path
                    return local_path

                self._record_failure(url)
                return None

            if _is_video_page(url):
                local_path = self._download_with_ytdlp(url, save_path)
                if local_path:
                    with self._url_lock:
                        self._downloaded_urls[url] = local_path
                    return local_path

                local_path = self._download_with_requests(url, save_path)
                if local_path:
                    with self._url_lock:
                        self._downloaded_urls[url] = local_path
                    return local_path

                self._record_failure(url)
                return None

            if _is_direct_image(url):
                local_path = self._download_with_requests(url, save_path)
                if local_path:
                    with self._url_lock:
                        self._downloaded_urls[url] = local_path
                    return local_path

                self._record_failure(url)
                return None

            local_path = self._download_with_requests(url, save_path)
            if local_path:
                with self._url_lock:
                    self._downloaded_urls[url] = local_path
                return local_path

            self._record_failure(url)
            return None

        except Exception as exc:
            self._logger.error(f"Exception during media download from {url}: {exc}")
            self._record_failure(url)
            return None

    def get_service_health(self) -> Dict[str, Any]:
        return {
            "reddit_downloader": self._reddit_downloader is not None,
            "yt_dlp_available": YT_DLP_AVAILABLE,
            "cached_urls": len(self._downloaded_urls),
            "failed_urls": len(self._failed_urls),
        }

    def is_service_available(self, service_name: str) -> bool:
        if service_name == "reddit":
            return self._reddit_downloader is not None
        if service_name == "yt_dlp":
            return YT_DLP_AVAILABLE
        return True

    def reset_service(self, service_name: str) -> None:
        if service_name == "reddit":
            try:
                self._reddit_downloader = RedditMediaDownloader()
            except Exception:
                self._reddit_downloader = None

    def process_pending_retries(self, max_retries: int = 50) -> Dict[str, int]:
        return {"processed": 0, "successful": 0, "failed": 0, "skipped": 0}


_media_manager: Optional[MediaDownloadManager] = None


def get_media_manager() -> MediaDownloadManager:
    global _media_manager
    if _media_manager is None:
        _media_manager = MediaDownloadManager()
    return _media_manager


def download_media_file(url: str, save_directory: str, file_id: str) -> Optional[str]:
    """
    Convenience wrapper used by save_utils.py.
    """
    if not url or not save_directory or not file_id:
        return None

    try:
        os.makedirs(save_directory, exist_ok=True)

        extension = _infer_media_extension(url)
        filename = f"{file_id}{extension}"
        save_path = os.path.join(save_directory, filename)

        manager = get_media_manager()
        return manager.download_media(url, save_path)
    except Exception as exc:
        logging.error(f"Error in download_media_file for {url}: {exc}")
        return None
