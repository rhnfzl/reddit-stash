#!/usr/bin/env python3
"""
Selective cleanup script for reddit folder and file_log.json.
Allows cleaning specific types of items (upvoted, saved, comments, submissions).
"""

import argparse
import os
import sys
from collections.abc import Sequence
from typing import NamedTuple

from utils.log_utils import configure_logging, get_log_file_path, load_file_log, save_file_log


class ItemType(NamedTuple):
    label: str
    file_prefixes: tuple[str, ...]
    log_key_suffixes: tuple[str, ...]


ITEM_TYPES = {
    "upvoted": ItemType("upvoted items", ("UPVOTE_POST_", "UPVOTE_COMMENT_"), ("-UPVOTE_POST", "-UPVOTE_COMMENT")),
    "saved": ItemType("saved items", ("SAVED_POST_", "SAVED_COMMENT_"), ("-SAVED_POST", "-SAVED_COMMENT")),
    "comments": ItemType("user comments", ("COMMENT_",), ("-Comment-COMMENT", "-Submission-COMMENT")),
    # Not plain "-POST": enriched GDPR comments are logged as "-Comment-POST" and must stay.
    "submissions": ItemType("user submissions", ("POST_",), ("-Submission-POST",)),
}


def _clean_log(reddit_dir: str, prefixes: tuple[str, ...], suffixes: tuple[str, ...], dry_run: bool) -> int:
    """Remove the selected file_log.json entries and return how many matched."""
    if not os.path.exists(get_log_file_path(reddit_dir)):
        print("⚠️  file_log.json not found")
        return 0

    file_log = load_file_log(reddit_dir)
    kept_log = {}
    for key, value in file_log.items():
        file_path = (value.get('file_path') or '') if isinstance(value, dict) else ''
        if key.endswith(suffixes) or os.path.basename(file_path).startswith(prefixes):
            print(f"  📝 {'Would remove' if dry_run else 'Removed'} log entry: {key}")
        else:
            kept_log[key] = value

    if not dry_run:
        save_file_log(kept_log, reddit_dir)
    return len(file_log) - len(kept_log)


def _remove_files(reddit_dir: str, prefixes: tuple[str, ...], dry_run: bool) -> tuple[int, int]:
    """Remove the selected files and return (removed, failed)."""
    removed = failed = 0

    def count_unreadable_dir(error: OSError) -> None:
        nonlocal failed
        if isinstance(error, FileNotFoundError):  # a missing directory holds nothing to remove
            return
        failed += 1
        print(f"  ⚠️  Error reading {error.filename}: {error}")

    for root, _dirs, files in os.walk(reddit_dir, onerror=count_unreadable_dir):
        for file in files:
            if not file.startswith(prefixes):
                continue
            file_path = os.path.join(root, file)
            try:
                if not dry_run:
                    os.remove(file_path)
            except OSError as e:
                failed += 1
                print(f"  ⚠️  Error removing {file_path}: {e}")
            else:
                removed += 1
                print(f"  ❌ {'Would remove' if dry_run else 'Removed'}: {os.path.relpath(file_path, reddit_dir)}")
    return removed, failed


def clean_items(item_types: Sequence[str], reddit_dir: str = "reddit", dry_run: bool = False) -> tuple[int, int, int]:
    """
    Remove specific types of items (files and log entries) while preserving others.

    Args:
        item_types: Keys of ITEM_TYPES to remove
        reddit_dir: Path to the reddit directory, which holds file_log.json
        dry_run: Report what would be removed without changing anything

    Returns:
        (files removed, log entries removed, errors), counting matches in a dry run
    """
    selected = [ITEM_TYPES[name] for name in item_types]
    labels = ', '.join(item.label for item in selected)
    prefixes = tuple(prefix for item in selected for prefix in item.file_prefixes)
    suffixes = tuple(suffix for item in selected for suffix in item.log_key_suffixes)

    if dry_run:
        print("🔍 DRY RUN MODE - Nothing will be actually removed")
    print(f"🧹 Cleaning {labels} from reddit folder...")
    print(f"📁 Reddit directory: {reddit_dir}")
    print(f"🎯 Target prefixes: {', '.join(prefixes)}")
    print("-" * 60)

    # Log first: a failure here leaves the files in place, never deleted files that are still logged.
    try:
        log_entries = _clean_log(reddit_dir, prefixes, suffixes, dry_run)
    except (OSError, ValueError, AttributeError, TypeError) as e:  # Last two: valid JSON, but not a log's shape
        print(f"⚠️  Error processing file_log.json: {e}")
        print("\n❌ Cleanup failed: no files were removed.")
        return 0, 0, 1

    files, errors = _remove_files(reddit_dir, prefixes, dry_run)

    print("\n" + "=" * 60)
    if errors:
        print(f"❌ Cleanup finished with {errors} error(s). Re-run to retry.")
    else:
        print("🔍 Dry Run Complete!" if dry_run else "✅ Cleanup Complete!")
    print(f"   • Files {'to remove' if dry_run else 'removed'}: {files}")
    print(f"   • Log entries {'to remove' if dry_run else 'removed'}: {log_entries}")
    print(f"   • Cleaned types: {labels}")

    if files == 0 and log_entries == 0 and not errors:
        print(f"\n💡 No {labels} found to clean.")
        print("   This is normal if you haven't run the script yet or")
        print("   if these items were already cleaned.")
    elif not dry_run and not errors:
        print("\n🎯 You can now run 'python reddit_stash.py' to re-download these item types.")

    return files, log_entries, errors


def main() -> None:
    """Main function to run the cleanup."""
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Selectively remove specific types of items from reddit folder",
        epilog="""
Examples:
  python clean_items.py --upvoted                    # Clean only upvoted items
  python clean_items.py --saved --upvoted           # Clean saved and upvoted items
  python clean_items.py --comments --submissions    # Clean user posts and comments
  python clean_items.py --upvoted --dry-run         # Preview upvoted items cleanup
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--reddit-dir",
        default="reddit",
        help="Path to reddit directory (default: reddit)"
    )

    # Item type arguments
    parser.add_argument(
        "--upvoted",
        action="store_true",
        help="Remove upvoted items (UPVOTE_POST_, UPVOTE_COMMENT_)"
    )

    parser.add_argument(
        "--saved",
        action="store_true",
        help="Remove saved items (SAVED_POST_, SAVED_COMMENT_)"
    )

    parser.add_argument(
        "--comments",
        action="store_true",
        help="Remove user comments (COMMENT_) - excludes saved/upvoted comments"
    )

    parser.add_argument(
        "--submissions",
        action="store_true",
        help="Remove user submissions (POST_) - excludes saved/upvoted posts"
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Remove all types (equivalent to --upvoted --saved --comments --submissions)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without actually removing"
    )

    args = parser.parse_args()

    item_types = [name for name in ITEM_TYPES if args.all or getattr(args, name)]
    if not item_types:
        parser.error("Must specify at least one item type: --upvoted, --saved, --comments, --submissions, or --all")

    _files, _log_entries, errors = clean_items(item_types, reddit_dir=args.reddit_dir, dry_run=args.dry_run)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
