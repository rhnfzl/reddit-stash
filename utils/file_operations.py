import os
import configparser
import logging
import prawcore
from tqdm import tqdm
from praw.models import Submission, Comment  # Import Submission and Comment
from utils.log_utils import log_file, save_file_log
from utils.save_utils import save_submission, save_comment_and_context  # Import common functions
from utils.time_utilities import dynamic_sleep
from utils.env_config import get_ignore_tls_errors
from utils.path_security import create_safe_path, create_reddit_file_path
from utils.praw_helpers import safe_fetch_items, safe_fetch_items_one_by_one


logger = logging.getLogger(__name__)

# Dynamically determine the path to the root directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Construct the full path to the settings.ini file
config_path = os.path.join(BASE_DIR, 'settings.ini')

# Load settings from the settings.ini file
config = configparser.ConfigParser()
config.read(config_path)
save_type = config.get('Settings', 'save_type', fallback='ALL').upper()
check_type = config.get('Settings', 'check_type', fallback='DIR').upper()

def create_directory(subreddit_name, save_directory, created_dirs_cache):
    """Create the directory for saving data if it does not exist."""
    # Use secure path creation to prevent directory traversal
    path_result = create_safe_path(save_directory, subreddit_name)

    if not path_result.is_safe:
        logger.error(f"Unsafe subreddit name '{subreddit_name}': {path_result.issues}")
        # Use a sanitized version or fallback
        fallback_name = "sanitized_subreddit"
        path_result = create_safe_path(save_directory, fallback_name)
        if not path_result.is_safe:
            raise ValueError(f"Cannot create safe directory path: {path_result.issues}")

    sub_dir = path_result.safe_path
    if sub_dir not in created_dirs_cache:
        os.makedirs(sub_dir, exist_ok=True)
        created_dirs_cache.add(sub_dir)
        logger.info(f"Created directory: {sub_dir}")

    return sub_dir

def get_existing_files_from_log(file_log):
    """Return a set of unique keys (subreddit + id) based on the JSON log."""
    existing_files = set(file_log.keys())
    return existing_files

def get_existing_files_from_dir(save_directory):
    """Build a set of all existing files in the save directory using os.walk."""
    existing_files = set()
    for root, dirs, files in os.walk(save_directory):
        for file in files:
            # Extract the unique key format (id-subreddit-content_type) from the file path
            filename = os.path.splitext(file)[0]
            subreddit_name = os.path.basename(root)
            content_type = None
            
            if filename.startswith("POST_"):
                file_id = filename.split("POST_")[1]
                content_type = "Submission"
            elif filename.startswith("COMMENT_"):
                file_id = filename.split("COMMENT_")[1]
                content_type = "Comment"
            elif filename.startswith("SAVED_POST_"):
                file_id = filename.split("SAVED_POST_")[1]
                content_type = "Submission"
            elif filename.startswith("SAVED_COMMENT_"):
                file_id = filename.split("SAVED_COMMENT_")[1]
                content_type = "Comment"
            else:
                continue
            
            unique_key = f"{file_id}-{subreddit_name}-{content_type}"
            existing_files.add(unique_key)
    return existing_files

def save_to_file(content, file_path, save_function, existing_files, file_log, save_directory, created_dirs_cache, category="POST", unsave=False, ignore_tls_errors=None):
    """Save content to a file using the specified save function."""
    from .praw_helpers import RecoveredItem

    # Check if this is a recovered item
    is_recovered = isinstance(content, RecoveredItem)

    file_id = content.id  # Assuming `id` is unique for each Reddit content

    if is_recovered:
        # For recovered items, get subreddit from recovered_data
        recovered_data = content.recovered_data if hasattr(content, 'recovered_data') else {}
        subreddit_name = recovered_data.get('subreddit', 'unknown')
    else:
        subreddit_name = content.subreddit.display_name  # Get the subreddit name

    # Create the unique key including the content type and category
    unique_key = f"{file_id}-{subreddit_name}-{type(content).__name__}-{category}"
    
    # If the file is already logged or exists in the directory, skip saving
    if unique_key in existing_files:
        return True, 0  # Indicate that the file already exists and no saving was performed, no media size

    # Ensure the subreddit directory exists only if we're about to save something new
    # Use secure path creation to prevent directory traversal
    path_result = create_safe_path(save_directory, subreddit_name)

    if not path_result.is_safe:
        logger.error(f"Unsafe subreddit name '{subreddit_name}': {path_result.issues}")
        # Use a sanitized version or fallback
        fallback_name = "sanitized_subreddit"
        path_result = create_safe_path(save_directory, fallback_name)
        if not path_result.is_safe:
            raise ValueError(f"Cannot create safe directory path: {path_result.issues}")

    sub_dir = path_result.safe_path
    if sub_dir not in created_dirs_cache:
        os.makedirs(sub_dir, exist_ok=True)
        created_dirs_cache.add(sub_dir)
    
    # Proceed with saving the file
    try:
        # Reset media size tracker before saving
        from .save_utils import save_submission
        if hasattr(save_submission, '_media_size_tracker'):
            delattr(save_submission, '_media_size_tracker')

        with open(file_path, 'w', encoding="utf-8") as f:
            save_function(content, f, unsave=unsave, ignore_tls_errors=ignore_tls_errors)

        # Get accumulated media size from the tracker
        media_size = getattr(save_submission, '_media_size_tracker', 0)

        # Prepare file info for logging
        file_info = {
            'subreddit': subreddit_name,
            'type': type(content).__name__,
            'file_path': file_path  # This will be converted to relative in log_file
        }

        # Add recovery metadata if this is a recovered item
        if is_recovered and hasattr(content, 'recovery_result'):
            recovery_result = content.recovery_result
            if recovery_result and recovery_result.metadata:
                file_info['recovered'] = True
                file_info['recovery_source'] = recovery_result.metadata.source.value
                file_info['recovery_timestamp'] = recovery_result.metadata.recovery_date
                file_info['recovery_quality'] = recovery_result.metadata.content_quality.value
                if recovery_result.recovered_url:
                    file_info['recovery_url'] = recovery_result.recovered_url

        # Log the file after saving successfully with the unique key
        log_file(file_log, unique_key, file_info, save_directory)

        return False, media_size  # Indicate that the file was saved successfully and return media size
    except Exception as e:
        print(f"Failed to save {file_path}: {e}")
        return False, 0  # Indicate that the file could not be saved, no media size

def handle_dynamic_sleep(item):
    """Handle dynamic sleep based on the type of Reddit item."""
    if isinstance(item, Submission) and item.is_self and item.selftext:
        dynamic_sleep(len(item.selftext))
    elif isinstance(item, Comment) and item.body:
        dynamic_sleep(len(item.body))
    else:
        dynamic_sleep(0)  # Minimal or no sleep for other types of posts


def save_user_activity(reddit, save_directory, file_log, unsave=False):
    """Save user's posts, comments, saved items, and upvoted content."""
    user = reddit.user.me()

    # Load the ignore_tls_errors setting
    ignore_tls_errors = get_ignore_tls_errors()

    # Determine how to check for existing files based on check_type
    if check_type == 'LOG':
        print("Check type is LOG. Using JSON log to find existing files.")
        existing_files = get_existing_files_from_log(file_log)
    elif check_type == 'DIR':
        print("Check type is DIR. Using directory scan to find existing files.")
        existing_files = get_existing_files_from_dir(save_directory)
    else:
        raise ValueError(f"Unknown check_type: {check_type}")

    created_dirs_cache = set()

    processed_count = 0  # Counter for processed items
    skipped_count = 0  # Counter for skipped items
    total_size = 0  # Total size of processed markdown data in bytes
    total_media_size = 0  # Total size of downloaded media files in bytes

    if save_type == 'ALL':
        # Save all user submissions and comments with safe fetching
        try:
            # Try batch fetch first (fast path)
            submissions = list(user.submissions.new(limit=1000))
        except prawcore.exceptions.NotFound:
            # Batch failed - use one-by-one fetch with recovery
            logger.warning("Batch fetch of submissions failed with 404, using safe iteration")
            submissions = safe_fetch_items_one_by_one(user.submissions.new(limit=1000), 'submission')

        try:
            # Try batch fetch first (fast path)
            comments = list(user.comments.new(limit=1000))
        except prawcore.exceptions.NotFound:
            # Batch failed - use one-by-one fetch with recovery
            logger.warning("Batch fetch of comments failed with 404, using safe iteration")
            comments = safe_fetch_items_one_by_one(user.comments.new(limit=1000), 'comment')

        processed_count, skipped_count, total_size, total_media_size = save_self_user_activity(
            submissions, comments,
            save_directory, existing_files, created_dirs_cache,
            processed_count, skipped_count, total_size, total_media_size, file_log, ignore_tls_errors
        )

        # Save all saved items (posts and comments)
        try:
            saved_items = list(user.saved(limit=1000))
        except prawcore.exceptions.NotFound:
            logger.warning("Batch fetch of saved items failed with 404, using safe iteration")
            saved_items = safe_fetch_items_one_by_one(user.saved(limit=1000), 'saved')

        processed_count, skipped_count, total_size, total_media_size = save_saved_user_activity(
            saved_items, save_directory, existing_files,
            created_dirs_cache, processed_count, skipped_count, total_size, total_media_size, file_log,
            unsave=unsave, ignore_tls_errors=ignore_tls_errors
        )

        # Save all upvoted posts and comments
        try:
            upvoted_items = list(user.upvoted(limit=1000))
        except prawcore.exceptions.NotFound:
            logger.warning("Batch fetch of upvoted items failed with 404, using safe iteration")
            upvoted_items = safe_fetch_items_one_by_one(user.upvoted(limit=1000), 'upvoted')

        processed_count, skipped_count, total_size, total_media_size = save_upvoted_posts_and_comments(
            upvoted_items, save_directory, existing_files, created_dirs_cache,
            processed_count, skipped_count, total_size, total_media_size, file_log, ignore_tls_errors
        )
    
    elif save_type == 'SAVED':
        try:
            saved_items = list(user.saved(limit=1000))
        except prawcore.exceptions.NotFound:
            logger.warning("Batch fetch of saved items failed with 404, using safe iteration")
            saved_items = safe_fetch_items_one_by_one(user.saved(limit=1000), 'saved')

        processed_count, skipped_count, total_size, total_media_size = save_saved_user_activity(
            saved_items, save_directory, existing_files,
            created_dirs_cache, processed_count, skipped_count, total_size, total_media_size, file_log,
            unsave=unsave, ignore_tls_errors=ignore_tls_errors
        )

    elif save_type == 'ACTIVITY':
        try:
            submissions = list(user.submissions.new(limit=1000))
        except prawcore.exceptions.NotFound:
            logger.warning("Batch fetch of submissions failed with 404, using safe iteration")
            submissions = safe_fetch_items_one_by_one(user.submissions.new(limit=1000), 'submission')

        try:
            comments = list(user.comments.new(limit=1000))
        except prawcore.exceptions.NotFound:
            logger.warning("Batch fetch of comments failed with 404, using safe iteration")
            comments = safe_fetch_items_one_by_one(user.comments.new(limit=1000), 'comment')

        processed_count, skipped_count, total_size, total_media_size = save_self_user_activity(
            submissions, comments,
            save_directory, existing_files, created_dirs_cache,
            processed_count, skipped_count, total_size, total_media_size, file_log, ignore_tls_errors
        )

    elif save_type == 'UPVOTED':
        try:
            upvoted_items = list(user.upvoted(limit=1000))
        except prawcore.exceptions.NotFound:
            logger.warning("Batch fetch of upvoted items failed with 404, using safe iteration")
            upvoted_items = safe_fetch_items_one_by_one(user.upvoted(limit=1000), 'upvoted')

        processed_count, skipped_count, total_size, total_media_size = save_upvoted_posts_and_comments(
            upvoted_items, save_directory, existing_files, created_dirs_cache,
            processed_count, skipped_count, total_size, total_media_size, file_log, ignore_tls_errors
        )

    # Save the updated file log
    save_file_log(file_log, save_directory)

    return processed_count, skipped_count, total_size, total_media_size


def save_self_user_activity(submissions, comments, save_directory, existing_files, created_dirs_cache, processed_count, skipped_count, total_size, total_media_size, file_log, ignore_tls_errors=None):
    """Save all user posts and comments."""
    for submission in tqdm(submissions, desc="Processing Users Submissions"):
        # Use secure path creation to prevent directory traversal
        path_result = create_reddit_file_path(
            save_directory, submission.subreddit.display_name, "POST", submission.id
        )
        if not path_result.is_safe:
            logger.error(f"Unsafe path for submission {submission.id}: {path_result.issues}")
            continue

        file_path = path_result.safe_path
        save_result, media_size = save_to_file(submission, file_path, save_submission, existing_files, file_log, save_directory, created_dirs_cache, category="POST", ignore_tls_errors=ignore_tls_errors)
        if save_result:
            skipped_count += 1
            continue

        # Only count file size if file was actually saved successfully
        processed_count += 1
        total_media_size += media_size  # Add media size from downloads
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            # File doesn't exist or can't be accessed, skip size calculation
            pass
        handle_dynamic_sleep(submission)  # Call the refactored sleep function

    for comment in tqdm(comments, desc="Processing Users Comments"):
        # Use secure path creation to prevent directory traversal
        path_result = create_reddit_file_path(
            save_directory, comment.subreddit.display_name, "COMMENT", comment.id
        )
        if not path_result.is_safe:
            logger.error(f"Unsafe path for comment {comment.id}: {path_result.issues}")
            continue

        file_path = path_result.safe_path
        save_result, media_size = save_to_file(comment, file_path, save_comment_and_context, existing_files, file_log, save_directory, created_dirs_cache, category="COMMENT", ignore_tls_errors=ignore_tls_errors)
        if save_result:
            skipped_count += 1
            continue

        # Only count file size if file was actually saved successfully
        processed_count += 1
        total_media_size += media_size  # Add media size from downloads
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            # File doesn't exist or can't be accessed, skip size calculation
            pass
        handle_dynamic_sleep(comment)  # Call the refactored sleep function

    return processed_count, skipped_count, total_size, total_media_size

def save_saved_user_activity(saved_items, save_directory, existing_files, created_dirs_cache, processed_count, skipped_count, total_size, total_media_size, file_log, unsave=False, ignore_tls_errors=None):
    """Save only saved user posts and comments."""
    for item in tqdm(saved_items, desc="Processing Saved Items"):
        if isinstance(item, Submission):
            # Use secure path creation to prevent directory traversal
            path_result = create_reddit_file_path(
                save_directory, item.subreddit.display_name, "SAVED_POST", item.id
            )
            if not path_result.is_safe:
                logger.error(f"Unsafe path for saved submission {item.id}: {path_result.issues}")
                continue

            file_path = path_result.safe_path
            save_result, media_size = save_to_file(item, file_path, save_submission, existing_files, file_log, save_directory, created_dirs_cache, category="SAVED_POST", unsave=unsave, ignore_tls_errors=ignore_tls_errors)
            if save_result:
                skipped_count += 1
                continue
        elif isinstance(item, Comment):
            # Use secure path creation to prevent directory traversal
            path_result = create_reddit_file_path(
                save_directory, item.subreddit.display_name, "SAVED_COMMENT", item.id
            )
            if not path_result.is_safe:
                logger.error(f"Unsafe path for saved comment {item.id}: {path_result.issues}")
                continue

            file_path = path_result.safe_path
            save_result, media_size = save_to_file(item, file_path, save_comment_and_context, existing_files, file_log, save_directory, created_dirs_cache, category="SAVED_COMMENT", unsave=unsave, ignore_tls_errors=ignore_tls_errors)
            if save_result:
                skipped_count += 1
                continue

        # Only count file size if file was actually saved successfully
        processed_count += 1
        total_media_size += media_size  # Add media size from downloads
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            # File doesn't exist or can't be accessed, skip size calculation
            pass
        handle_dynamic_sleep(item)

    return processed_count, skipped_count, total_size, total_media_size

def save_upvoted_posts_and_comments(upvoted_items, save_directory, existing_files, created_dirs_cache, processed_count, skipped_count, total_size, total_media_size, file_log, ignore_tls_errors=None):
    """Save only upvoted user posts and comments."""
    for item in tqdm(upvoted_items, desc="Processing Upvoted Items"):
        if isinstance(item, Submission):
            # Use secure path creation to prevent directory traversal
            path_result = create_reddit_file_path(
                save_directory, item.subreddit.display_name, "UPVOTE_POST", item.id
            )
            if not path_result.is_safe:
                logger.error(f"Unsafe path for upvoted submission {item.id}: {path_result.issues}")
                continue

            file_path = path_result.safe_path
            save_result, media_size = save_to_file(item, file_path, save_submission, existing_files, file_log, save_directory, created_dirs_cache, category="UPVOTE_POST", ignore_tls_errors=ignore_tls_errors)
            if save_result:
                skipped_count += 1
                continue
        elif isinstance(item, Comment):
            # Use secure path creation to prevent directory traversal
            path_result = create_reddit_file_path(
                save_directory, item.subreddit.display_name, "UPVOTE_COMMENT", item.id
            )
            if not path_result.is_safe:
                logger.error(f"Unsafe path for upvoted comment {item.id}: {path_result.issues}")
                continue

            file_path = path_result.safe_path
            save_result, media_size = save_to_file(item, file_path, save_comment_and_context, existing_files, file_log, save_directory, created_dirs_cache, category="UPVOTE_COMMENT", ignore_tls_errors=ignore_tls_errors)
            if save_result:
                skipped_count += 1
                continue

        # Only count file size if file was actually saved successfully
        processed_count += 1
        total_media_size += media_size  # Add media size from downloads
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            # File doesn't exist or can't be accessed, skip size calculation
            pass
        handle_dynamic_sleep(item)

    return processed_count, skipped_count, total_size, total_media_size
