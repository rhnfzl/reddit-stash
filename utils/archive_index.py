"""Canonical indexing helpers for the Markdown archive."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterator, Tuple

CATEGORY_PREFIXES = (
    "SAVED_COMMENT_",
    "SAVED_POST_",
    "UPVOTE_COMMENT_",
    "UPVOTE_POST_",
    "GDPR_COMMENT_",
    "GDPR_POST_",
    "GDPR_VOTE_",
    "COMMENT_",
    "POST_",
)


def item_type_for_category(category: str) -> str:
    category = (category or "").upper()
    return "Comment" if "COMMENT" in category else "Submission"


def canonical_item_key(item_id: str, subreddit: str, category: str) -> str:
    """Return the sole supported dedupe key format.

    Example: abc123-AskReddit-Submission-POST
    """
    safe_id = str(item_id).strip()
    safe_subreddit = str(subreddit or "unknown").strip()
    normalized_category = str(category or "POST").strip().upper()
    if not safe_id or not safe_subreddit:
        raise ValueError("item_id and subreddit must be non-empty")
    if normalized_category not in {
        "POST", "COMMENT", "SAVED_POST", "SAVED_COMMENT",
        "UPVOTE_POST", "UPVOTE_COMMENT", "GDPR_POST", "GDPR_COMMENT",
        "GDPR_VOTE",
    }:
        raise ValueError(f"Unsupported archive category: {category!r}")
    return f"{safe_id}-{safe_subreddit}-{item_type_for_category(normalized_category)}-{normalized_category}"


def category_and_id(filename: str):
    stem = Path(filename).stem
    for prefix in CATEGORY_PREFIXES:
        if stem.startswith(prefix):
            item_id = stem[len(prefix):]
            if item_id:
                return prefix[:-1], item_id
    return None



def subreddit_from_archive_path(path: Path, category: str) -> str:
    """Return the subreddit represented by an archive file's parent folder.

    Old CSV-only GDPR imports used an `r_<subreddit>` folder prefix and wrote
    a `# GDPR Export ...` heading. Only those legacy files should have one
    leading `r_` stripped. Real subreddit names can themselves start with
    `r_`, and API-enriched GDPR records use the exact subreddit folder name.
    """
    subreddit = path.parent.name or "unknown"
    if not str(category).upper().startswith("GDPR_") or not subreddit.startswith("r_"):
        return subreddit
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline().strip()
    except OSError:
        return subreddit
    if first_line.startswith("# GDPR Export "):
        return subreddit[2:] or "unknown"
    return subreddit


def iter_archive_index(archive_dir: str | os.PathLike[str]) -> Iterator[Tuple[str, Dict[str, str]]]:
    """Yield (dedupe_key, log_info) from archived Reddit Markdown files."""
    root = Path(archive_dir).expanduser().resolve()
    if not root.exists():
        return
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        parsed = category_and_id(path.name)
        if not parsed:
            continue
        category, item_id = parsed
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) < 2:
            subreddit = "unknown"
        else:
            subreddit = subreddit_from_archive_path(path, category)
        key = canonical_item_key(item_id, subreddit, category)
        yield key, {
            "id": item_id,
            "subreddit": subreddit,
            "type": item_type_for_category(category),
            "category": category,
            "file_path": relative.as_posix(),
        }


def build_file_log(archive_dir: str | os.PathLike[str]) -> dict:
    return {key: info for key, info in iter_archive_index(archive_dir)}
