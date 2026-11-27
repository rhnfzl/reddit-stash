#!/usr/bin/env python3
"""Rebuild file_log.json from the actual Reddit-Stash Markdown archive."""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from utils.archive_index import build_file_log
from utils.log_utils import save_file_log


def rebuild(archive: str, apply: bool = False) -> tuple[int, str | None]:
    root = Path(archive).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Archive directory does not exist: {root}")
    entries = build_file_log(root)
    log_path = root / "file_log.json"
    print(f"Archive: {root}")
    print(f"Recognized Markdown files: {len(entries)}")
    print(f"Target log: {log_path}")
    if not apply:
        print("DRY RUN: no changes made. Re-run with --apply to write the rebuilt log.")
        return len(entries), None

    backup_path = None
    if log_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = root / f"file_log.json.backup-{stamp}"
        shutil.copy2(log_path, backup)
        backup_path = str(backup)
        print(f"Existing log backed up to: {backup}")

    save_file_log(entries, root)
    print(f"Rebuilt log with {len(entries)} entries.")
    return len(entries), backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default=None, help="Archive directory (defaults to Settings.save_directory)")
    parser.add_argument("--apply", action="store_true", help="Write the rebuilt log; without this, only preview")
    args = parser.parse_args()

    archive = args.archive
    if not archive:
        import configparser
        from utils.config_paths import get_settings_file_path
        config = configparser.ConfigParser()
        read = config.read(get_settings_file_path())
        if not read:
            parser.error("Could not read settings; supply --archive explicitly")
        archive = config.get("Settings", "save_directory", fallback="reddit_dropbox/")

    try:
        rebuild(archive, apply=args.apply)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
