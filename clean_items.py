#!/usr/bin/env python3
"""Safely remove selected archive categories and their matching log entries.

No deletion occurs unless --apply is supplied. The archive path defaults to
Settings.save_directory from the selected settings file, not a hard-coded
`reddit/` directory.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from utils.config_paths import get_settings_file_path
from utils.log_utils import load_file_log
from utils.archive_index import iter_archive_index

PREFIX_GROUPS = {
    "upvoted": ("UPVOTE_POST_", "UPVOTE_COMMENT_"),
    "saved": ("SAVED_POST_", "SAVED_COMMENT_"),
    "comments": ("COMMENT_", "GDPR_COMMENT_"),
    "submissions": ("POST_", "GDPR_POST_"),
    "votes": ("GDPR_VOTE_",),
}


def configured_archive_dir() -> Path:
    parser = configparser.ConfigParser()
    if not parser.read(get_settings_file_path()):
        raise FileNotFoundError(
            f"Could not read settings file {get_settings_file_path()}; "
            "pass --reddit-dir explicitly."
        )
    configured = parser.get("Settings", "save_directory", fallback="reddit_dropbox/")
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def _resolve_log_entry_path(archive_dir: Path, value: object) -> Path | None:
    if not isinstance(value, dict) or not value.get("file_path"):
        return None
    candidate = Path(str(value["file_path"])).expanduser()
    if not candidate.is_absolute():
        candidate = archive_dir / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _selected_prefixes(groups: Iterable[str]) -> tuple[str, ...]:
    prefixes: list[str] = []
    for group in groups:
        prefixes.extend(PREFIX_GROUPS[group])
    return tuple(prefixes)


def _load_selected_log(archive: Path, log_path: Path) -> dict:
    """Load the requested log path; never silently replace corrupt JSON."""
    standard_path = (archive / "file_log.json").resolve()
    if log_path.resolve() == standard_path:
        return load_file_log(archive)
    if not log_path.exists():
        if any(path.is_file() for path in archive.rglob("*.md")):
            raise FileNotFoundError(
                f"Selected file log is missing: {log_path}. Rebuild the archive index "
                "before running destructive cleanup."
            )
        return {}
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot trust custom file log {log_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Custom file log {log_path} must contain a JSON object")
    return payload


def _atomic_write_log(log_path: Path, payload: dict) -> None:
    """Atomically write the exact log path selected by the caller."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=log_path.name + ".", suffix=".tmp", dir=str(log_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, log_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def clean_items(
    reddit_dir: str | os.PathLike[str],
    *,
    file_log_path: str | os.PathLike[str] | None = None,
    upvoted: bool = False,
    saved: bool = False,
    comments: bool = False,
    submissions: bool = False,
    votes: bool = False,
    apply: bool = False,
) -> tuple[int, int]:
    groups = [
        name for name, enabled in (
            ("upvoted", upvoted), ("saved", saved),
            ("comments", comments), ("submissions", submissions), ("votes", votes),
        ) if enabled
    ]
    if not groups:
        raise ValueError("Select at least one of --upvoted, --saved, --comments, --submissions, --votes")

    archive = Path(reddit_dir).expanduser().resolve()
    if not archive.is_dir():
        raise FileNotFoundError(f"Archive directory does not exist: {archive}")
    if file_log_path:
        requested_log = Path(file_log_path).expanduser()
        log_path = (requested_log if requested_log.is_absolute() else archive / requested_log).resolve()
    else:
        log_path = archive / "file_log.json"
    prefixes = _selected_prefixes(groups)
    selected_categories = {
        "upvoted": {"UPVOTE_POST", "UPVOTE_COMMENT"},
        "saved": {"SAVED_POST", "SAVED_COMMENT"},
        "comments": {"COMMENT", "GDPR_COMMENT"},
        "submissions": {"POST", "GDPR_POST"},
        "votes": {"GDPR_VOTE"},
    }
    selected_suffixes = {
        f"-{category}"
        for group in groups
        for category in selected_categories[group]
    }

    # Load before touching files. A corrupt log must stop cleanup rather than
    # produce a partially modified archive with an untrusted index.
    file_log = _load_selected_log(archive, log_path)

    index_by_relative_path = {
        info["file_path"]: key for key, info in iter_archive_index(archive)
    }
    candidates: list[Path] = []
    for path in archive.rglob("*"):
        if path.is_file() and path.name.startswith(prefixes):
            candidates.append(path)
    removed_paths: set[Path] = set()
    removed_keys: set[str] = set()
    failed_keys: set[str] = set()
    removed_files = 0
    for path in candidates:
        relative = path.relative_to(archive).as_posix()
        archive_key = index_by_relative_path.get(relative)
        if not apply:
            print(f"Would remove: {relative}")
            removed_paths.add(path.resolve())
            if archive_key:
                removed_keys.add(archive_key)
            continue
        try:
            path.unlink()
            removed_files += 1
            removed_paths.add(path.resolve())
            if archive_key:
                removed_keys.add(archive_key)
            print(f"Removed: {relative}")
        except OSError as exc:
            if archive_key:
                failed_keys.add(archive_key)
            print(f"Could not remove {relative}: {exc}")

    removed_log_entries = 0
    cleaned_log = {}
    for key, value in file_log.items():
        category_match = isinstance(key, str) and any(key.endswith(suffix) for suffix in selected_suffixes)
        entry_path = _resolve_log_entry_path(archive, value)
        path_match = bool(entry_path and entry_path in removed_paths)
        stale_category_entry = (
            category_match
            and key not in failed_keys
            and (key in removed_keys or entry_path is None or not entry_path.exists())
        )
        if path_match or stale_category_entry:
            removed_log_entries += 1
            continue
        cleaned_log[key] = value

    if apply and removed_log_entries:
        _atomic_write_log(log_path, cleaned_log)

    print(f"Selected categories: {', '.join(groups)}")
    if apply:
        print(f"Deleted files: {removed_files}; removed log entries: {removed_log_entries}")
    else:
        print(f"DRY RUN: {len(removed_paths)} matching files and {removed_log_entries} log entries would be removed.")
        print("No changes made. Re-run with --apply to execute this cleanup.")
    return removed_files if apply else len(removed_paths), removed_log_entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reddit-dir", default=None, help="Archive directory (defaults to Settings.save_directory)")
    parser.add_argument("--file-log", default=None, help="Explicit file_log.json path (defaults to <archive>/file_log.json)")
    parser.add_argument("--upvoted", action="store_true")
    parser.add_argument("--saved", action="store_true")
    parser.add_argument("--comments", action="store_true")
    parser.add_argument("--submissions", action="store_true")
    parser.add_argument("--votes", action="store_true", help="Select GDPR vote records")
    parser.add_argument("--all", action="store_true", help="Select all archive categories")
    parser.add_argument("--apply", action="store_true", help="Actually delete; without this, dry-run only")
    args = parser.parse_args()

    try:
        archive = Path(args.reddit_dir).expanduser().resolve() if args.reddit_dir else configured_archive_dir()
        selected = args.all or any((args.upvoted, args.saved, args.comments, args.submissions, args.votes))
        if not selected:
            parser.error("Select at least one category or --all")
        clean_items(
            archive,
            file_log_path=args.file_log,
            upvoted=args.upvoted or args.all,
            saved=args.saved or args.all,
            comments=args.comments or args.all,
            submissions=args.submissions or args.all,
            votes=args.votes or args.all,
            apply=args.apply,
        )
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
