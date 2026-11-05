import os
import json
import logging
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, TextIO
from collections.abc import Iterable
from urllib.parse import parse_qs, urlparse
from praw.models import Submission, Comment
from utils.praw_helpers import lazy_load_comments
from utils.feature_flags import MediaFeatureConfig, get_media_config
from utils.media_services.download_manager import download_media_into
from utils.media_services.media_types import DownloadResult, GalleryImage
from utils.media_services.reddit_media import RedditMediaDownloader
from utils.constants import COMMENT_IMAGE_EXTENSIONS, IMAGE_URL_EXTENSIONS
from utils.domain_matching import domain_matches

logger = logging.getLogger(__name__)

# How media lines written by this module start, so a later retry can repoint them.
_MEDIA_LINE_PREFIXES = ("![", "**Video:** [")

_YOUTUBE_ID = re.compile(r'[A-Za-z0-9_-]+')
_YOUTUBE_ID_PATHS = ('shorts', 'embed', 'live', 'v')  # youtube.com/<these>/<id>


def relink_media_line(url: str, local_path: str) -> bool:
    """Point the owning post's media line at the now-downloaded file.

    Returns whether a line now points at the file, so the retry pass can report
    recovered media whose markdown keeps the remote link.

    Only the markdown named after the media's item id is touched (media file
    "<id>.jpg" or "<id>_<gallery_item>.jpg" belongs to "<CATEGORY>_<id>.md"), and
    only lines this module writes for media, so quoted text keeps its links.
    shortcut: transformed URLs (e.g. v.redd.it DASH), images in a parent comment
    and posts cleared by an earlier --cycle do not match and keep the remote link;
    the media file is still saved next to the post. Upgrade by storing the owning
    markdown in the queue metadata and fetching it when it is not local.
    """
    directory = os.path.dirname(local_path)
    item_id = os.path.splitext(os.path.basename(local_path))[0].split("_")[0]
    remote_link = f"]({url})"
    try:
        names = [n for n in os.listdir(directory) if n.endswith(f"_{item_id}.md")]
    except OSError:
        return False
    linked = False
    for name in names:
        md_path = os.path.join(directory, name)
        try:
            with open(md_path, encoding="utf-8") as f:
                lines = f.readlines()
            changed = False
            for i, line in enumerate(lines):
                if remote_link in line and line.lstrip("> ").startswith(_MEDIA_LINE_PREFIXES):
                    lines[i] = line.replace(remote_link, f"]({local_path})")
                    changed = True
            if changed:
                # Write a sibling temp file and swap it in, so a failed write never empties the post.
                fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".md.tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.writelines(lines)
                    shutil.copymode(md_path, tmp_path)  # mkstemp creates 0600
                    os.replace(tmp_path, md_path)
                    linked = True
                except BaseException:
                    os.unlink(tmp_path)
                    raise
        except OSError as e:
            logger.warning(f"Could not relink {md_path}: {e}")
    return linked


def format_date(timestamp: float) -> str:
    """Format a UTC timestamp into a human-readable date."""
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def yaml_frontmatter(fields: dict[str, Any]) -> str:
    """Render a YAML header with JSON-encoded scalar values."""
    lines = ''.join(f'{key}: {json.dumps(value, ensure_ascii=False)}\n' for key, value in fields.items())
    return f'---\n{lines}---\n\n'


def extract_video_id(url: str | None) -> str | None:
    """Return the video ID of a YouTube URL, or None when it names no video."""
    try:
        parsed = urlparse(url or '')
    except ValueError:
        return None
    segments = [segment for segment in parsed.path.split('/') if segment]
    video_id = None
    if domain_matches(parsed.hostname, 'youtu.be'):
        video_id = segments[0] if segments else None
    elif domain_matches(parsed.hostname, 'youtube.com'):
        if len(segments) > 1 and segments[0] in _YOUTUBE_ID_PATHS:
            video_id = segments[1]
        else:
            video_id = parse_qs(parsed.query).get('v', [None])[0]
    return video_id if video_id and _YOUTUBE_ID.fullmatch(video_id) else None


def _is_image_url(url: str | None) -> bool:
    """Check if a URL points to a downloadable image."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path.lower()

        if path.endswith(IMAGE_URL_EXTENSIONS):
            return True

        # Known image hosting domains (even without extension)
        image_domains = ['i.redd.it', 'i.imgur.com', 'preview.redd.it', 'external-preview.redd.it']
        return any(domain_matches(domain, image_domain) for image_domain in image_domains)
    except ValueError:
        return False


def _is_video_url(url: str | None) -> bool:
    """Check if a URL points to a Reddit video."""
    if not url:
        return False
    try:
        return domain_matches(urlparse(url).hostname, 'v.redd.it')
    except ValueError:
        return False


def _get_video_download_url(submission: Submission) -> str:
    """Extract the actual video stream URL from a PRAW submission.

    Reddit stores the short redirect URL in submission.url (e.g., https://v.redd.it/abc123)
    but the actual downloadable video stream is in submission.media['reddit_video']['fallback_url'].
    Since Reddit's 2024 HTTPS migration, some videos only have metadata in secure_media.
    Falls back to constructing a DASH URL from the short v.redd.it URL if metadata is unavailable.
    """
    try:
        for media_attr in ('media', 'secure_media'):
            media = getattr(submission, media_attr, None)
            if media:
                reddit_video = media.get('reddit_video', {})
                fallback_url = reddit_video.get('fallback_url')
                if fallback_url:
                    logger.debug(f"Found fallback_url via submission.{media_attr} for {submission.id}")
                    return fallback_url
        logger.debug(f"No media/secure_media metadata for video post {submission.id}, constructing DASH URL")
    except Exception as e:
        logger.warning(f"Error extracting fallback_url for {submission.id}: {e}")

    # Construct DASH URL from short v.redd.it URL as last resort
    parsed = urlparse(submission.url)
    if domain_matches(parsed.hostname, 'v.redd.it'):
        video_id = parsed.path.strip('/')
        if video_id:
            dash_url = f"https://v.redd.it/{video_id}/DASH_720.mp4"
            logger.debug(f"Using constructed DASH URL: {dash_url}")
            return dash_url

    return submission.url


def _download_gallery(gallery_images: list[GalleryImage], submission: Submission, save_dir: str,
                      max_workers: int, context_mode: bool) -> dict[int, DownloadResult]:
    """Download gallery images and return each result by 1-based image number."""
    def _download_gallery_item(image: GalleryImage) -> DownloadResult:
        return download_media_into(image.url, save_dir, f"{submission.id}_{image.gallery_id}")

    results = {}
    if context_mode:
        # In context mode, download sequentially and stop on first failure
        for idx, image in enumerate(gallery_images, 1):
            results[idx] = _download_gallery_item(image)
            if not results[idx].local_path:
                logger.info(f"Context mode: gallery image {idx} failed, skipping rest")
                break
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_download_gallery_item, image): idx
                for idx, image in enumerate(gallery_images, 1)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
    return results


def _save_gallery(submission: Submission, f: TextIO, media_config: MediaFeatureConfig, save_dir: str,
                  context_mode: bool) -> int:
    """Write a gallery post, with downloaded images where possible and URL links otherwise; returns media bytes."""
    gallery_images = RedditMediaDownloader.extract_media_urls_from_submission(submission)

    if not gallery_images:
        f.write(f"**Gallery post** (images unavailable): [View on Reddit](https://reddit.com{submission.permalink})\n")
        return 0

    f.write(f"**Gallery ({len(gallery_images)} images)**\n\n")
    max_workers = media_config.max_concurrent_downloads()
    results = _download_gallery(gallery_images, submission, save_dir, max_workers, context_mode)

    media_size = 0
    # Images context mode never reached have no result, so they get a URL link
    for idx, image in enumerate(gallery_images, 1):
        result = results.get(idx)
        if result and result.local_path:
            f.write(f"![Gallery Image {idx}]({result.local_path})\n")
            media_size += result.bytes_downloaded
        else:
            f.write(f"![Gallery Image {idx}]({image.url})\n")
        f.write(f"*Image {idx} of {len(gallery_images)}*\n\n")

    f.write(f"**Original Gallery URL:** [Link](https://reddit.com{submission.permalink})\n")
    return media_size


def _save_video(submission: Submission, f: TextIO, media_config: MediaFeatureConfig, save_dir: str) -> int:
    """Write a v.redd.it post, downloading the video when enabled; returns media bytes."""
    if not media_config.is_videos_enabled():
        f.write(f"**Video (download disabled):** [Link]({submission.url})\n")
        return 0
    result = download_media_into(_get_video_download_url(submission), save_dir, submission.id)
    if result.local_path:
        f.write(f"**Video:** [{os.path.basename(result.local_path)}]({result.local_path})\n")
        f.write(f"**Original Video URL:** [Link]({submission.url})\n")
        return result.bytes_downloaded
    f.write(f"**Video:** [Link]({submission.url})\n")
    return 0


def _save_image(submission: Submission, f: TextIO, media_config: MediaFeatureConfig, save_dir: str) -> int:
    """Write an image post, downloading the image when enabled; returns media bytes."""
    result = download_media_into(submission.url, save_dir, submission.id) if media_config.is_images_enabled() else None
    if result and result.local_path:
        f.write(f"![Image]({result.local_path})\n")
        f.write(f"**Original Image URL:** [Link]({submission.url})\n")
        return result.bytes_downloaded
    f.write(f"![Image]({submission.url})\n")
    return 0


def _save_submission_media(submission: Submission, f: TextIO, media_config: MediaFeatureConfig, save_dir: str,
                           context_mode: bool) -> int:
    """Handle media detection and download for a submission's link post; returns media bytes."""
    if getattr(submission, 'is_gallery', False) and media_config.is_albums_enabled():
        return _save_gallery(submission, f, media_config, save_dir, context_mode)
    if _is_video_url(submission.url):
        return _save_video(submission, f, media_config, save_dir)
    # Broad detection: i.redd.it, i.imgur.com, preview.redd.it, .webp, etc.
    if _is_image_url(submission.url):
        return _save_image(submission, f, media_config, save_dir)
    if video_id := extract_video_id(submission.url):
        f.write(f"[![Video](https://img.youtube.com/vi/{video_id}/0.jpg)]({submission.url})")
    else:
        f.write(submission.url if submission.url else '[Deleted Post]')
    return 0


def _write_link_post_body(submission: Submission, f: TextIO, context_mode: bool) -> int:
    """Write a link post's optional text and its media; returns media bytes."""
    # Link posts can also have body text (selftext)
    if hasattr(submission, 'selftext') and submission.selftext:
        f.write(submission.selftext)
        f.write('\n\n---\n\n')

    media_config = get_media_config()
    save_dir = os.path.dirname(f.name)

    # In context_mode (comment parent), a media error such as bad gallery metadata falls back to a URL link
    try:
        return _save_submission_media(submission, f, media_config, save_dir, context_mode)
    except Exception as media_err:
        if not context_mode:
            raise
        logger.debug(f"Context mode media fallback for {submission.id}: {media_err}")
        f.write(f"**Media:** [Link]({submission.url})\n")
        return 0


def save_submission(submission: Submission, f: TextIO, context_mode: bool = False) -> int:
    """Save a submission and its metadata; returns media bytes downloaded.

    Args:
        submission: PRAW Submission object
        f: File object to write to
        context_mode: When True (comment context), a gallery stops at its first failed
            image and media errors fall back to URL links. Failed downloads are still
            queued for a later run, as in normal mode.
    """
    try:
        title = submission.title
        frontmatter = {
            'id': submission.id,
            'title': title,
            'subreddit': f'/r/{submission.subreddit.display_name}',
            'timestamp': format_date(submission.created_utc),
            'author': f'/u/{submission.author.name if submission.author else "[deleted]"}',
        }

        if submission.link_flair_text:  # Check if flair exists and is not None
            frontmatter['flair'] = submission.link_flair_text

        frontmatter['comments'] = submission.num_comments
        frontmatter['permalink'] = f'https://reddit.com{submission.permalink}'

        f.write(yaml_frontmatter(frontmatter))
        f.write(f'# {title}\n\n')

        f.write(f'**Upvotes:** {submission.score} | **Permalink:** [Link](https://reddit.com{submission.permalink})\n\n')

        media_size = 0
        if submission.is_self:
            f.write(submission.selftext if submission.selftext else '[Deleted Post]')
        else:
            media_size = _write_link_post_body(submission, f, context_mode)

        f.write('\n\n## Comments:\n\n')
        lazy_comments = lazy_load_comments(submission)
        media_size += write_comments_markdown(lazy_comments, f)

        return media_size
    except Exception as e:
        logger.error(f"Error saving submission {submission.id}: {e}")
        raise

def _write_comment_parent_context(parent: Submission | Comment, f: TextIO) -> int:
    """Write what a saved comment replied to: its post or its parent comment; returns media bytes."""
    if isinstance(parent, Submission):
        f.write(f'## Context: Post by /u/{parent.author.name if parent.author else "[deleted]"}\n')
        f.write(f'- **Title:** {parent.title}\n')
        f.write(f'- **Upvotes:** {parent.score} | **Permalink:** [Link](https://reddit.com{parent.permalink})\n')
        if parent.is_self:
            f.write(f'{parent.selftext}\n\n')
        else:
            if hasattr(parent, 'selftext') and parent.selftext:
                f.write(f'{parent.selftext}\n\n')
            f.write(f'[Link to post content]({parent.url})\n\n')

        # Save the full submission context, including all comments
        f.write('\n\n## Full Post Context:\n\n')
        return save_submission(parent, f, context_mode=True)

    if isinstance(parent, Comment):
        f.write(f'## Context: Parent Comment by /u/{parent.author.name if parent.author else "[deleted]"}\n')
        f.write(f'- **Upvotes:** {parent.score} | **Permalink:** [Link](https://reddit.com{parent.permalink})\n')
        f.write(f'{parent.body}\n\n')

        # Recursively save the parent comment's context
        return save_comment_and_context(parent, f)

    return 0


def save_comment_and_context(comment: Comment, f: TextIO) -> int:
    """Save a comment, its context, and any child comments; returns media bytes downloaded.

    Args:
        comment: PRAW Comment object
        f: File object to write to
    """
    try:
        author = f'/u/{comment.author.name if comment.author else "[deleted]"}'
        body = comment.body
        title = f'Comment by {author}'
        frontmatter = {
            'id': comment.id,
            'title': title,
            'author': author,
            'body': body,
            'score': comment.score,
            'permalink': f'https://reddit.com{comment.permalink}',
        }

        f.write(yaml_frontmatter(frontmatter))
        f.write(f'# {title}\n\n')
        f.write(f'{body}\n\n')
        f.write(f'**Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n\n')

        media_size = _write_comment_parent_context(comment.parent(), f)

        if comment.replies:
            f.write('\n\n## Child Comments:\n\n')
            media_size += write_comments_markdown(comment.replies, f)

        return media_size
    except Exception as e:
        logger.error(f"Error saving comment {comment.id}: {e}")
        raise

def _trailing_image_url(comment_body: str) -> str | None:
    """Return the image URL a comment body ends with, if any."""
    if comment_body.endswith(COMMENT_IMAGE_EXTENSIONS):
        potential_url = comment_body.split()[-1]
        if potential_url.startswith(('http://', 'https://')):
            return potential_url
    return None


def _write_comment_body(comment: Comment, comment_body: str, f: TextIO, bq: str) -> int:
    """Write a comment body in blockquote prefix bq, downloading a trailing image; returns media bytes."""
    image_url = _trailing_image_url(comment_body)
    if not image_url:
        for line in comment_body.split('\n'):
            f.write(f'{bq}{line}\n')
        f.write('\n')
        return 0

    # Write any text before the image URL
    body_before_url = comment_body[:comment_body.rfind(image_url)].strip()
    if body_before_url:
        for line in body_before_url.split('\n'):
            f.write(f'{bq}{line}\n')
        f.write(f'{bq}\n')

    result = None
    if get_media_config().is_images_enabled():
        result = download_media_into(image_url, os.path.dirname(f.name), comment.id)
    if result and result.local_path:
        f.write(f'{bq}![Image]({result.local_path})\n')
        f.write(f'{bq}*Original Image URL: [Link]({image_url})*\n\n')
        return result.bytes_downloaded
    f.write(f'{bq}![Image]({image_url})\n\n')
    return 0


def write_comments_markdown(comments: Iterable[Any], f: TextIO, depth: int = 0) -> int:
    """Write comments to f as nested blockquotes, downloading images in their bodies; returns media bytes."""
    media_size = 0
    for comment in comments:
        if isinstance(comment, Comment):
            bq = "> " * depth

            author = comment.author.name if comment.author else "[deleted]"
            f.write(f'\n{bq}**Comment by /u/{author}**\n')
            f.write(f'{bq}*Upvotes: {comment.score} | [Permalink](https://reddit.com{comment.permalink})*\n\n')

            media_size += _write_comment_body(comment, comment.body if comment.body else '[deleted]', f, bq)

            # Recursively process child comments
            if comment.replies:
                media_size += write_comments_markdown(comment.replies, f, depth + 1)

            # Add separator only for top-level comments to reduce clutter
            if depth == 0:
                f.write('---\n')
    return media_size
