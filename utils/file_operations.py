import os
import configparser
import logging
import threading
import prawcore
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
from praw.models import Submission, Comment
from utils.log_utils import log_file, save_file_log
from utils.save_utils import save_submission, save_comment_and_context, _reset_media_tracker, _get_media_size
from utils.time_utilities import dynamic_sleep
from utils.env_config import get_ignore_tls_errors
from utils.path_security import create_safe_path, create_reddit_file_path
from utils.praw_helpers import safe_fetch_items_one_by_one


logger = logging.getLogger(__name__)

# Lock protecting concurrent access to created_dirs_cache (check-then-create pattern)
_dir_cache_lock = threading.Lock()

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
    with _dir_cache_lock:
        if sub_dir not in created_dirs_cache:
            os.makedirs(sub_dir, exist_ok=True)
            created_dirs_cache.add(sub_dir)

    # Proceed with saving the file
    try:
        # Reset media size tracker before saving
        _reset_media_tracker()

        with open(file_path, 'w', encoding="utf-8") as f:
            save_function(content, f, unsave=unsave, ignore_tls_errors=ignore_tls_errors)

        # Get accumulated media size from the tracker
        media_size = _get_media_size()

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


def _clone_reddit(reddit):
    """Create a new PRAW Reddit instance with same credentials for thread-safe parallel use.

    PRAW's Reddit instance is NOT thread-safe (shares a single requests.Session).
    Each thread needs its own instance with separate OAuth token and rate limit budget.
    """
    import praw
    return praw.Reddit(
        client_id=reddit.config.client_id,
        client_secret=reddit.config.client_secret,
        username=reddit.config.username,
        password=reddit.config.password,
        user_agent=reddit.config.user_agent,
    )


def _fetch_items(reddit_instance, fetch_method_name, limit, label):
    """Fetch items from a PRAW user endpoint with batch-then-one-by-one fallback.

    Uses its own PRAW instance for thread-safe parallel fetching.
    """
    user = reddit_instance.user.me()
    if fetch_method_name in ('submissions', 'comments'):
        listing = getattr(user, fetch_method_name)
        items_iter = listing.new(limit=limit)
    else:
        items_iter = getattr(user, fetch_method_name)(limit=limit)

    try:
        return list(items_iter)
    except prawcore.exceptions.NotFound:
        logger.warning(f"Batch fetch of {label} failed with 404, using safe iteration")
        if fetch_method_name in ('submissions', 'comments'):
            listing = getattr(user, fetch_method_name)
            items_iter = listing.new(limit=limit)
        else:
            items_iter = getattr(user, fetch_method_name)(limit=limit)
        return safe_fetch_items_one_by_one(items_iter, label)


def _merge_results(*results):
    """Merge (processed, skipped, size, media_size) tuples from parallel threads."""
    processed = sum(r[0] for r in results)
    skipped = sum(r[1] for r in results)
    size = sum(r[2] for r in results)
    media_size = sum(r[3] for r in results)
    return processed, skipped, size, media_size


def _process_submissions_batch(submissions, save_directory, existing_files, created_dirs_cache,
                               file_log, ignore_tls_errors, category="POST", unsave=False,
                               tqdm_desc="Processing Submissions", tqdm_position=0):
    """Process a batch of submissions in a single thread.

    Returns (processed_count, skipped_count, total_size, total_media_size).
    """
    processed_count = 0
    skipped_count = 0
    total_size = 0
    total_media_size = 0

    for submission in tqdm(submissions, desc=tqdm_desc, position=tqdm_position, leave=True):
        path_result = create_reddit_file_path(
            save_directory, submission.subreddit.display_name, category, submission.id
        )
        if not path_result.is_safe:
            logger.error(f"Unsafe path for submission {submission.id}: {path_result.issues}")
            continue

        file_path = path_result.safe_path
        save_result, media_size = save_to_file(
            submission, file_path, save_submission, existing_files, file_log,
            save_directory, created_dirs_cache, category=category,
            unsave=unsave, ignore_tls_errors=ignore_tls_errors
        )
        if save_result:
            skipped_count += 1
            continue

        processed_count += 1
        total_media_size += media_size
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            pass
        handle_dynamic_sleep(submission)

    return processed_count, skipped_count, total_size, total_media_size


def _process_comments_batch(comments, save_directory, existing_files, created_dirs_cache,
                            file_log, ignore_tls_errors, category="COMMENT", unsave=False,
                            tqdm_desc="Processing Comments", tqdm_position=1):
    """Process a batch of comments in a single thread.

    Returns (processed_count, skipped_count, total_size, total_media_size).
    """
    processed_count = 0
    skipped_count = 0
    total_size = 0
    total_media_size = 0

    for comment in tqdm(comments, desc=tqdm_desc, position=tqdm_position, leave=True):
        path_result = create_reddit_file_path(
            save_directory, comment.subreddit.display_name, category, comment.id
        )
        if not path_result.is_safe:
            logger.error(f"Unsafe path for comment {comment.id}: {path_result.issues}")
            continue

        file_path = path_result.safe_path
        save_result, media_size = save_to_file(
            comment, file_path, save_comment_and_context, existing_files, file_log,
            save_directory, created_dirs_cache, category=category,
            unsave=unsave, ignore_tls_errors=ignore_tls_errors
        )
        if save_result:
            skipped_count += 1
            continue

        processed_count += 1
        total_media_size += media_size
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            pass
        handle_dynamic_sleep(comment)

    return processed_count, skipped_count, total_size, total_media_size


def _process_mixed_items(items, save_directory, existing_files, created_dirs_cache,
                         file_log, ignore_tls_errors, sub_category="SAVED_POST",
                         comment_category="SAVED_COMMENT", unsave=False,
                         tqdm_desc="Processing Items", tqdm_position=0):
    """Process mixed submissions and comments in a single thread.

    Items from one PRAW fetch share the same reddit instance, so they must be
    processed in one thread to avoid concurrent access to the shared session.

    Returns (processed_count, skipped_count, total_size, total_media_size).
    """
    processed_count = 0
    skipped_count = 0
    total_size = 0
    total_media_size = 0

    for item in tqdm(items, desc=tqdm_desc, position=tqdm_position, leave=True):
        if isinstance(item, Submission):
            category = sub_category
            save_fn = save_submission
        elif isinstance(item, Comment):
            category = comment_category
            save_fn = save_comment_and_context
        else:
            continue

        path_result = create_reddit_file_path(
            save_directory, item.subreddit.display_name, category, item.id
        )
        if not path_result.is_safe:
            logger.error(f"Unsafe path for item {item.id}: {path_result.issues}")
            continue

        file_path = path_result.safe_path
        save_result, media_size = save_to_file(
            item, file_path, save_fn, existing_files, file_log,
            save_directory, created_dirs_cache, category=category,
            unsave=unsave, ignore_tls_errors=ignore_tls_errors
        )
        if save_result:
            skipped_count += 1
            continue

        processed_count += 1
        total_media_size += media_size
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            pass
        handle_dynamic_sleep(item)

    return processed_count, skipped_count, total_size, total_media_size


def save_user_activity(reddit, save_directory, file_log, unsave=False):
    """Save user's posts, comments, saved items, and upvoted content.

    Uses parallel fetching and processing when possible:
    - ALL mode: 4 parallel fetches, then 4 parallel processing threads
    - ACTIVITY mode: 2 parallel fetches, then 2 parallel processing threads
    - SAVED/UPVOTED mode: 1 fetch, then 2 parallel processing threads (subs + comments)
    """
    ignore_tls_errors = get_ignore_tls_errors()

    if check_type == 'LOG':
        print("Check type is LOG. Using JSON log to find existing files.")
        existing_files = get_existing_files_from_log(file_log)
    elif check_type == 'DIR':
        print("Check type is DIR. Using directory scan to find existing files.")
        existing_files = get_existing_files_from_dir(save_directory)
    else:
        raise ValueError(f"Unknown check_type: {check_type}")

    created_dirs_cache = set()

    shared_args = dict(
        save_directory=save_directory, existing_files=existing_files,
        created_dirs_cache=created_dirs_cache, file_log=file_log,
        ignore_tls_errors=ignore_tls_errors,
    )

    if save_type == 'ALL':
        # Phase 1: Parallel fetching — 4 threads, each with its own PRAW instance
        endpoints = [
            ('submissions', 'submission'),
            ('comments', 'comment'),
            ('saved', 'saved'),
            ('upvoted', 'upvoted'),
        ]
        fetched = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for method, label in endpoints:
                r = _clone_reddit(reddit)
                futures[label] = pool.submit(_fetch_items, r, method, 1000, label)
            for label, future in futures.items():
                fetched[label] = future.result()

        submissions = fetched['submission']
        comments = fetched['comment']
        saved_items = fetched['saved']
        upvoted_items = fetched['upvoted']

        # Phase 2: Parallel processing — 4 threads, one per fetch source
        # Items from each fetch share a PRAW instance, so each source must stay
        # in one thread. saved/upvoted are processed as mixed-type batches.
        with ThreadPoolExecutor(max_workers=4) as pool:
            f1 = pool.submit(
                _process_submissions_batch, submissions,
                category="POST", tqdm_desc="Submissions", tqdm_position=0, **shared_args
            )
            f2 = pool.submit(
                _process_comments_batch, comments,
                category="COMMENT", tqdm_desc="Comments", tqdm_position=1, **shared_args
            )
            f3 = pool.submit(
                _process_mixed_items, saved_items,
                sub_category="SAVED_POST", comment_category="SAVED_COMMENT",
                unsave=unsave, tqdm_desc="Saved Items", tqdm_position=2, **shared_args
            )
            f4 = pool.submit(
                _process_mixed_items, upvoted_items,
                sub_category="UPVOTE_POST", comment_category="UPVOTE_COMMENT",
                tqdm_desc="Upvoted Items", tqdm_position=3, **shared_args
            )
            results = [f.result() for f in [f1, f2, f3, f4]]

        # Phase 3: Merge all counters
        processed_count, skipped_count, total_size, total_media_size = _merge_results(*results)

    elif save_type == 'ACTIVITY':
        # Parallel fetch: submissions + comments
        with ThreadPoolExecutor(max_workers=2) as pool:
            r1 = _clone_reddit(reddit)
            r2 = _clone_reddit(reddit)
            f_sub = pool.submit(_fetch_items, r1, 'submissions', 1000, 'submission')
            f_com = pool.submit(_fetch_items, r2, 'comments', 1000, 'comment')
            submissions = f_sub.result()
            comments = f_com.result()

        # Parallel processing: submissions + comments
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(
                _process_submissions_batch, submissions,
                category="POST", tqdm_desc="Submissions", tqdm_position=0, **shared_args
            )
            f2 = pool.submit(
                _process_comments_batch, comments,
                category="COMMENT", tqdm_desc="Comments", tqdm_position=1, **shared_args
            )
            processed_count, skipped_count, total_size, total_media_size = _merge_results(
                f1.result(), f2.result()
            )

    elif save_type == 'SAVED':
        saved_items = _fetch_items(reddit, 'saved', 1000, 'saved')
        processed_count, skipped_count, total_size, total_media_size = _process_mixed_items(
            saved_items, sub_category="SAVED_POST", comment_category="SAVED_COMMENT",
            unsave=unsave, tqdm_desc="Saved Items", **shared_args
        )

    elif save_type == 'UPVOTED':
        upvoted_items = _fetch_items(reddit, 'upvoted', 1000, 'upvoted')
        processed_count, skipped_count, total_size, total_media_size = _process_mixed_items(
            upvoted_items, sub_category="UPVOTE_POST", comment_category="UPVOTE_COMMENT",
            tqdm_desc="Upvoted Items", **shared_args
        )

    else:
        raise ValueError(f"Unknown save_type: {save_type}")

    # Save the updated file log (all threads are done at this point)
    save_file_log(file_log, save_directory)

    return processed_count, skipped_count, total_size, total_media_size
