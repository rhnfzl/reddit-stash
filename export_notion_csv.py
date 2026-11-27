#!/usr/bin/env python3
"""Export a Reddit-Stash SQLite archive to a Notion-importable CSV."""
from __future__ import annotations

import argparse
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def to_iso(timestamp) -> str:
    if timestamp in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(timestamp)


def export(db_path: str, out_csv: str) -> int:
    database = Path(db_path).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"SQLite database not found: {database}")
    output = Path(out_csv).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(database)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        required = {"id", "title", "subreddit", "permalink", "url", "created_utc", "body"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"Database posts table is missing columns: {', '.join(sorted(missing))}")
        rows = conn.execute(
            "SELECT id, title, subreddit, permalink, url, created_utc, body "
            "FROM posts ORDER BY created_utc DESC, id"
        ).fetchall()

    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Reddit ID", "Title", "Subreddit", "Permalink", "URL", "Date", "Excerpt"])
        for item_id, title, subreddit, permalink, url, created_utc, body in rows:
            excerpt = " ".join((body or "").split())[:250]
            writer.writerow([item_id, title or "", subreddit or "", permalink or "", url or "", to_iso(created_utc), excerpt])
    print(f"Wrote {len(rows)} rows to {output}")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="reddit_archive.db")
    parser.add_argument("--out", default="notion_import.csv")
    args = parser.parse_args()
    try:
        export(args.db, args.out)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
