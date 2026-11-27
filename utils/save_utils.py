from __future__ import annotations

import logging
import os
import threading
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import requests
from praw.models import Comment, Submission
from utils.env_config import get_ignore_tls_errors
from utils.feature_flags import get_media_config
from utils.config_paths import get_storage_provider
from utils.media_services.reddit_media import RedditMediaDownloader
from utils.praw_helpers import RecoveredItem, create_recovery_metadata_markdown
from utils.time_utilities import lazy_load_comments
from .media_quota import media_size_limit, stream_response_to_file

logger = logging.getLogger(__name__)
# Thread-local storage for media size tracking
_media_size_local = threading.local()


def format_date(timestamp: float) -> str:
    """Format a UTC timestamp into a human-readable date."""
    return datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def extract_video_id(url: str) -> Optional[str]:
    """Extract the video ID from a YouTube URL."""
    if not url:
        return None
    if "youtube.com" in url:
        return url.split("v=")[-1]
    if "youtu.be" in url:
        return url.split("/")[-1]
    return None


def _active_storage_provider() -> str:
    """Return the currently selected storage provider."""
    try:
        provider = get_storage_provider()
        return (provider or "none").strip().lower()
    except Exception:
        return "none"

def _use_per_item_media_dirs() -> bool:
    """Dropbox gets per-item media folders; Mega keeps the legacy flat layout."""
    return _active_storage_provider() == "dropbox"


def _resolve_media_save_dir(path: str) -> str:
    """
    Resolve a media destination directory based on the active provider.

    Dropbox: store beside each markdown file in its own dedicated folder.
    Mega: keep the legacy flat structure under the parent subreddit directory.
    Examples:
      Dropbox: reddit/r_Python/POST_abc123.md -> reddit/r_Python/POST_abc123/
      Mega:    reddit_mega/r_Python/POST_abc123.md -> reddit_mega/r_Python/
    """
    if not path:
        return path

    if os.path.splitext(path)[1].lower() == ".md":
        media_dir = os.path.splitext(path)[0] if _use_per_item_media_dirs() else os.path.dirname(path)
    else:
        media_dir = path

    if not media_dir:
        media_dir = "."
    os.makedirs(media_dir, exist_ok=True)
    return media_dir


def _prefixed_media_id(base_id: str, sequence: Optional[int] = None) -> str:
    """Keep the original media ID unchanged."""
    return str(base_id).strip()


def _candidate_media_directories(save_directory: str) -> list[str]:
    """Return directories that may already contain a downloaded media file.
    The first candidate is the per-post/per-comment media directory.
    The second candidate is the legacy flat folder (the parent directory),
    which keeps existing archives from re-downloading after the layout change.
    """
    if not save_directory:
        return []

    normalized = os.path.normpath(save_directory)
    candidates = [normalized]

    legacy_parent = os.path.dirname(normalized)
    if legacy_parent and legacy_parent not in candidates:
        candidates.append(legacy_parent)
    # If the resolved path is already a file path, include its directory too.
    if os.path.isfile(normalized):
        directory = os.path.dirname(normalized)
        if directory and directory not in candidates:
            candidates.insert(0, directory)

    return candidates


def _media_url_extension(image_url: str) -> str:
    """Best-effort extension guess from a media URL."""
    if not image_url:
        return ""
    try:
        path = urlparse(image_url).path
    except Exception:
        path = image_url

    ext = os.path.splitext(path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".gifv", ".bmp", ".tiff"}:
        return ext
    return ""


def _find_existing_media_file(save_directory: str, media_id: str, image_url: str | None = None) -> str | None:
    """Return an already-downloaded media path if one exists.
    This prevents re-downloading when the archive layout changes from flat
    subreddit folders to per-post/per-comment folders.
    """
    if not save_directory or not media_id:
        return None

    media_id = str(media_id).strip()
    if not media_id:
        return None

    expected_ext = _media_url_extension(image_url or "")
    candidates = _candidate_media_directories(save_directory)
    for directory in candidates:
        if not directory or not os.path.isdir(directory):
            continue

        if expected_ext:
            exact = os.path.join(directory, f"{media_id}{expected_ext}")
            if os.path.isfile(exact):
                return exact
        try:
            for name in sorted(os.listdir(directory)):
                if name == "file_log.json":
                    continue
                if not name.startswith(media_id):
                    continue
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    return path
        except OSError:
            continue
    return None

def download_image(
    image_url: str,
    save_directory: str,
    submission_id: str,
    ignore_tls_errors: Optional[bool] = None,
):
    """
    Download media using the centralized media download manager.

    Returns: tuple[str | None, int]: (file_path, file_size)
    """
    try:
        from .media_download_manager import download_media_file

        save_directory = _resolve_media_save_dir(save_directory)
        existing_path = _find_existing_media_file(save_directory, submission_id, image_url)
        if existing_path:
            try:
                return existing_path, os.path.getsize(existing_path)
            except OSError:
                return existing_path, 0

        result_path = download_media_file(image_url, save_directory, submission_id)
        if result_path:
            try:
                file_size = os.path.getsize(result_path)
                return result_path, file_size
            except OSError:
                return result_path, 0

        return None, 0
    except Exception as e:
        logger.error(f"Failed to download image from {image_url}: {e}")
        return None, 0

def _download_image_fallback(
    image_url: str,
    save_directory: str,
    submission_id: str,
    ignore_tls_errors: Optional[bool] = None,
):
    """Compatibility fallback using the same per-file/daily limits as the manager."""
    response = None
    try:
        if ignore_tls_errors is None:
            ignore_tls_errors = get_ignore_tls_errors()
        save_directory = _resolve_media_save_dir(save_directory)
        existing_path = _find_existing_media_file(save_directory, submission_id, image_url)
        if existing_path:
            return existing_path, os.path.getsize(existing_path)

        request_kwargs = {
            "stream": True,
            "timeout": (5, 30),
            "headers": {"User-Agent": "Reddit Stash Media Downloader/1.0"},
        }
        if ignore_tls_errors:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            request_kwargs["verify"] = False
        response = requests.get(image_url, **request_kwargs)
        response.raise_for_status()

        extension = os.path.splitext(urlparse(image_url).path)[1].lower()
        if extension not in {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm",
            ".bmp", ".tiff", ".mov", ".mkv",
        }:
            extension = ".jpg"
        image_path = os.path.join(save_directory, f"{submission_id}{extension}")
        size = stream_response_to_file(
            response,
            image_path,
            media_size_limit(image_url, response.headers.get("Content-Type", "")),
        )
        return image_path, size
    except Exception as exc:
        logger.error("Fallback download failed for %s: %s", image_url, exc)
        return None, 0
    finally:
        if response is not None:
            response.close()


def _is_image_url(
    url: str) -> bool:
    """Check if a URL points to a downloadable image."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path.lower()

        image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff")
        if path.endswith(image_extensions):
            return True

        path_no_query = url.split("?")[0].lower()
        if any(path_no_query.endswith(ext) for ext in image_extensions):
            return True
        image_domains = [
            "i.redd.it",
            "i.imgur.com",
            "preview.redd.it",
            "external-preview.redd.it",
        ]
        if any(domain.endswith(d) for d in image_domains):
            return True

        return False
    except Exception:
        return False


def _is_video_url(url: str) -> bool:
    """Check if a URL points to a Reddit video."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        return "v.redd.it" in domain
    except Exception:
        return False


def _get_video_download_url(submission: Submission) -> str:
    """
    Extract the best Reddit video source URL from a PRAW submission.

    Reddit's fallback_url is a video-only MP4. Reddit's DASH/HLS manifests
    expose the separate video and audio streams that yt-dlp can select and
    merge with FFmpeg. Prefer those manifests; only fall back to the legacy
    fallback_url when a manifest cannot be obtained.
    """
    try:
        for media_attr in ("media", "secure_media"):
            media = getattr(submission, media_attr, None)
            if media:
                reddit_video = media.get("reddit_video", {}) or {}

                dash_url = reddit_video.get("dash_url")
                if dash_url:
                    logger.debug(
                        f"Found dash_url via submission.{media_attr} for {submission.id}"
                    )
                    return dash_url

                hls_url = reddit_video.get("hls_url")
                if hls_url:
                    logger.debug(
                        f"Found hls_url via submission.{media_attr} for {submission.id}"
                    )
                    return hls_url

                fallback_url = reddit_video.get("fallback_url")
                if fallback_url:
                    parsed = urlparse(fallback_url)
                    if "v.redd.it" in parsed.netloc.lower():
                        video_id = parsed.path.strip("/").split("/")[0]
                        if video_id:
                            dash_url = f"https://v.redd.it/{video_id}/DASHPlaylist.mpd"
                            logger.debug(
                                f"Constructed DASH manifest from fallback_url for "
                                f"{submission.id}: {dash_url}"
                            )
                            return dash_url

                    logger.debug(
                        f"Falling back to fallback_url via submission.{media_attr} "
                        f"for {submission.id}"
                    )
                    return fallback_url

        logger.info(
            f"No media/secure_media metadata for video post {submission.id}, "
            "constructing DASH URL"
        )
    except Exception as e:
        logger.warning(f"Error extracting Reddit video source for {submission.id}: {e}")

    try:
        parsed = urlparse(submission.url)
        if "v.redd.it" in parsed.netloc:
            video_id = parsed.path.strip("/").split("/")[0]
            if video_id:
                dash_url = f"https://v.redd.it/{video_id}/DASHPlaylist.mpd"
                logger.info(f"Using constructed DASH URL: {dash_url}")
                return dash_url
    except Exception:
        pass

    return submission.url


def _track_media_size(size: int) -> None:
    """Track accumulated media download sizes per-thread."""
    if not hasattr(_media_size_local, "size"):
        _media_size_local.size = 0
    _media_size_local.size += size


def _reset_media_tracker() -> None:
    """Reset the per-thread media size tracker to zero."""
    _media_size_local.size = 0


def _get_media_size() -> int:
    """Get the accumulated media size for the current thread."""
    return getattr(_media_size_local, "size", 0)

def _save_submission_media(
    submission: Submission,
    f,
    is_recovered: bool,
    media_config,
    save_dir: str,
    ignore_tls_errors: Optional[bool],
    context_mode: bool,
) -> None:
    """Handle media detection and download for a submission's link post."""
    save_dir = _resolve_media_save_dir(save_dir)
    # 1. Gallery posts
    if (
        not is_recovered
        and hasattr(submission, "is_gallery")
        and submission.is_gallery
        and media_config.is_albums_enabled()
        and media_config.is_images_enabled()
    ):
        media_urls = RedditMediaDownloader.extract_media_urls_from_submission(submission)
        gallery_images = [m for m in media_urls if m.get("source") == "reddit_gallery"]
        if gallery_images:
            f.write(f"**Gallery ({len(gallery_images)} images)**\n\n")
            max_workers = media_config.max_concurrent_downloads()
            def _download_gallery_item(args):
                idx, info = args
                gid = (
                    info.get("gallery_id")
                    or info.get("media_id")
                    or info.get("id")
                    or f"gallery_{idx}"
                )
                fid = _prefixed_media_id(f"{submission.id}_{gid}", idx)
                return idx, download_image(info["url"], save_dir, fid, ignore_tls_errors)
            results = {}
            # Download sequentially so on-disk creation order matches the post order.
            for i, m in enumerate(gallery_images, 1):
                idx, (path, size) = _download_gallery_item((i, m))
                results[idx] = (path, size)
                if context_mode and not path:
                    logger.info(
                        f"Context mode: gallery image {idx} failed, skipping rest"
                    )
                    break
            for idx in sorted(results):
                path, size = results[idx]
                gallery_url = gallery_images[idx - 1]["url"]
                if path:
                    f.write(f"![Gallery Image {idx}]({path})\n")
                    _track_media_size(size)
                else:
                    f.write(f"![Gallery Image {idx}]({gallery_url})\n")
                f.write(f"*Image {idx} of {len(gallery_images)}*\n\n")
            for idx in range(len(results) + 1, len(gallery_images) + 1):
                gallery_url = gallery_images[idx - 1]["url"]
                f.write(f"![Gallery Image {idx}]({gallery_url})\n")
                f.write(f"*Image {idx} of {len(gallery_images)}*\n\n")
            f.write(f"**Original Gallery URL:** [Link](https://reddit.com{submission.permalink})\n")
        else:
            f.write(
                f"**Gallery post** (images unavailable): "
                f"[View on Reddit](https://reddit.com{submission.permalink})\n"
            )
        return
    # 2. Reddit video (v.redd.it)
    if _is_video_url(submission.url):
        if media_config.is_videos_enabled():
            video_url = _get_video_download_url(submission)
            video_path, video_size = download_image(
                video_url,
                save_dir,
                submission.id,
                ignore_tls_errors,
            )
            if video_path:
                f.write(f"**Video:** [{os.path.basename(video_path)}]({video_path})\n")
                f.write(f"**Original Video URL:** [Link]({submission.url})\n")
                _track_media_size(video_size)
            else:
                f.write(f"**Video:** [Link]({submission.url})\n")
        else:
            f.write(f"**Video (download disabled):** [Link]({submission.url})\n")
        return
    # 3. Images
    if _is_image_url(submission.url):
        if media_config.is_images_enabled():
            image_path, image_size = download_image(
                submission.url,
                save_dir,
                submission.id,
                ignore_tls_errors,
            )
            if image_path:
                f.write(f"![Image]({image_path})\n")
                f.write(f"**Original Image URL:** [Link]({submission.url})\n")
                _track_media_size(image_size)
            else:
                f.write(f"![Image]({submission.url})\n")
        else:
            f.write(f"![Image]({submission.url})\n")
        return
    # 4. YouTube
    if "youtube.com" in submission.url or "youtu.be" in submission.url:
        video_id = extract_video_id(submission.url)
        f.write(f"[![Video](https://img.youtube.com/vi/{video_id}/0.jpg)]({submission.url})")
        return

    # 5. Everything else
    f.write(submission.url if submission.url else "[Deleted Post]")


def save_submission(
    submission: Submission,
    f,
    unsave: bool = False,
    ignore_tls_errors: Optional[bool] = None,
    recovery_metadata=None,
    context_mode: bool = False,
    include_media: bool = True,
) -> None:
    """Save a submission and its metadata."""
    if unsave:
        raise RuntimeError("Automatic unsaving is disabled until remote upload verification exists.")
    try:
        is_recovered = isinstance(submission, RecoveredItem)
        if recovery_metadata or is_recovered:
            if is_recovered and hasattr(submission, "recovery_result"):
                recovery_metadata = submission.recovery_result

        if recovery_metadata:
            recovery_banner = create_recovery_metadata_markdown(recovery_metadata)
            f.write(recovery_banner)
            f.write("---\n")
        f.write(f"id: {submission.id}\n")
        if is_recovered:
            recovered_data = submission.recovered_data if hasattr(submission, "recovered_data") else {}
            f.write(f'subreddit: {recovered_data.get("subreddit", "[unknown]")}\n')
            f.write(f'timestamp: {recovered_data.get("created_utc", "unknown")}\n')
            f.write(f'author: {recovered_data.get("author", "[deleted]")}\n')
            f.write("recovered: true\n")
        else:
            f.write(f"subreddit: /r/{submission.subreddit.display_name}\n")
            f.write(f"timestamp: {format_date(submission.created_utc)}\n")
            f.write(f"author: /u/{submission.author.name if submission.author else '[deleted]'}\n")
        if submission.link_flair_text:
            f.write(f"flair: {submission.link_flair_text}\n")
        if not is_recovered:
            f.write(f"comments: {submission.num_comments}\n")
        f.write(f"permalink: https://reddit.com{submission.permalink}\n")
        f.write("---\n\n")

        f.write(f"# {submission.title}\n\n")
        f.write(
            f"**Upvotes:** {submission.score} | "
            f"**Permalink:** [Link](https://reddit.com{submission.permalink})\n\n"
        )
        if submission.is_self:
            f.write(submission.selftext if submission.selftext else "[Deleted Post]")
        else:
            if hasattr(submission, "selftext") and submission.selftext:
                f.write(submission.selftext)
            f.write("\n\n---\n\n")

            media_config = get_media_config()

            save_dir = _resolve_media_save_dir(f.name)
            try:
                if include_media:
                    _save_submission_media(
                        submission,
                        f,
                        is_recovered,
                        media_config,
                        save_dir,
                        ignore_tls_errors,
                        context_mode,
                    )
                else:
                    f.write("**Media omitted in this context view.**\n")
            except Exception as media_err:
                logger.warning(
                    "Media download failed for submission %s; preserving Markdown: %s",
                    submission.id,
                    media_err,
                )
                f.write(
                    f"**Media download failed; original link:** "
                    f"[Link]({submission.url or '[unavailable]'})\n"
                )
        f.write("\n\n## Comments:\n\n")
        lazy_comments = lazy_load_comments(submission)
        process_comments(lazy_comments, f)

        if unsave:
            try:
                submission.unsave()
                logger.info(f"Unsaved submission: {submission.id}")
            except Exception as e:
                logger.warning(f"Failed to unsave submission {submission.id}: {e}")

    except Exception as e:
        logger.error(f"Error saving submission {submission.id}: {e}")
        raise


def save_comment_and_context(
    comment: Comment,
    f,
    unsave: bool = False,
    ignore_tls_errors: Optional[bool] = None,
    recovery_metadata=None,
) -> None:
    """Save a comment, its context, and any child comments."""
    if unsave:
        raise RuntimeError("Automatic unsaving is disabled until remote upload verification exists.")
    try:
        is_recovered = isinstance(comment, RecoveredItem)

        if recovery_metadata or is_recovered:
            if is_recovered and hasattr(comment, "recovery_result"):
                recovery_metadata = comment.recovery_result
        if recovery_metadata:
            recovery_banner = create_recovery_metadata_markdown(recovery_metadata)
            f.write(recovery_banner)
            f.write("---\n")
        if is_recovered:
            recovered_data = comment.recovered_data if hasattr(comment, "recovered_data") else {}
            f.write(f'Comment by {recovered_data.get("author", "[deleted]")}\n')
            f.write("- **Recovered:** true\n")
            f.write(f'{recovered_data.get("body", "[Content not available]")}\n\n')
        else:
            f.write(f'Comment by /u/{comment.author.name if comment.author else "[deleted]"}\n')
            f.write(f'- **Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n')
            f.write(f"{comment.body}\n\n")
        f.write("---\n\n")
        if not is_recovered:
            parent = comment.parent()
            if isinstance(parent, Submission):
                f.write(f'## Context: Post by /u/{parent.author.name if parent.author else "[deleted]"}\n')
                f.write(f"- **Title:** {parent.title}\n")
                f.write(
                    f"- **Upvotes:** {parent.score} | "
                    f"**Permalink:** [Link](https://reddit.com{parent.permalink})\n"
                )
                if parent.is_self:
                    f.write(f"{parent.selftext}\n\n")
                else:
                    if hasattr(parent, "selftext") and parent.selftext:
                        f.write(f"{parent.selftext}\n\n")
                    f.write(f"[Link to post content]({parent.url})\n\n")
                    f.write("\n\n## Full Post Context:\n\n")
                    save_submission(
                        parent,
                        f,
                        ignore_tls_errors=ignore_tls_errors,
                        context_mode=True,
                        include_media=False,
                    )
            elif isinstance(parent, Comment):
                f.write(f'## Context: Parent Comment by /u/{parent.author.name if parent.author else "[deleted]"}\n')
                f.write(
                    f"- **Upvotes:** {parent.score} | "
                    f"**Permalink:** [Link](https://reddit.com{parent.permalink})\n"
                )
                f.write(f"{parent.body}\n\n")
                save_comment_and_context(parent, f, ignore_tls_errors=ignore_tls_errors)
        if comment.replies:
            f.write("\n\n## Child Comments:\n\n")
            process_comments(comment.replies, f, ignore_tls_errors=ignore_tls_errors)

        if unsave:
            try:
                comment.unsave()
                logger.info(f"Unsaved comment: {comment.id}")
            except Exception as e:
                logger.warning(f"Failed to unsave comment {comment.id}: {e}")
    except Exception as e:
        logger.error(f"Error saving comment {comment.id}: {e}")
        raise


def process_comments(
    comments,
    f,
    depth: int = 0,
    simple_format: bool = False,
    ignore_tls_errors: Optional[bool] = None,
) -> None:
    """Process all comments using pure blockquote nesting for hierarchy."""
    for comment in comments:
        if not isinstance(comment, Comment):
            continue
        bq = "> " * depth if depth > 0 else ""
        author = comment.author.name if comment.author else "[deleted]"

        f.write(f"\n{bq}**Comment by /u/{author}**\n")
        f.write(f"{bq}*Upvotes: {comment.score} | [Permalink](https://reddit.com{comment.permalink})*\n\n")

        comment_body = comment.body if comment.body else "[deleted]"
        image_url = None
        if any(comment_body.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
            potential_url = comment_body.split()[-1]
            if potential_url.startswith(("http://", "https://")) and "." in potential_url:
                image_url = potential_url
        if image_url and get_media_config().is_images_enabled():
            body_before_url = comment_body[: comment_body.rfind(image_url)].strip()
            if body_before_url:
                for line in body_before_url.split("\n"):
                    f.write(f"{bq}{line}\n")
            f.write(f"{bq}\n")
            image_path, image_size = download_image(
                image_url,
                _resolve_media_save_dir(f.name),
                comment.id,
                ignore_tls_errors,
            )
            if image_path:
                f.write(f"{bq}![Image]({image_path})\n")
                f.write(f"{bq}*Original Image URL: [Link]({image_url})*\n\n")
                _track_media_size(image_size)
            else:
                f.write(f"{bq}![Image]({image_url})\n\n")
        else:
            lines = comment_body.split("\n")
            for line in lines:
                f.write(f"{bq}{line}\n")
            f.write("\n")
        if not simple_format and comment.replies:
            process_comments(comment.replies, f, depth + 1, simple_format, ignore_tls_errors)
