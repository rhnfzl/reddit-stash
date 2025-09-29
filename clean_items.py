#!/usr/bin/env python3
"""
Selective cleanup script for reddit folder and file_log.json.
Allows cleaning specific types of items (upvoted, saved, comments, submissions).
"""

import os
import json

def clean_items(reddit_dir="reddit", file_log_path="reddit/file_log.json",
                upvoted=False, saved=False, comments=False, submissions=False):
    """
    Remove specific types of items (files and log entries) while preserving others.

    Args:
        reddit_dir: Path to the reddit directory
        file_log_path: Path to the file_log.json file
        upvoted: Remove upvoted items (UPVOTE_POST_, UPVOTE_COMMENT_)
        saved: Remove saved items (SAVED_POST_, SAVED_COMMENT_)
        comments: Remove comment items (COMMENT_, but preserve SAVED_COMMENT_, UPVOTE_COMMENT_)
        submissions: Remove submission items (POST_, but preserve SAVED_POST_, UPVOTE_POST_)
    """

    removed_files = 0
    removed_log_entries = 0

    # Determine what to clean based on arguments
    prefixes_to_remove = []
    cleanup_types = []

    if upvoted:
        prefixes_to_remove.extend(["UPVOTE_POST_", "UPVOTE_COMMENT_"])
        cleanup_types.append("upvoted items")

    if saved:
        prefixes_to_remove.extend(["SAVED_POST_", "SAVED_COMMENT_"])
        cleanup_types.append("saved items")

    if comments:
        prefixes_to_remove.append("COMMENT_")
        cleanup_types.append("user comments")

    if submissions:
        prefixes_to_remove.append("POST_")
        cleanup_types.append("user submissions")

    if not cleanup_types:
        print("❌ No cleanup type specified. Use --upvoted, --saved, --comments, or --submissions")
        return 0, 0

    print(f"🧹 Cleaning {', '.join(cleanup_types)} from reddit folder...")
    print(f"📁 Reddit directory: {reddit_dir}")
    print(f"📄 File log: {file_log_path}")
    print(f"🎯 Target prefixes: {', '.join(prefixes_to_remove)}")
    print("-" * 60)

    # 1. Remove files with specified prefixes
    if os.path.exists(reddit_dir):
        for root, dirs, files in os.walk(reddit_dir):
            for file in files:
                # Check if file starts with any of the target prefixes
                if any(file.startswith(prefix) for prefix in prefixes_to_remove):
                    file_path = os.path.join(root, file)
                    try:
                        os.remove(file_path)
                        removed_files += 1
                        print(f"  ❌ Removed: {os.path.relpath(file_path, reddit_dir)}")
                    except Exception as e:
                        print(f"  ⚠️  Error removing {file_path}: {e}")

    # 2. Clean file_log.json entries
    if os.path.exists(file_log_path):
        try:
            with open(file_log_path, 'r') as f:
                file_log = json.load(f)

            original_count = len(file_log)
            cleaned_log = {}

            for key, value in file_log.items():
                should_remove = False

                # Check if this entry should be removed based on file path
                if isinstance(value, dict) and 'file_path' in value:
                    file_path = value['file_path']
                    filename = os.path.basename(file_path)

                    if any(filename.startswith(prefix) for prefix in prefixes_to_remove):
                        should_remove = True

                # Also check if the key contains the target patterns (for category-based keys)
                if upvoted and 'UPVOTE' in key:
                    should_remove = True
                elif saved and any(pattern in key for pattern in ['SAVED_POST', 'SAVED_COMMENT']):
                    should_remove = True
                elif comments and key.endswith('-Submission-COMMENT'):  # User comments
                    should_remove = True
                elif submissions and key.endswith('-Submission-POST'):  # User submissions
                    should_remove = True

                if should_remove:
                    removed_log_entries += 1
                    print(f"  📝 Removed log entry: {key}")
                else:
                    cleaned_log[key] = value

            # Save cleaned log
            with open(file_log_path, 'w') as f:
                json.dump(cleaned_log, f, indent=2)

            print(f"\n📊 File log cleaned: {original_count} → {len(cleaned_log)} entries")

        except Exception as e:
            print(f"⚠️  Error processing file_log.json: {e}")
    else:
        print("⚠️  file_log.json not found")

    # 3. Summary
    print("\n" + "=" * 60)
    print("✅ Cleanup Complete!")
    print(f"   • Files removed: {removed_files}")
    print(f"   • Log entries removed: {removed_log_entries}")
    print(f"   • Cleaned types: {', '.join(cleanup_types)}")

    if removed_files == 0 and removed_log_entries == 0:
        print(f"\n💡 No {', '.join(cleanup_types)} found to clean.")
        print("   This is normal if you haven't run the script yet or")
        print("   if these items were already cleaned.")
    else:
        print("\n🎯 You can now run 'python reddit_stash.py' to re-download these item types.")

    return removed_files, removed_log_entries


def main():
    """Main function to run the cleanup."""
    import argparse

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

    # Handle --all flag
    if args.all:
        args.upvoted = True
        args.saved = True
        args.comments = True
        args.submissions = True

    # Validate at least one type is specified
    if not any([args.upvoted, args.saved, args.comments, args.submissions]):
        parser.error("Must specify at least one item type: --upvoted, --saved, --comments, --submissions, or --all")

    if args.dry_run:
        print("🔍 DRY RUN MODE - No files will be actually removed")
        print("-" * 60)

        # Determine prefixes to check
        prefixes_to_check = []
        types_to_check = []

        if args.upvoted:
            prefixes_to_check.extend(["UPVOTE_POST_", "UPVOTE_COMMENT_"])
            types_to_check.append("upvoted")

        if args.saved:
            prefixes_to_check.extend(["SAVED_POST_", "SAVED_COMMENT_"])
            types_to_check.append("saved")

        if args.comments:
            prefixes_to_check.append("COMMENT_")
            types_to_check.append("comments")

        if args.submissions:
            prefixes_to_check.append("POST_")
            types_to_check.append("submissions")

        print(f"🎯 Would clean: {', '.join(types_to_check)}")
        print(f"📋 Target prefixes: {', '.join(prefixes_to_check)}")
        print()

        # List what would be removed
        reddit_dir = args.reddit_dir
        count = 0

        if os.path.exists(reddit_dir):
            for root, dirs, files in os.walk(reddit_dir):
                for file in files:
                    if any(file.startswith(prefix) for prefix in prefixes_to_check):
                        file_path = os.path.join(root, file)
                        print(f"  Would remove: {os.path.relpath(file_path, reddit_dir)}")
                        count += 1

        print(f"\n📊 Would remove {count} files")

    else:
        # Actually perform the cleanup
        clean_items(
            reddit_dir=args.reddit_dir,
            upvoted=args.upvoted,
            saved=args.saved,
            comments=args.comments,
            submissions=args.submissions
        )


if __name__ == "__main__":
    main()