from __future__ import annotations

import configparser
import logging
import os
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Tuple

import prawcore
from praw.models import Comment, Submission
from tqdm import tqdm

from utils.config_paths import get_settings_file_path
from utils.env_config import get_ignore_tls_errors
from utils.log_utils import log_file, save_file_log
from utils.praw_helpers import safe_fetch_items_one_by_one
from utils.save_utils import (
    _get_media_size,
    _reset_media_tracker,
    save_comment_and_context,
    save_submission,
)
from utils.time_utilities import dynamic_sleep
from utils.path_security import create_safe_path, create_reddit_file_path
from .archive_index import canonical_item_key, iter_archive_index
from .save_result import SaveResult, SaveStatus
from .failure_tracker import clear_failure, record_failure

logger = logging.getLogger(__name__)

# Lock protecting concurrent access to created_dirs_cache
_dir_cache_lock = threading.Lock()


def _load_active_config() -> configparser.ConfigParser:
    """Load the *active* settings file (respects SETTINGS_FILE / MEGA_SETTINGS_FILE).

    Previously this module always read the repo-root settings.ini directly,
    regardless of which settings file the running workflow selected. That
    meant a Mega run (SETTINGS_FILE=mega_settings.ini, check_type=LOG) would
    silently pick up check_type=DIR from settings.ini instead, which breaks
    the dedup check (see save_type/check_type below).
    """
    parser = configparser.ConfigParser()
    parser.read(get_settings_file_path())
    return parser


def _get_save_type() -> str:
    return _load_active_config().get("Settings", "save_type", fallback="ALL").upper()


def _get_check_type() -> str:
    return _load_active_config().get("Settings", "check_type", fallback="DIR").upper()


# Kept as module-level names for backward compatibility with any external
# callers/tests, but computed lazily so they always reflect the active
# settings file at call time rather than being frozen at import time.
save_type = _get_save_type()
check_type = _get_check_type()

def create_directory(subreddit_name, save_directory, created_dirs_cache):
    """Create the directory for saving data if it does not exist."""
    path_result = create_safe_path(save_directory, subreddit_name)

    if not path_result.is_safe:
        logger.error(
            "Unsafe subreddit name '%s': %s", subreddit_name, path_result.issues
        )
        fallback_name = "sanitized_subreddit"
        path_result = create_safe_path(save_directory, fallback_name)
        if not path_result.is_safe:
            raise ValueError(f"Cannot create safe directory path: {path_result.issues}")

    sub_dir = path_result.safe_path
    if sub_dir not in created_dirs_cache:
        os.makedirs(sub_dir, exist_ok=True)
        created_dirs_cache.add(sub_dir)
        logger.info("Created directory: %s", sub_dir)
    return sub_dir

def get_existing_files_from_log(file_log):
    """Return a set of unique keys based on the JSON log."""
    return set(file_log.keys())

def get_existing_files_from_dir(save_directory):
    """Index recognized Markdown archive files using the canonical save key."""
    return {key for key, _info in iter_archive_index(save_directory)}

def save_to_file(
    content,
    file_path,
    save_function,
    existing_files,
    file_log,
    save_directory,
    created_dirs_cache,
    category="POST",
    unsave=False,
    ignore_tls_errors=None,
):
    """Atomically save one item and distinguish saved, skipped, and failed."""
    from .praw_helpers import RecoveredItem

    if unsave:
        return SaveResult(
            SaveStatus.FAILED,
            error=(
                "Automatic unsaving is disabled until the remote archive upload "
                "has a verified post-upload commit stage."
            ),
        )

    is_recovered = isinstance(content, RecoveredItem)
    file_id = str(content.id)
    if is_recovered:
        recovered_data = getattr(content, "recovered_data", {}) or {}
        subreddit_name = str(recovered_data.get("subreddit") or "unknown")
    else:
        subreddit_name = str(content.subreddit.display_name)

    unique_key = canonical_item_key(file_id, subreddit_name, category)
    if unique_key in existing_files:
        return SaveResult(SaveStatus.SKIPPED)

    if os.path.exists(file_path):
        return SaveResult(
            SaveStatus.FAILED,
            error=f"Destination exists but is not indexed: {file_path}",
        )

    path_result = create_safe_path(save_directory, subreddit_name)
    if not path_result.is_safe:
        logger.error("Unsafe subreddit name '%s': %s", subreddit_name, path_result.issues)
        return SaveResult(SaveStatus.FAILED, error=f"Unsafe archive path: {path_result.issues}")

    sub_dir = path_result.safe_path
    with _dir_cache_lock:
        if sub_dir not in created_dirs_cache:
            os.makedirs(sub_dir, exist_ok=True)
            created_dirs_cache.add(sub_dir)

    temporary_path = None
    try:
        _reset_media_tracker()
        parent = os.path.dirname(file_path) or save_directory
        os.makedirs(parent, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            prefix=os.path.basename(file_path) + ".",
            suffix=".part",
            dir=parent,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            save_function(
                content,
                handle,
                unsave=False,
                ignore_tls_errors=ignore_tls_errors,
            )
            handle.flush()
            os.fsync(handle.fileno())

        # Publish the completed Markdown file atomically. The destination was
        # checked above; os.replace protects readers from partial content.
        os.replace(temporary_path, file_path)
        temporary_path = None
        media_size = int(_get_media_size() or 0)
        existing_files.add(unique_key)

        file_info = {
            "id": file_id,
            "subreddit": subreddit_name,
            "type": "Comment" if "COMMENT" in category.upper() else "Submission",
            "category": category.upper(),
            "file_path": file_path,
        }
        if is_recovered and hasattr(content, "recovery_result"):
            recovery_result = content.recovery_result
            if recovery_result and recovery_result.metadata:
                file_info["recovered"] = True
                file_info["recovery_source"] = recovery_result.metadata.source.value
                file_info["recovery_timestamp"] = recovery_result.metadata.recovery_date
                file_info["recovery_quality"] = recovery_result.metadata.content_quality.value
                if recovery_result.recovered_url:
                    file_info["recovery_url"] = recovery_result.recovered_url

        # If log persistence fails, the Markdown file remains available to the
        # directory index/rebuild utility; the run will still report a failure.
        log_file(file_log, unique_key, file_info, save_directory)
        return SaveResult(SaveStatus.SAVED, media_size=media_size)

    except Exception as exc:
        logger.exception("Failed to save %s", file_path)
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        return SaveResult(SaveStatus.FAILED, error=str(exc))

def handle_dynamic_sleep(item):
    """Handle dynamic sleep based on the type of Reddit item."""
    if isinstance(item, Submission) and item.is_self and item.selftext:
        dynamic_sleep(len(item.selftext))
    elif isinstance(item, Comment) and item.body:
        dynamic_sleep(len(item.body))
    else:
        dynamic_sleep(0)

def _clone_reddit(reddit):
    """Create a new PRAW Reddit instance with same credentials for thread-safe parallel use."""
    import praw

    return praw.Reddit(
        client_id=reddit.config.client_id,
        client_secret=reddit.config.client_secret,
        username=reddit.config.username,
        password=reddit.config.password,
        user_agent=reddit.config.user_agent,
    )

def _fetch_items(reddit_instance, fetch_method_name, limit, label):
    """Fetch items from a PRAW user endpoint with batch-then-one-by-one fallback."""
    user = reddit_instance.user.me()

    if fetch_method_name in ("submissions", "comments"):
        listing = getattr(user, fetch_method_name)
        items_iter = listing.new(limit=limit)
    else:
        items_iter = getattr(user, fetch_method_name)(limit=limit)

    try:
        return list(items_iter)
    except prawcore.exceptions.NotFound:
        logger.warning("Batch fetch of %s failed with 404, using safe iteration", label)
        if fetch_method_name in ("submissions", "comments"):
            listing = getattr(user, fetch_method_name)
            items_iter = listing.new(limit=limit)
        else:
            items_iter = getattr(user, fetch_method_name)(limit=limit)
        return safe_fetch_items_one_by_one(items_iter, label)

def _merge_results(*results):
    """Merge (processed, skipped, failed, Markdown bytes, media bytes) tuples."""
    return tuple(sum(result[index] for result in results) for index in range(5))

def _process_submissions_batch(
    submissions,
    save_directory,
    existing_files,
    created_dirs_cache,
    file_log,
    ignore_tls_errors,
    category="POST",
    unsave=False,
    tqdm_desc="Processing Submissions",
    tqdm_position=0,
):
    processed_count = skipped_count = failed_count = total_size = total_media_size = 0
    for submission in tqdm(submissions, desc=tqdm_desc, position=tqdm_position, leave=True):
        path_result = create_reddit_file_path(
            save_directory, submission.subreddit.display_name, category, submission.id
        )
        if not path_result.is_safe:
            logger.error("Unsafe path for submission %s: %s", submission.id, path_result.issues)
            failed_count += 1
            try:
                record_failure(save_directory, submission.id, submission.subreddit.display_name, category,
                               f"Unsafe archive path: {path_result.issues}")
            except Exception:
                logger.exception("Could not record unsafe path failure for %s", submission.id)
            continue
        file_path = path_result.safe_path

        result = save_to_file(
            submission,
            file_path,
            save_submission,
            existing_files,
            file_log,
            save_directory,
            created_dirs_cache,
            category=category,
            unsave=False,
            ignore_tls_errors=ignore_tls_errors,
        )
        item_id = str(getattr(submission, "id", "unknown"))
        subreddit_name = str(getattr(getattr(submission, "subreddit", None), "display_name", "unknown"))
        if result.status is SaveStatus.SKIPPED:
            try:
                clear_failure(save_directory, item_id, subreddit_name, category)
            except Exception:
                logger.exception("Could not clear previous failure for %s", item_id)
            skipped_count += 1
            continue
        if result.status is SaveStatus.FAILED:
            failed_count += 1
            logger.error("Item %s was not archived: %s", item_id, result.error)
            try:
                record_failure(save_directory, item_id, subreddit_name, category, result.error or "Unknown error")
            except Exception:
                logger.exception("Could not persist failure for %s", item_id)
            continue
    
        processed_count += 1
        total_media_size += int(result.media_size or 0)
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            logger.warning("Could not stat saved archive file %s", file_path)
        try:
            clear_failure(save_directory, item_id, subreddit_name, category)
        except Exception:
            logger.exception("Could not clear previous failure for %s", item_id)
        handle_dynamic_sleep(submission)

    return processed_count, skipped_count, failed_count, total_size, total_media_size

def _process_comments_batch(
    comments,
    save_directory,
    existing_files,
    created_dirs_cache,
    file_log,
    ignore_tls_errors,
    category="COMMENT",
    unsave=False,
    tqdm_desc="Processing Comments",
    tqdm_position=1,
):
    processed_count = skipped_count = failed_count = total_size = total_media_size = 0
    for comment in tqdm(comments, desc=tqdm_desc, position=tqdm_position, leave=True):
        path_result = create_reddit_file_path(
            save_directory, comment.subreddit.display_name, category, comment.id
        )
        if not path_result.is_safe:
            logger.error("Unsafe path for comment %s: %s", comment.id, path_result.issues)
            failed_count += 1
            try:
                record_failure(save_directory, comment.id, comment.subreddit.display_name, category,
                               f"Unsafe archive path: {path_result.issues}")
            except Exception:
                logger.exception("Could not record unsafe path failure for %s", comment.id)
            continue
        file_path = path_result.safe_path

        result = save_to_file(
            comment,
            file_path,
            save_comment_and_context,
            existing_files,
            file_log,
            save_directory,
            created_dirs_cache,
            category=category,
            unsave=False,
            ignore_tls_errors=ignore_tls_errors,
        )
        item_id = str(getattr(comment, "id", "unknown"))
        subreddit_name = str(getattr(getattr(comment, "subreddit", None), "display_name", "unknown"))
        if result.status is SaveStatus.SKIPPED:
            try:
                clear_failure(save_directory, item_id, subreddit_name, category)
            except Exception:
                logger.exception("Could not clear previous failure for %s", item_id)
            skipped_count += 1
            continue
        if result.status is SaveStatus.FAILED:
            failed_count += 1
            logger.error("Item %s was not archived: %s", item_id, result.error)
            try:
                record_failure(save_directory, item_id, subreddit_name, category, result.error or "Unknown error")
            except Exception:
                logger.exception("Could not persist failure for %s", item_id)
            continue
    
        processed_count += 1
        total_media_size += int(result.media_size or 0)
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            logger.warning("Could not stat saved archive file %s", file_path)
        try:
            clear_failure(save_directory, item_id, subreddit_name, category)
        except Exception:
            logger.exception("Could not clear previous failure for %s", item_id)
        handle_dynamic_sleep(comment)

    return processed_count, skipped_count, failed_count, total_size, total_media_size

def _process_mixed_items(
    items,
    save_directory,
    existing_files,
    created_dirs_cache,
    file_log,
    ignore_tls_errors,
    sub_category="SAVED_POST",
    comment_category="SAVED_COMMENT",
    unsave=False,
    tqdm_desc="Processing Items",
    tqdm_position=0,
):
    processed_count = skipped_count = failed_count = total_size = total_media_size = 0
    for item in tqdm(items, desc=tqdm_desc, position=tqdm_position, leave=True):
        if isinstance(item, Submission):
            category, save_fn = sub_category, save_submission
        elif isinstance(item, Comment):
            category, save_fn = comment_category, save_comment_and_context
        else:
            logger.warning("Skipping unsupported Reddit item type: %s", type(item).__name__)
            failed_count += 1
            continue
        path_result = create_reddit_file_path(
            save_directory, item.subreddit.display_name, category, item.id
        )
        if not path_result.is_safe:
            logger.error("Unsafe path for item %s: %s", item.id, path_result.issues)
            failed_count += 1
            try:
                record_failure(save_directory, item.id, item.subreddit.display_name, category,
                               f"Unsafe archive path: {path_result.issues}")
            except Exception:
                logger.exception("Could not record unsafe path failure for %s", item.id)
            continue
        file_path = path_result.safe_path

        result = save_to_file(
            item,
            file_path,
            save_fn,
            existing_files,
            file_log,
            save_directory,
            created_dirs_cache,
            category=category,
            unsave=False,
            ignore_tls_errors=ignore_tls_errors,
        )
        item_id = str(getattr(item, "id", "unknown"))
        subreddit_name = str(getattr(getattr(item, "subreddit", None), "display_name", "unknown"))
        if result.status is SaveStatus.SKIPPED:
            try:
                clear_failure(save_directory, item_id, subreddit_name, category)
            except Exception:
                logger.exception("Could not clear previous failure for %s", item_id)
            skipped_count += 1
            continue
        if result.status is SaveStatus.FAILED:
            failed_count += 1
            logger.error("Item %s was not archived: %s", item_id, result.error)
            try:
                record_failure(save_directory, item_id, subreddit_name, category, result.error or "Unknown error")
            except Exception:
                logger.exception("Could not persist failure for %s", item_id)
            continue
    
        processed_count += 1
        total_media_size += int(result.media_size or 0)
        try:
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)
        except OSError:
            logger.warning("Could not stat saved archive file %s", file_path)
        try:
            clear_failure(save_directory, item_id, subreddit_name, category)
        except Exception:
            logger.exception("Could not clear previous failure for %s", item_id)
        handle_dynamic_sleep(item)

    return processed_count, skipped_count, failed_count, total_size, total_media_size

def save_user_activity(reddit, save_directory, file_log, unsave=False):
    """Archive Reddit user activity and return explicit saved/skipped/failed totals."""
    if unsave:
        raise ValueError(
            "Automatic unsaving is disabled until a verified post-upload commit stage exists."
        )
    ignore_tls_errors = get_ignore_tls_errors()

    active_check_type = _get_check_type()
    if active_check_type == "LOG":
        print("Check type is LOG. Using JSON log plus the on-disk archive index.")
        existing_files = get_existing_files_from_log(file_log)
        existing_files.update(get_existing_files_from_dir(save_directory))
    elif active_check_type == "DIR":
        print("Check type is DIR. Using directory scan to find existing files.")
        existing_files = get_existing_files_from_dir(save_directory)
    else:
        raise ValueError(f"Unknown check_type: {active_check_type}")

    created_dirs_cache = set()
    shared_args = dict(
        save_directory=save_directory,
        existing_files=existing_files,
        created_dirs_cache=created_dirs_cache,
        file_log=file_log,
        ignore_tls_errors=ignore_tls_errors,
    )
    active_save_type = _get_save_type()

    if active_save_type == "ALL":
        endpoints = [
            ("submissions", "submission"), ("comments", "comment"),
            ("saved", "saved"), ("upvoted", "upvoted"),
        ]
        fetched = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                label: pool.submit(_fetch_items, _clone_reddit(reddit), method, 1000, label)
                for method, label in endpoints
            }
            for label, future in futures.items():
                fetched[label] = future.result()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(_process_submissions_batch, fetched["submission"], category="POST",
                            tqdm_desc="Submissions", tqdm_position=0, **shared_args),
                pool.submit(_process_comments_batch, fetched["comment"], category="COMMENT",
                            tqdm_desc="Comments", tqdm_position=1, **shared_args),
                pool.submit(_process_mixed_items, fetched["saved"], sub_category="SAVED_POST",
                            comment_category="SAVED_COMMENT", tqdm_desc="Saved Items",
                            tqdm_position=2, **shared_args),
                pool.submit(_process_mixed_items, fetched["upvoted"], sub_category="UPVOTE_POST",
                            comment_category="UPVOTE_COMMENT", tqdm_desc="Upvoted Items",
                            tqdm_position=3, **shared_args),
            ]
            results = [future.result() for future in futures]
        totals = _merge_results(*results)

    elif active_save_type == "ACTIVITY":
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_sub = pool.submit(_fetch_items, _clone_reddit(reddit), "submissions", 1000, "submission")
            f_com = pool.submit(_fetch_items, _clone_reddit(reddit), "comments", 1000, "comment")
            submissions, comments = f_sub.result(), f_com.result()
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_process_submissions_batch, submissions, category="POST",
                             tqdm_desc="Submissions", tqdm_position=0, **shared_args)
            f2 = pool.submit(_process_comments_batch, comments, category="COMMENT",
                             tqdm_desc="Comments", tqdm_position=1, **shared_args)
            totals = _merge_results(f1.result(), f2.result())

    elif active_save_type == "SAVED":
        saved_items = _fetch_items(reddit, "saved", 1000, "saved")
        totals = _process_mixed_items(
            saved_items, sub_category="SAVED_POST", comment_category="SAVED_COMMENT",
            tqdm_desc="Saved Items", **shared_args,
        )
    elif active_save_type == "UPVOTED":
        upvoted_items = _fetch_items(reddit, "upvoted", 1000, "upvoted")
        totals = _process_mixed_items(
            upvoted_items, sub_category="UPVOTE_POST", comment_category="UPVOTE_COMMENT",
            tqdm_desc="Upvoted Items", **shared_args,
        )
    else:
        raise ValueError(f"Unknown save_type: {active_save_type}")

    save_file_log(file_log, save_directory)
    return totals
