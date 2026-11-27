from __future__ import annotations

"""Process Reddit GDPR export data without silently losing archive records."""

import logging
import os
import tempfile
from typing import Any, Iterable, Tuple
from urllib.parse import urlparse

import pandas as pd
from tqdm import tqdm

from utils.archive_index import canonical_item_key
from utils.env_config import get_ignore_tls_errors
from utils.file_operations import save_to_file
from utils.failure_tracker import clear_failure, record_failure
from utils.log_utils import is_file_logged, log_file
from utils.path_security import create_reddit_file_path
from utils.praw_helpers import RecoveredItem
from utils.save_result import SaveStatus
from utils.save_utils import (
    create_recovery_metadata_markdown,
    save_comment_and_context,
    save_submission,
)
from utils.time_utilities import dynamic_sleep

logger = logging.getLogger(__name__)


def get_gdpr_directory(save_directory: str) -> str:
    """Return the path to the GDPR data directory."""
    return os.path.join(save_directory, "gdpr_data")


def _read_csv(path: str) -> pd.DataFrame:
    """Read export IDs as strings so leading zeros and IDs are preserved."""
    return pd.read_csv(path, dtype=str).fillna("")


def _extract_subreddit_from_permalink(permalink: str) -> str:
    """Return the legacy r_<name> folder token from relative or absolute URLs."""
    if permalink:
        value = str(permalink).strip()
        parsed = urlparse(value)
        path = parsed.path if parsed.scheme or parsed.netloc else value
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0].lower() == "r" and parts[1]:
            return f"r_{parts[1]}"
    return "r_unknown"


def _normalize_direction(direction: str) -> str:
    raw = str(direction).strip().lower()
    if raw in {"1", "up", "upvote", "upvoted", "true"}:
        return "upvote"
    if raw in {"-1", "down", "downvote", "downvoted", "false"}:
        return "downvote"
    return raw or "unknown"


def _write_csv_only_record(
    *,
    record_type: str,
    record_id: str,
    permalink: str,
    body_lines: Iterable[str],
    save_directory: str,
    existing_files: set[str],
    file_log: dict[str, Any],
    created_dirs_cache: set[str],
) -> Tuple[int, int]:
    """Atomically write a minimal GDPR Markdown record with a canonical key."""
    normalized_type = str(record_type).upper().strip()
    if normalized_type not in {"POST", "COMMENT", "VOTE"}:
        raise ValueError(f"Unsupported GDPR record type: {record_type!r}")
    record_id = str(record_id).strip()
    if not record_id:
        return 0, 0

    subreddit_folder = _extract_subreddit_from_permalink(permalink)
    subreddit_key = subreddit_folder[2:] if subreddit_folder.startswith("r_") else subreddit_folder
    category = f"GDPR_{normalized_type}"
    unique_key = canonical_item_key(record_id, subreddit_key or "unknown", category)
    if unique_key in existing_files or is_file_logged(file_log, unique_key):
        return 0, 0

    path_result = create_reddit_file_path(
        save_directory, subreddit_folder, category, record_id
    )
    if not path_result.is_safe:
        raise ValueError(
            f"Unsafe GDPR {normalized_type.lower()} path for {record_id}: {path_result.issues}"
        )

    file_path = path_result.safe_path
    directory = os.path.dirname(file_path)
    if directory not in created_dirs_cache:
        os.makedirs(directory, exist_ok=True)
        created_dirs_cache.add(directory)

    reddit_url = (
        f"https://www.reddit.com{permalink}"
        if permalink else "https://www.reddit.com/"
    )
    content = "\n".join(body_lines).rstrip() + "\n"
    fd, temporary_path = tempfile.mkstemp(
        prefix=os.path.basename(file_path) + ".",
        suffix=".part",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Never overwrite an existing archive item that was not indexed.
        if os.path.exists(file_path):
            raise FileExistsError(
                f"GDPR destination already exists but is not indexed: {file_path}"
            )
        os.replace(temporary_path, file_path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise

    file_size = os.path.getsize(file_path)
    log_file(
        file_log,
        unique_key,
        {
            "id": record_id,
            "subreddit": subreddit_key or "unknown",
            "type": "Comment" if normalized_type == "COMMENT" else "Submission",
            "category": category,
            "file_path": file_path,
            "permalink": reddit_url,
            "gdpr_record_type": normalized_type,
        },
        save_directory,
    )
    existing_files.add(unique_key)
    return 1, file_size


def _save_csv_only_post(row, save_directory, existing_files, file_log, created_dirs_cache):
    post_id = str(row.get("id", "")).strip()
    if not post_id:
        return 0, 0
    permalink = str(row.get("permalink", "")).strip()
    reddit_url = (
        f"https://www.reddit.com{permalink}"
        if permalink else f"https://www.reddit.com/comments/{post_id}"
    )
    return _write_csv_only_record(
        record_type="POST",
        record_id=post_id,
        permalink=permalink,
        body_lines=[
            f"# GDPR Export Post: {post_id}", "",
            f"**Reddit Link:** [{reddit_url}]({reddit_url})", "",
            f"**Post ID:** {post_id}", "", "---", "",
            "*This is a CSV-only export (no API enrichment required for this record).",
            "Content was not fetched from Reddit.",
            "Visit the link above to view the full post.*", "",
        ],
        save_directory=save_directory,
        existing_files=existing_files,
        file_log=file_log,
        created_dirs_cache=created_dirs_cache,
    )


def _save_csv_only_comment(row, save_directory, existing_files, file_log, created_dirs_cache):
    comment_id = str(row.get("id", "")).strip()
    if not comment_id:
        return 0, 0
    permalink = str(row.get("permalink", "")).strip()
    reddit_url = (
        f"https://www.reddit.com{permalink}"
        if permalink else f"https://www.reddit.com/comments/{comment_id}"
    )
    return _write_csv_only_record(
        record_type="COMMENT",
        record_id=comment_id,
        permalink=permalink,
        body_lines=[
            f"# GDPR Export Comment: {comment_id}", "",
            f"**Reddit Link:** [{reddit_url}]({reddit_url})", "",
            f"**Comment ID:** {comment_id}", "", "---", "",
            "*This is a CSV-only export (no API enrichment required for this record).",
            "Content was not fetched from Reddit.",
            "Visit the link above to view the full comment.*", "",
        ],
        save_directory=save_directory,
        existing_files=existing_files,
        file_log=file_log,
        created_dirs_cache=created_dirs_cache,
    )


def _save_csv_only_vote(row, save_directory, existing_files, file_log, created_dirs_cache):
    vote_id = str(row.get("id", "")).strip()
    if not vote_id:
        return 0, 0
    permalink = str(row.get("permalink", "")).strip()
    direction = _normalize_direction(row.get("direction", ""))
    reddit_url = (
        f"https://www.reddit.com{permalink}"
        if permalink else "https://www.reddit.com/"
    )
    return _write_csv_only_record(
        record_type="VOTE",
        record_id=vote_id,
        permalink=permalink,
        body_lines=[
            f"# GDPR Export Vote: {vote_id}", "",
            f"**Reddit Link:** [{reddit_url}]({reddit_url})", "",
            f"**Vote ID:** {vote_id}", f"**Direction:** {direction}",
            "", "---", "",
            "*This record comes from Reddit's GDPR vote export.",
            "It stores the vote metadata and the linked Reddit item, not the content itself.*", "",
        ],
        save_directory=save_directory,
        existing_files=existing_files,
        file_log=file_log,
        created_dirs_cache=created_dirs_cache,
    )


def _process_csv(
    *,
    csv_path: str,
    csv_label: str,
    record_type: str,
    save_directory: str,
    existing_files: set[str],
    file_log: dict[str, Any],
    created_dirs_cache: set[str],
    csv_only_mode: bool,
    reddit,
) -> Tuple[int, int, int, int]:
    """Return (processed_count, skipped_count, failed_count, Markdown_bytes)."""
    if not os.path.exists(csv_path):
        return 0, 0, 0, 0

    print(f"\nProcessing {csv_label} from GDPR export...")
    dataframe = _read_csv(csv_path)
    processed_count = skipped_count = failed_count = total_size = 0

    for _, row in tqdm(
        dataframe.iterrows(), total=len(dataframe), desc=f"Processing GDPR {csv_label}"
    ):
        item_id = str(row.get("id", "")).strip() or "[unknown]"
        try:
            if record_type == "POST":
                if csv_only_mode:
                    count, size = _save_csv_only_post(
                        row, save_directory, existing_files, file_log, created_dirs_cache
                    )
                    if count:
                        processed_count += count
                        total_size += size
                    else:
                        skipped_count += 1
                    if item_id != "[unknown]":
                        clear_failure(save_directory, item_id, _extract_subreddit_from_permalink(str(row.get("permalink", ""))).removeprefix("r_"), "GDPR_POST")
                    continue

                post_id = str(row.get("id", "")).strip()
                if not post_id:
                    skipped_count += 1
                    continue
                submission = reddit.submission(id=post_id)
                path_result = create_reddit_file_path(
                    save_directory,
                    submission.subreddit.display_name,
                    "GDPR_POST",
                    submission.id,
                )
                if not path_result.is_safe:
                    raise ValueError(
                        f"Unsafe GDPR submission path: {path_result.issues}"
                    )
                result = save_to_file(
                    submission,
                    path_result.safe_path,
                    save_submission,
                    existing_files,
                    file_log,
                    save_directory,
                    created_dirs_cache,
                    category="GDPR_POST",
                    unsave=False,
                    ignore_tls_errors=get_ignore_tls_errors(),
                )
                if result.status is SaveStatus.SKIPPED:
                    skipped_count += 1
                    clear_failure(save_directory, post_id, submission.subreddit.display_name, "GDPR_POST")
                    continue
                if result.status is SaveStatus.FAILED:
                    failed_count += 1
                    logger.error("GDPR post %s failed to archive: %s", post_id, result.error)
                    record_failure(save_directory, post_id, submission.subreddit.display_name, "GDPR_POST", result.error or "Unknown error")
                    continue
                processed_count += 1
                clear_failure(save_directory, post_id, submission.subreddit.display_name, "GDPR_POST")
                total_size += os.path.getsize(path_result.safe_path)
                dynamic_sleep(
                    len(submission.selftext)
                    if submission.is_self and submission.selftext else 0
                )

            elif record_type == "COMMENT":
                if csv_only_mode:
                    count, size = _save_csv_only_comment(
                        row, save_directory, existing_files, file_log, created_dirs_cache
                    )
                    if count:
                        processed_count += count
                        total_size += size
                    else:
                        skipped_count += 1
                    if item_id != "[unknown]":
                        clear_failure(save_directory, item_id, _extract_subreddit_from_permalink(str(row.get("permalink", ""))).removeprefix("r_"), "GDPR_COMMENT")
                    continue

                comment_id = str(row.get("id", "")).strip()
                if not comment_id:
                    skipped_count += 1
                    continue
                comment = reddit.comment(id=comment_id)
                path_result = create_reddit_file_path(
                    save_directory,
                    comment.subreddit.display_name,
                    "GDPR_COMMENT",
                    comment.id,
                )
                if not path_result.is_safe:
                    raise ValueError(
                        f"Unsafe GDPR comment path: {path_result.issues}"
                    )
                result = save_to_file(
                    comment,
                    path_result.safe_path,
                    save_comment_and_context,
                    existing_files,
                    file_log,
                    save_directory,
                    created_dirs_cache,
                    category="GDPR_COMMENT",
                    unsave=False,
                    ignore_tls_errors=get_ignore_tls_errors(),
                )
                if result.status is SaveStatus.SKIPPED:
                    skipped_count += 1
                    clear_failure(save_directory, comment_id, comment.subreddit.display_name, "GDPR_COMMENT")
                    continue
                if result.status is SaveStatus.FAILED:
                    failed_count += 1
                    logger.error("GDPR comment %s failed to archive: %s", comment_id, result.error)
                    record_failure(save_directory, comment_id, comment.subreddit.display_name, "GDPR_COMMENT", result.error or "Unknown error")
                    continue
                processed_count += 1
                clear_failure(save_directory, comment_id, comment.subreddit.display_name, "GDPR_COMMENT")
                total_size += os.path.getsize(path_result.safe_path)
                dynamic_sleep(len(comment.body) if comment.body else 0)

            elif record_type == "VOTE":
                count, size = _save_csv_only_vote(
                    row, save_directory, existing_files, file_log, created_dirs_cache
                )
                if count:
                    processed_count += count
                    total_size += size
                else:
                    skipped_count += 1
                if item_id != "[unknown]":
                    clear_failure(save_directory, item_id, _extract_subreddit_from_permalink(str(row.get("permalink", ""))).removeprefix("r_"), "GDPR_VOTE")
            else:
                raise ValueError(f"Unknown GDPR record type: {record_type!r}")

        except Exception as exc:
            failed_count += 1
            logger.exception("Failed processing GDPR %s %s: %s", record_type, item_id, exc)
            if item_id != "[unknown]":
                subreddit = _extract_subreddit_from_permalink(str(row.get("permalink", ""))).removeprefix("r_")
                try:
                    record_failure(save_directory, item_id, subreddit, f"GDPR_{record_type}", str(exc))
                except Exception:
                    logger.exception("Could not persist GDPR failure journal entry for %s", item_id)

    return processed_count, skipped_count, failed_count, total_size


def process_gdpr_export(
    reddit,
    save_directory: str,
    existing_files: set[str],
    created_dirs_cache: set[str],
    file_log: dict[str, Any],
) -> Tuple[int, int, int, int]:
    """Process saved posts/comments/votes and report errors distinctly."""
    processed_count = skipped_count = failed_count = total_size = 0
    csv_only_mode = reddit is None
    if csv_only_mode:
        print("Running in CSV-only mode (no API credentials).")
        print("Saving post/comment/vote links from GDPR export without fetching full content.")

    gdpr_dir = get_gdpr_directory(save_directory)
    jobs = (
        ("saved_posts.csv", "saved posts", "POST", csv_only_mode, reddit),
        ("saved_comments.csv", "saved comments", "COMMENT", csv_only_mode, reddit),
        ("post_votes.csv", "post votes", "VOTE", True, None),
    )
    for filename, label, record_type, only_mode, client in jobs:
        result = _process_csv(
            csv_path=os.path.join(gdpr_dir, filename),
            csv_label=label,
            record_type=record_type,
            save_directory=save_directory,
            existing_files=existing_files,
            file_log=file_log,
            created_dirs_cache=created_dirs_cache,
            csv_only_mode=only_mode,
            reddit=client,
        )
        processed, skipped, failed, size = result
        processed_count += processed
        skipped_count += skipped
        failed_count += failed
        total_size += size

    return processed_count, skipped_count, failed_count, total_size
