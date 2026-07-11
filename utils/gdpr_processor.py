import os
import logging
import pandas as pd
from tqdm import tqdm
from utils.file_operations import save_to_file
from utils.save_utils import save_submission, save_comment_and_context
from utils.time_utilities import dynamic_sleep
from utils.env_config import get_ignore_tls_errors
from utils.path_security import create_reddit_file_path
from utils.log_utils import log_file, is_file_logged
from utils.content_recovery.arctic_shift import ArcticShiftClient
from utils.content_recovery.providers.pullpush_provider import PullPushProvider


logger = logging.getLogger(__name__)

def get_gdpr_directory(save_directory):
    """Return the path to the GDPR data directory."""
    # This function only checks for the directory's existence and
    # prints a message if it's missing. Creation should be handled
    # by the caller.
    gdpr_dir = os.path.join(save_directory, 'gdpr_data')
    if not os.path.exists(gdpr_dir):
        print(f"GDPR data directory not found at: {gdpr_dir}")
    return gdpr_dir


def _save_csv_only_post(
    row,
    save_directory,
    existing_files,
    file_log,
    created_dirs_cache,
    archived_data=None,
):
    """Save a minimal markdown file for a GDPR post using only CSV metadata (no API)."""
    post_id = str(row['id'])
    permalink = row.get('permalink', '')
    unique_key = f"GDPR_POST_{post_id}"

    # Use a generic subreddit folder since we can't determine it from CSV alone
    subreddit = _extract_subreddit_from_permalink(permalink)
    path_result = create_reddit_file_path(save_directory, subreddit, "GDPR_POST", post_id)
    if not path_result.is_safe:
        logger.error(f"Unsafe path for GDPR post {post_id}: {path_result.issues}")
        return 0, 0

    file_path = path_result.safe_path
    already_saved = unique_key in existing_files or is_file_logged(file_log, unique_key)
    if already_saved and not (archived_data and _is_link_only_csv_export(file_path)):
        return 0, 0  # skipped

    # Ensure directory exists
    dir_path = os.path.dirname(file_path)
    if dir_path not in created_dirs_cache:
        os.makedirs(dir_path, exist_ok=True)
        created_dirs_cache.add(dir_path)

    reddit_url = f"https://www.reddit.com{permalink}" if permalink else f"https://www.reddit.com/comments/{post_id}"
    content = _csv_only_post_markdown(post_id, reddit_url, archived_data)
    return _write_csv_only_export(
        file_path,
        content,
        unique_key,
        'GDPR_POST',
        post_id,
        bool(archived_data),
        existing_files,
        file_log,
        save_directory,
    )


def _save_csv_only_comment(
    row,
    save_directory,
    existing_files,
    file_log,
    created_dirs_cache,
    archived_data=None,
):
    """Save a minimal markdown file for a GDPR comment using only CSV metadata (no API)."""
    comment_id = str(row['id'])
    permalink = row.get('permalink', '')
    unique_key = f"GDPR_COMMENT_{comment_id}"

    subreddit = _extract_subreddit_from_permalink(permalink)
    path_result = create_reddit_file_path(save_directory, subreddit, "GDPR_COMMENT", comment_id)
    if not path_result.is_safe:
        logger.error(f"Unsafe path for GDPR comment {comment_id}: {path_result.issues}")
        return 0, 0

    file_path = path_result.safe_path
    already_saved = unique_key in existing_files or is_file_logged(file_log, unique_key)
    if already_saved and not (archived_data and _is_link_only_csv_export(file_path)):
        return 0, 0  # skipped

    dir_path = os.path.dirname(file_path)
    if dir_path not in created_dirs_cache:
        os.makedirs(dir_path, exist_ok=True)
        created_dirs_cache.add(dir_path)

    reddit_url = f"https://www.reddit.com{permalink}" if permalink else f"https://www.reddit.com/comments/{comment_id}"
    content = _csv_only_comment_markdown(comment_id, reddit_url, archived_data)

    return _write_csv_only_export(
        file_path,
        content,
        unique_key,
        'GDPR_COMMENT',
        comment_id,
        bool(archived_data),
        existing_files,
        file_log,
        save_directory,
    )


def _is_link_only_csv_export(file_path):
    """Return whether an existing CSV-only export is missing archived content."""
    try:
        with open(file_path, encoding='utf-8') as export_file:
            return 'Content was not fetched from Reddit.' in export_file.read()
    except OSError:
        return False


def _write_csv_only_export(
    file_path,
    content,
    unique_key,
    content_type,
    item_id,
    archive_enriched,
    existing_files,
    file_log,
    save_directory,
):
    """Write a new or archive-enriched CSV-only export and update its log entry."""

    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

    file_size = os.path.getsize(file_path)
    log_file(file_log, unique_key,
             {
                 'file_path': file_path,
                 'type': content_type,
                 'id': item_id,
                 'archive_enriched': archive_enriched,
             },
             save_directory)
    existing_files.add(unique_key)
    return 1, file_size


def _extract_subreddit_from_permalink(permalink):
    """Extract subreddit name from a Reddit permalink, or return a fallback."""
    if permalink:
        # Permalink format: /r/SubredditName/comments/...
        parts = permalink.strip('/').split('/')
        if len(parts) >= 2 and parts[0] == 'r':
            return f"r_{parts[1]}"
    return "r_unknown"


def _csv_only_post_markdown(post_id, reddit_url, archived_data):
    """Render a CSV-only post export, including archive text when available."""
    if not archived_data:
        return (
            f"# GDPR Export Post: {post_id}\n\n"
            f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
            f"**Post ID:** {post_id}\n\n"
            f"---\n\n"
            f"*This is a CSV-only export (no API credentials available). "
            f"Content was not fetched from Reddit. "
            f"Visit the link above to view the full post.*\n"
        )

    title = _archive_text(archived_data.get('title')) or f'GDPR Export Post: {post_id}'
    body = _archive_text(archived_data.get('selftext')) or '*No post text was available in the archive.*'
    return (
        f"# {title}\n\n"
        f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
        f"**Post ID:** {post_id}\n\n"
        f"**Archive:** Public Reddit archive\n\n"
        f"---\n\n"
        f"## Post\n\n{body}\n"
    )


def _csv_only_comment_markdown(comment_id, reddit_url, archived_data):
    """Render a CSV-only comment export, including archive text when available."""
    if not archived_data:
        return (
            f"# GDPR Export Comment: {comment_id}\n\n"
            f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
            f"**Comment ID:** {comment_id}\n\n"
            f"---\n\n"
            f"*This is a CSV-only export (no API credentials available). "
            f"Content was not fetched from Reddit. "
            f"Visit the link above to view the full comment.*\n"
        )

    body = _archive_text(archived_data.get('body')) or '*No comment text was available in the archive.*'
    return (
        f"# GDPR Export Comment: {comment_id}\n\n"
        f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
        f"**Comment ID:** {comment_id}\n\n"
        f"**Archive:** Public Reddit archive\n\n"
        f"---\n\n"
        f"## Comment\n\n{body}\n"
    )


def _archive_text(value):
    """Return non-empty archive text without turning missing values into strings."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _archive_id(item_id):
    """Normalize a Reddit fullname to the base36 ID used in archive responses."""
    if pd.isna(item_id):
        return ''
    normalized_id = str(item_id).strip()
    return normalized_id[3:] if normalized_id.startswith(('t1_', 't3_')) else normalized_id


def _fetch_from_archive(ids, content_type, archive_client, archive_fallback=None):
    """Fetch a batch from Arctic Shift, then enrich missing records from PullPush."""
    normalized_ids = list(dict.fromkeys(
        normalized_id
        for item_id in ids
        if (normalized_id := _archive_id(item_id))
    ))
    fetch_method = archive_client.fetch_posts if content_type == 'posts' else archive_client.fetch_comments

    try:
        records = fetch_method(normalized_ids)
    except Exception as error:
        logger.warning(f'Archive lookup failed for GDPR {content_type}: {error}')
        records = {}

    records = records if isinstance(records, dict) else {}
    normalized_records = {
        _archive_id(item_id): record
        for item_id, record in records.items()
        if isinstance(record, dict)
    }
    missing_ids = [item_id for item_id in normalized_ids if item_id not in normalized_records]

    if archive_fallback and missing_ids:
        try:
            fallback_records = archive_fallback.fetch_metadata_by_ids(content_type, missing_ids)
        except Exception as error:
            logger.warning(f'PullPush fallback failed for GDPR {content_type}: {error}')
            fallback_records = {}
        fallback_records = fallback_records if isinstance(fallback_records, dict) else {}
        normalized_records.update({
            _archive_id(item_id): record
            for item_id, record in fallback_records.items()
            if isinstance(record, dict)
        })

    return normalized_records


def process_gdpr_export(
    reddit,
    save_directory,
    existing_files,
    created_dirs_cache,
    file_log,
    archive_client=None,
    archive_fallback=None,
):
    """Process saved posts/comments from GDPR export CSVs.

    When reddit is None (no API credentials), saves minimal markdown files
    using only CSV metadata (post IDs and permalinks).
    """
    processed_count = 0
    skipped_count = 0
    total_size = 0

    csv_only_mode = reddit is None
    if csv_only_mode:
        print("Running in CSV-only mode (no API credentials). "
              "Fetching archived post and comment text when it is available.")
        if archive_client is None:
            archive_client = ArcticShiftClient()
            archive_fallback = archive_fallback or PullPushProvider()
    else:
        # Load the ignore_tls_errors setting only when API is available
        ignore_tls_errors = get_ignore_tls_errors()

    gdpr_dir = get_gdpr_directory(save_directory)

    # Process saved posts
    posts_file = os.path.join(gdpr_dir, 'saved_posts.csv')
    if os.path.exists(posts_file):
        print("\nProcessing saved posts from GDPR export...")
        df = pd.read_csv(posts_file)
        archived_posts = (
            _fetch_from_archive(df['id'].tolist(), 'posts', archive_client, archive_fallback)
            if csv_only_mode else {}
        )

        for _, row in tqdm(df.iterrows(), total=len(df), desc="Processing GDPR Posts"):
            try:
                if csv_only_mode:
                    count, size = _save_csv_only_post(
                        row,
                        save_directory,
                        existing_files,
                        file_log,
                        created_dirs_cache,
                        archived_posts.get(_archive_id(row['id'])),
                    )
                    if count == 0:
                        skipped_count += 1
                    else:
                        processed_count += count
                        total_size += size
                else:
                    # Get full submission data using the ID
                    submission = reddit.submission(id=row['id'])
                    # Use secure path creation to prevent directory traversal
                    path_result = create_reddit_file_path(
                        save_directory, submission.subreddit.display_name, "GDPR_POST", submission.id
                    )
                    if not path_result.is_safe:
                        logger.error(f"Unsafe path for GDPR submission {submission.id}: {path_result.issues}")
                        skipped_count += 1
                        continue

                    file_path = path_result.safe_path

                    # Use existing save_to_file function
                    already_saved, _ = save_to_file(
                        submission,
                        file_path,
                        save_submission,
                        existing_files,
                        file_log,
                        save_directory,
                        created_dirs_cache,
                        ignore_tls_errors=ignore_tls_errors,
                    )
                    if already_saved:
                        skipped_count += 1
                        continue

                    processed_count += 1
                    total_size += os.path.getsize(file_path)
                    dynamic_sleep(len(submission.selftext) if submission.is_self else 0)

            except Exception as e:
                print(f"Error processing GDPR post {row['id']}: {e}")
                skipped_count += 1

    # Process saved comments
    comments_file = os.path.join(gdpr_dir, 'saved_comments.csv')
    if os.path.exists(comments_file):
        print("\nProcessing saved comments from GDPR export...")
        df = pd.read_csv(comments_file)
        archived_comments = (
            _fetch_from_archive(df['id'].tolist(), 'comments', archive_client, archive_fallback)
            if csv_only_mode else {}
        )

        for _, row in tqdm(df.iterrows(), total=len(df), desc="Processing GDPR Comments"):
            try:
                if csv_only_mode:
                    count, size = _save_csv_only_comment(
                        row,
                        save_directory,
                        existing_files,
                        file_log,
                        created_dirs_cache,
                        archived_comments.get(_archive_id(row['id'])),
                    )
                    if count == 0:
                        skipped_count += 1
                    else:
                        processed_count += count
                        total_size += size
                else:
                    # Get full comment data using the ID
                    comment = reddit.comment(id=row['id'])
                    # Use secure path creation to prevent directory traversal
                    path_result = create_reddit_file_path(
                        save_directory, comment.subreddit.display_name, "GDPR_COMMENT", comment.id
                    )
                    if not path_result.is_safe:
                        logger.error(f"Unsafe path for GDPR comment {comment.id}: {path_result.issues}")
                        skipped_count += 1
                        continue

                    file_path = path_result.safe_path

                    # Use existing save_to_file function
                    already_saved, _ = save_to_file(
                        comment,
                        file_path,
                        save_comment_and_context,
                        existing_files,
                        file_log,
                        save_directory,
                        created_dirs_cache,
                        ignore_tls_errors=ignore_tls_errors,
                    )
                    if already_saved:
                        skipped_count += 1
                        continue

                    processed_count += 1
                    total_size += os.path.getsize(file_path)
                    dynamic_sleep(len(comment.body))

            except Exception as e:
                print(f"Error processing GDPR comment {row['id']}: {e}")
                skipped_count += 1

    return processed_count, skipped_count, total_size
