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


def _save_csv_only_post(row, save_directory, existing_files, file_log, created_dirs_cache):
    """Save a minimal markdown file for a GDPR post using only CSV metadata (no API)."""
    post_id = str(row['id'])
    permalink = row.get('permalink', '')
    unique_key = f"GDPR_POST_{post_id}"

    if unique_key in existing_files or is_file_logged(file_log, unique_key):
        return 0, 0  # skipped

    # Use a generic subreddit folder since we can't determine it from CSV alone
    subreddit = _extract_subreddit_from_permalink(permalink)
    path_result = create_reddit_file_path(save_directory, subreddit, "GDPR_POST", post_id)
    if not path_result.is_safe:
        logger.error(f"Unsafe path for GDPR post {post_id}: {path_result.issues}")
        return 0, 0

    file_path = path_result.safe_path

    # Ensure directory exists
    dir_path = os.path.dirname(file_path)
    if dir_path not in created_dirs_cache:
        os.makedirs(dir_path, exist_ok=True)
        created_dirs_cache.add(dir_path)

    reddit_url = f"https://www.reddit.com{permalink}" if permalink else f"https://www.reddit.com/comments/{post_id}"
    content = (
        f"# GDPR Export Post: {post_id}\n\n"
        f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
        f"**Post ID:** {post_id}\n\n"
        f"---\n\n"
        f"*This is a CSV-only export (no API credentials available). "
        f"Content was not fetched from Reddit. "
        f"Visit the link above to view the full post.*\n"
    )

    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

    file_size = os.path.getsize(file_path)
    log_file(file_log, unique_key,
             {'file_path': file_path, 'type': 'GDPR_POST', 'id': post_id},
             save_directory)
    existing_files.add(unique_key)
    return 1, file_size


def _save_csv_only_comment(row, save_directory, existing_files, file_log, created_dirs_cache):
    """Save a minimal markdown file for a GDPR comment using only CSV metadata (no API)."""
    comment_id = str(row['id'])
    permalink = row.get('permalink', '')
    unique_key = f"GDPR_COMMENT_{comment_id}"

    if unique_key in existing_files or is_file_logged(file_log, unique_key):
        return 0, 0  # skipped

    subreddit = _extract_subreddit_from_permalink(permalink)
    path_result = create_reddit_file_path(save_directory, subreddit, "GDPR_COMMENT", comment_id)
    if not path_result.is_safe:
        logger.error(f"Unsafe path for GDPR comment {comment_id}: {path_result.issues}")
        return 0, 0

    file_path = path_result.safe_path

    dir_path = os.path.dirname(file_path)
    if dir_path not in created_dirs_cache:
        os.makedirs(dir_path, exist_ok=True)
        created_dirs_cache.add(dir_path)

    reddit_url = f"https://www.reddit.com{permalink}" if permalink else f"https://www.reddit.com/comments/{comment_id}"
    content = (
        f"# GDPR Export Comment: {comment_id}\n\n"
        f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
        f"**Comment ID:** {comment_id}\n\n"
        f"---\n\n"
        f"*This is a CSV-only export (no API credentials available). "
        f"Content was not fetched from Reddit. "
        f"Visit the link above to view the full comment.*\n"
    )

    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

    file_size = os.path.getsize(file_path)
    log_file(file_log, unique_key,
             {'file_path': file_path, 'type': 'GDPR_COMMENT', 'id': comment_id},
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


def process_gdpr_export(reddit, save_directory, existing_files, created_dirs_cache, file_log):
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
              "Saving post/comment links from GDPR export without fetching full content.")
    else:
        # Load the ignore_tls_errors setting only when API is available
        ignore_tls_errors = get_ignore_tls_errors()

    gdpr_dir = get_gdpr_directory(save_directory)

    # Process saved posts
    posts_file = os.path.join(gdpr_dir, 'saved_posts.csv')
    if os.path.exists(posts_file):
        print("\nProcessing saved posts from GDPR export...")
        df = pd.read_csv(posts_file)

        for _, row in tqdm(df.iterrows(), total=len(df), desc="Processing GDPR Posts"):
            try:
                if csv_only_mode:
                    count, size = _save_csv_only_post(
                        row, save_directory, existing_files, file_log, created_dirs_cache)
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
                    if save_to_file(submission, file_path, save_submission,
                                  existing_files, file_log, save_directory, created_dirs_cache,
                                  ignore_tls_errors=ignore_tls_errors):
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

        for _, row in tqdm(df.iterrows(), total=len(df), desc="Processing GDPR Comments"):
            try:
                if csv_only_mode:
                    count, size = _save_csv_only_comment(
                        row, save_directory, existing_files, file_log, created_dirs_cache)
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
                    if save_to_file(comment, file_path, save_comment_and_context,
                                  existing_files, file_log, save_directory, created_dirs_cache,
                                  ignore_tls_errors=ignore_tls_errors):
                        skipped_count += 1
                        continue

                    processed_count += 1
                    total_size += os.path.getsize(file_path)
                    dynamic_sleep(len(comment.body))

            except Exception as e:
                print(f"Error processing GDPR comment {row['id']}: {e}")
                skipped_count += 1

    return processed_count, skipped_count, total_size