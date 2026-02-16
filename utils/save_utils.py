import os
import logging
import requests
import threading
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse
from praw.models import Submission, Comment
from utils.time_utilities import lazy_load_comments
from utils.env_config import get_ignore_tls_errors
from utils.praw_helpers import RecoveredItem, create_recovery_metadata_markdown
from utils.feature_flags import get_media_config
from utils.media_services.reddit_media import RedditMediaDownloader

logger = logging.getLogger(__name__)

# Thread-local storage for media size tracking (replaces function attribute hack)
_media_size_local = threading.local()

def format_date(timestamp):
    """Format a UTC timestamp into a human-readable date."""
    return datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

def extract_video_id(url):
    """Extract the video ID from a YouTube URL."""
    if "youtube.com" in url:
        return url.split("v=")[-1]
    elif "youtu.be" in url:
        return url.split("/")[-1]
    return None

def download_image(image_url, save_directory, submission_id, ignore_tls_errors=None):
    """Download an image from the given URL and save it locally using the sophisticated media download system.

    Returns:
        tuple: (file_path, file_size) if successful, (None, 0) if failed
    """
    try:
        # Import the new media download manager
        from .media_download_manager import download_media_file

        # Use the new media download system which includes:
        # - Proper Imgur API integration with rate limiting
        # - Reddit media handling (i.redd.it, v.redd.it)
        # - Circuit breaker protection and retry logic
        # - Service-specific error isolation
        result_path = download_media_file(image_url, save_directory, submission_id)

        if result_path:
            # Get file size for storage tracking
            try:
                file_size = os.path.getsize(result_path)
                return result_path, file_size
            except OSError:
                return result_path, 0
        else:
            # No fallback - respect rate limiting and service protection
            return None, 0

    except Exception as e:
        logger.error(f"Failed to download image from {image_url}: {e}")
        # No fallback - respect rate limiting and service protection
        return None, 0


def _download_image_fallback(image_url, save_directory, submission_id, ignore_tls_errors=None):
    """Fallback to the original download method for backward compatibility.

    Returns:
        tuple: (file_path, file_size) if successful, (None, 0) if failed
    """
    try:
        # Load ignore_tls_errors setting if not provided
        if ignore_tls_errors is None:
            ignore_tls_errors = get_ignore_tls_errors()

        # Configure request parameters
        request_kwargs = {}
        if ignore_tls_errors:
            # Disable SSL warnings if ignoring TLS errors
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            request_kwargs['verify'] = False

        response = requests.get(image_url, **request_kwargs)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)

        # Determine the image extension from the URL
        extension = os.path.splitext(image_url)[1]
        if extension.lower() not in ['.jpg', '.jpeg', '.png', '.gif']:
            extension = '.jpg'  # Default to .jpg if the extension is unusual

        # Save the image with a unique name
        image_filename = f"{submission_id}{extension}"
        image_path = os.path.join(save_directory, image_filename)

        with open(image_path, 'wb') as f:
            f.write(response.content)

        # Get file size for storage tracking
        try:
            file_size = os.path.getsize(image_path)
            return image_path, file_size
        except OSError:
            return image_path, 0

    except Exception as e:
        logger.error(f"Fallback download failed for {image_url}: {e}")
        return None, 0


def _is_image_url(url):
    """Check if a URL points to a downloadable image."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path.lower()

        # Direct image extensions (including .webp)
        image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff')
        if path.endswith(image_extensions):
            return True
        # Check extension before query params
        path_no_query = url.split('?')[0].lower()
        if any(path_no_query.endswith(ext) for ext in image_extensions):
            return True

        # Known image hosting domains (even without extension)
        image_domains = ['i.redd.it', 'i.imgur.com', 'preview.redd.it', 'external-preview.redd.it']
        if any(domain.endswith(d) for d in image_domains):
            return True

        return False
    except Exception:
        return False


def _is_video_url(url):
    """Check if a URL points to a Reddit video."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        return 'v.redd.it' in domain
    except Exception:
        return False


def _get_video_download_url(submission):
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
        logger.info(f"No media/secure_media metadata for video post {submission.id}, constructing DASH URL")
    except Exception as e:
        logger.warning(f"Error extracting fallback_url for {submission.id}: {e}")

    # Construct DASH URL from short v.redd.it URL as last resort
    try:
        parsed = urlparse(submission.url)
        if 'v.redd.it' in parsed.netloc:
            video_id = parsed.path.strip('/')
            if video_id:
                dash_url = f"https://v.redd.it/{video_id}/DASH_720.mp4"
                logger.info(f"Using constructed DASH URL: {dash_url}")
                return dash_url
    except Exception:
        pass

    return submission.url


def _track_media_size(size):
    """Track accumulated media download sizes per-thread."""
    if not hasattr(_media_size_local, 'size'):
        _media_size_local.size = 0
    _media_size_local.size += size


def _reset_media_tracker():
    """Reset the per-thread media size tracker to zero."""
    _media_size_local.size = 0


def _get_media_size():
    """Get the accumulated media size for the current thread."""
    return getattr(_media_size_local, 'size', 0)


def _save_submission_media(submission, f, is_recovered, media_config, save_dir, ignore_tls_errors, context_mode):
    """Handle media detection and download for a submission's link post.

    Extracted to allow context_mode wrapping with try-except in save_submission().
    """
    # --- 1. Gallery posts ---
    if (not is_recovered
            and hasattr(submission, 'is_gallery')
            and submission.is_gallery
            and media_config.is_albums_enabled()):

        media_urls = RedditMediaDownloader.extract_media_urls_from_submission(submission)
        gallery_images = [m for m in media_urls if m.get('source') == 'reddit_gallery']

        if gallery_images:
            f.write(f"**Gallery ({len(gallery_images)} images)**\n\n")
            max_workers = media_config.get_media_config().get('max_concurrent_downloads', 3)

            def _download_gallery_item(args):
                idx, info = args
                gid = info.get('gallery_id', f'gallery_{idx}')
                fid = f"{submission.id}_{gid}"
                return idx, download_image(info['url'], save_dir, fid, ignore_tls_errors)

            results = {}
            if context_mode:
                # In context mode, download sequentially and stop on first failure
                for i, m in enumerate(gallery_images, 1):
                    idx, (path, size) = _download_gallery_item((i, m))
                    results[idx] = (path, size)
                    if not path:
                        logger.info(f"Context mode: gallery image {idx} failed, skipping rest")
                        break
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(_download_gallery_item, (i, m)): i
                        for i, m in enumerate(gallery_images, 1)
                    }
                    for future in as_completed(futures):
                        idx, (path, size) = future.result()
                        results[idx] = (path, size)

            for idx in sorted(results):
                path, size = results[idx]
                gallery_url = gallery_images[idx - 1]['url']
                if path:
                    f.write(f"![Gallery Image {idx}]({path})\n")
                    _track_media_size(size)
                else:
                    f.write(f"![Gallery Image {idx}]({gallery_url})\n")
                f.write(f"*Image {idx} of {len(gallery_images)}*\n\n")

            # Write remaining undownloaded images as URL links
            for idx in range(len(results) + 1, len(gallery_images) + 1):
                gallery_url = gallery_images[idx - 1]['url']
                f.write(f"![Gallery Image {idx}]({gallery_url})\n")
                f.write(f"*Image {idx} of {len(gallery_images)}*\n\n")

            f.write(f"**Original Gallery URL:** [Link](https://reddit.com{submission.permalink})\n")
        else:
            f.write(f"**Gallery post** (images unavailable): [View on Reddit](https://reddit.com{submission.permalink})\n")

    # --- 2. Reddit video (v.redd.it) ---
    elif _is_video_url(submission.url):
        if media_config.is_videos_enabled():
            video_url = _get_video_download_url(submission)
            video_path, video_size = download_image(
                video_url, save_dir, submission.id, ignore_tls_errors
            )
            if video_path:
                f.write(f"**Video:** [{os.path.basename(video_path)}]({video_path})\n")
                f.write(f"**Original Video URL:** [Link]({submission.url})\n")
                _track_media_size(video_size)
            else:
                f.write(f"**Video:** [Link]({submission.url})\n")
        else:
            f.write(f"**Video (download disabled):** [Link]({submission.url})\n")

    # --- 3. Images (broad detection: i.redd.it, i.imgur.com, preview.redd.it, .webp, etc.) ---
    elif _is_image_url(submission.url):
        if media_config.is_images_enabled():
            image_path, image_size = download_image(
                submission.url, save_dir, submission.id, ignore_tls_errors
            )
            if image_path:
                f.write(f"![Image]({image_path})\n")
                f.write(f"**Original Image URL:** [Link]({submission.url})\n")
                _track_media_size(image_size)
            else:
                f.write(f"![Image]({submission.url})\n")
        else:
            f.write(f"![Image]({submission.url})\n")

    # --- 4. YouTube ---
    elif "youtube.com" in submission.url or "youtu.be" in submission.url:
        video_id = extract_video_id(submission.url)
        f.write(f"[![Video](https://img.youtube.com/vi/{video_id}/0.jpg)]({submission.url})")

    # --- 5. Everything else (plain URL) ---
    else:
        f.write(submission.url if submission.url else '[Deleted Post]')


def save_submission(submission, f, unsave=False, ignore_tls_errors=None, recovery_metadata=None, context_mode=False):
    """Save a submission and its metadata, optionally unsaving it after.

    Args:
        submission: PRAW Submission object or RecoveredItem
        f: File object to write to
        unsave: Whether to unsave the submission after saving
        ignore_tls_errors: Whether to ignore TLS errors during downloads
        recovery_metadata: RecoveryResult object if this is recovered content
        context_mode: When True (comment context), media downloads fail fast
            and fall back to URL links instead of retrying
    """
    try:
        # Check if this is a recovered item
        is_recovered = isinstance(submission, RecoveredItem)

        # If recovery_metadata is provided or item is recovered, add recovery banner
        if recovery_metadata or is_recovered:
            if is_recovered and hasattr(submission, 'recovery_result'):
                recovery_metadata = submission.recovery_result

            if recovery_metadata:
                recovery_banner = create_recovery_metadata_markdown(recovery_metadata)
                f.write(recovery_banner)

        f.write('---\n')  # Start of frontmatter
        f.write(f'id: {submission.id}\n')

        # Handle recovered items differently
        if is_recovered:
            # Extract data from recovery result
            recovered_data = submission.recovered_data if hasattr(submission, 'recovered_data') else {}
            f.write(f'subreddit: {recovered_data.get("subreddit", "[unknown]")}\n')
            f.write(f'timestamp: {recovered_data.get("created_utc", "unknown")}\n')
            f.write(f'author: {recovered_data.get("author", "[deleted]")}\n')
            f.write('recovered: true\n')
        else:
            # Normal PRAW object
            f.write(f'subreddit: /r/{submission.subreddit.display_name}\n')
            f.write(f'timestamp: {format_date(submission.created_utc)}\n')
            f.write(f'author: /u/{submission.author.name if submission.author else "[deleted]"}\n')

        if not is_recovered and submission.link_flair_text:  # Check if flair exists and is not None
            f.write(f'flair: {submission.link_flair_text}\n')

        if not is_recovered:
            f.write(f'comments: {submission.num_comments}\n')
            f.write(f'permalink: https://reddit.com{submission.permalink}\n')

        f.write('---\n\n')  # End of frontmatter
        f.write(f'# {submission.title}\n\n')
        f.write(f'**Upvotes:** {submission.score} | **Permalink:** [Link](https://reddit.com{submission.permalink})\n\n')

        if submission.is_self:
            f.write(submission.selftext if submission.selftext else '[Deleted Post]')
        else:
            # Link posts can also have body text (selftext)
            if hasattr(submission, 'selftext') and submission.selftext:
                f.write(submission.selftext)
                f.write('\n\n---\n\n')

            media_config = get_media_config()
            save_dir = os.path.dirname(f.name)

            # In context_mode (comment parent), wrap media in try-except
            # to fail fast and fall back to URL links
            try:
                _save_submission_media(submission, f, is_recovered, media_config,
                                      save_dir, ignore_tls_errors, context_mode)
            except Exception as media_err:
                if context_mode:
                    logger.info(f"Context mode media fallback for {submission.id}: {media_err}")
                    f.write(f"**Media:** [Link]({submission.url})\n")
                else:
                    raise

        f.write('\n\n## Comments:\n\n')
        lazy_comments = lazy_load_comments(submission)
        process_comments(lazy_comments, f)

        # Unsave the submission if requested
        if unsave:
            try:
                submission.unsave()
                logger.info(f"Unsaved submission: {submission.id}")
            except Exception as e:
                logger.warning(f"Failed to unsave submission {submission.id}: {e}")

    except Exception as e:
        logger.error(f"Error saving submission {submission.id}: {e}")
        raise

def save_comment_and_context(comment, f, unsave=False, ignore_tls_errors=None, recovery_metadata=None):
    """Save a comment, its context, and any child comments.

    Args:
        comment: PRAW Comment object or RecoveredItem
        f: File object to write to
        unsave: Whether to unsave the comment after saving
        ignore_tls_errors: Whether to ignore TLS errors during downloads
        recovery_metadata: RecoveryResult object if this is recovered content
    """
    try:
        # Check if this is a recovered item
        is_recovered = isinstance(comment, RecoveredItem)

        # If recovery_metadata is provided or item is recovered, add recovery banner
        if recovery_metadata or is_recovered:
            if is_recovered and hasattr(comment, 'recovery_result'):
                recovery_metadata = comment.recovery_result

            if recovery_metadata:
                recovery_banner = create_recovery_metadata_markdown(recovery_metadata)
                f.write(recovery_banner)

        # Save the comment itself
        f.write('---\n')  # Start of frontmatter

        if is_recovered:
            # Handle recovered comment
            recovered_data = comment.recovered_data if hasattr(comment, 'recovered_data') else {}
            f.write(f'Comment by {recovered_data.get("author", "[deleted]")}\n')
            f.write('- **Recovered:** true\n')
            f.write(f'{recovered_data.get("body", "[Content not available]")}\n\n')
        else:
            # Normal PRAW comment
            f.write(f'Comment by /u/{comment.author.name if comment.author else "[deleted]"}\n')
            f.write(f'- **Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n')
            f.write(f'{comment.body}\n\n')

        f.write('---\n\n')  # End of frontmatter

        # Save the parent context (skip for recovered items as we don't have parent info)
        if not is_recovered:
            parent = comment.parent()
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
                save_submission(parent, f, ignore_tls_errors=ignore_tls_errors, context_mode=True)

            elif isinstance(parent, Comment):
                f.write(f'## Context: Parent Comment by /u/{parent.author.name if parent.author else "[deleted]"}\n')
                f.write(f'- **Upvotes:** {parent.score} | **Permalink:** [Link](https://reddit.com{parent.permalink})\n')
                f.write(f'{parent.body}\n\n')

                # Recursively save the parent comment's context
                save_comment_and_context(parent, f, ignore_tls_errors=ignore_tls_errors)

            # Save child comments if any exist
            if comment.replies:
                f.write('\n\n## Child Comments:\n\n')
                process_comments(comment.replies, f, ignore_tls_errors=ignore_tls_errors)

        # Unsave the comment if requested
        if unsave:
            try:
                comment.unsave()
                logger.info(f"Unsaved comment: {comment.id}")
            except Exception as e:
                logger.warning(f"Failed to unsave comment {comment.id}: {e}")

    except Exception as e:
        logger.error(f"Error saving comment {comment.id}: {e}")
        raise

def process_comments(comments, f, depth=0, simple_format=False, ignore_tls_errors=None):
    """Process all comments using pure blockquote nesting for hierarchy."""
    for i, comment in enumerate(comments):
        if isinstance(comment, Comment):
            bq = "> " * depth if depth > 0 else ""

            # Write comment header
            author = comment.author.name if comment.author else "[deleted]"
            f.write(f'\n{bq}**Comment by /u/{author}**\n')
            f.write(f'{bq}*Upvotes: {comment.score} | [Permalink](https://reddit.com{comment.permalink})*\n\n')

            # Process comment body
            comment_body = comment.body if comment.body else '[deleted]'

            # Check for image URLs in the comment body
            image_url = None
            if any(comment_body.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
                potential_url = comment_body.split()[-1]
                if potential_url.startswith(('http://', 'https://')) and '.' in potential_url:
                    image_url = potential_url

            if image_url:
                # Write any text before the image URL
                body_before_url = comment_body[:comment_body.rfind(image_url)].strip()
                if body_before_url:
                    for line in body_before_url.split('\n'):
                        f.write(f'{bq}{line}\n')
                    f.write(f'{bq}\n')

                image_path, image_size = download_image(image_url, os.path.dirname(f.name), comment.id, ignore_tls_errors)
                if image_path:
                    f.write(f'{bq}![Image]({image_path})\n')
                    f.write(f'{bq}*Original Image URL: [Link]({image_url})*\n\n')
                    _track_media_size(image_size)
                else:
                    f.write(f'{bq}![Image]({image_url})\n\n')
            else:
                lines = comment_body.split('\n')
                for line in lines:
                    f.write(f'{bq}{line}\n')
                f.write('\n')

            # Recursively process child comments
            if not simple_format and comment.replies:
                process_comments(comment.replies, f, depth + 1, simple_format, ignore_tls_errors)

            # Add separator only for top-level comments to reduce clutter
            if depth == 0:
                f.write('---\n')