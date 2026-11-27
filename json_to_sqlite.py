#!/usr/bin/env python3
"""Import Reddit-Stash Markdown archives (and legacy Reddit JSON) into SQLite.

The Markdown format is the primary archive format written by reddit_stash.py.
The `posts_fts` table is a normal FTS5 table maintained by SQLite triggers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from utils.archive_index import subreddit_from_archive_path

CATEGORY_PREFIXES = (
    "SAVED_COMMENT_", "SAVED_POST_", "UPVOTE_COMMENT_", "UPVOTE_POST_",
    "GDPR_COMMENT_", "GDPR_POST_", "GDPR_VOTE_", "COMMENT_", "POST_",
)


def iter_archive_files(base_dir: str | os.PathLike[str]) -> Iterator[Path]:
    root = Path(base_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Archive directory does not exist: {root}")
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in {".md", ".json"}:
            yield path


def _category_from_filename(path: Path):
    stem = path.stem
    for prefix in CATEGORY_PREFIXES:
        if stem.startswith(prefix):
            category = prefix[:-1]
            item_id = stem[len(prefix):]
            if item_id:
                return category, item_id
    return None, None


def _first_match(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else default


def _timestamp_to_epoch(value: Any):
    if value in (None, "", "unknown"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(str(value).strip(), fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def normalize_json(obj: dict, path: Path) -> dict | None:
    if not isinstance(obj, dict):
        return None
    item_id = obj.get("id") or obj.get("name")
    if not item_id:
        return None
    subreddit = obj.get("subreddit") or obj.get("subreddit_name_prefixed") or path.parent.name
    subreddit = str(subreddit).removeprefix("r/").removeprefix("/r/")
    item_type = str(obj.get("kind") or obj.get("type") or "post").lower()
    return {
        "id": str(item_id),
        "type": "comment" if "comment" in item_type else "submission",
        "subreddit": subreddit or None,
        "title": str(obj.get("title") or ""),
        "author": str(obj.get("author") or ""),
        "body": str(obj.get("selftext") or obj.get("body") or ""),
        "permalink": obj.get("permalink"),
        "url": obj.get("url"),
        "created_utc": _timestamp_to_epoch(obj.get("created_utc") or obj.get("created")),
        "raw": obj,
    }


def parse_markdown(path: Path, archive_root: Path) -> dict | None:
    category, item_id = _category_from_filename(path)
    if not category or not item_id:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    subreddit = subreddit_from_archive_path(path, category)

    meta_subreddit = _first_match(r"^subreddit:\s*(.+?)\s*$", text)
    if meta_subreddit:
        subreddit = meta_subreddit.replace("/r/", "").replace("r/", "").strip()
    author = _first_match(r"^author:\s*(.+?)\s*$", text)
    timestamp = _first_match(r"^timestamp:\s*(.+?)\s*$", text)
    permalink = _first_match(r"^permalink:\s*(https?://\S+)\s*$", text)
    if not permalink:
        permalink = _first_match(r"\[Link\]\((https?://[^)]+)\)", text)

    title = ""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            break
    if not title and "COMMENT" in category:
        title = _first_match(r"^Comment by\s+(.+?)\s*$", text, "Comment")
    if not title:
        title = path.stem

    # Use the archived text after the title as the searchable body. Keeping the
    # full Markdown preserves context and avoids destructive parsing assumptions.
    body = text
    url = _first_match(r"\*\*Original (?:Image|Video) URL:\*\*\s*\[Link\]\((https?://[^)]+)\)", text)
    created = _timestamp_to_epoch(timestamp)
    relative = path.relative_to(archive_root).as_posix()
    raw = {
        "source_file": relative,
        "category": category,
        "id": item_id,
        "subreddit": subreddit,
        "author": author,
        "timestamp": timestamp,
    }
    return {
        "id": item_id,
        "type": "comment" if "COMMENT" in category else "submission",
        "subreddit": subreddit or None,
        "title": title,
        "author": author,
        "body": body,
        "permalink": permalink or None,
        "url": url or None,
        "created_utc": created,
        "raw": raw,
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            type TEXT,
            subreddit TEXT,
            title TEXT,
            author TEXT,
            body TEXT,
            permalink TEXT,
            url TEXT,
            created_utc INTEGER,
            scraped_utc INTEGER,
            raw_json TEXT
        )
    """)

    # Earlier versions created an external-content FTS table but did not keep it
    # synchronized. Drop that index (not the posts table) and rebuild it safely.
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='posts_fts'").fetchone()
    fts_sql = (row[0] or "").lower() if row else ""
    if row and ("content='posts'" in fts_sql or "content = 'posts'" in fts_sql or "post_id" not in fts_sql):
        conn.execute("DROP TABLE posts_fts")

    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts
        USING fts5(post_id UNINDEXED, title, body)
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subreddit ON posts(subreddit)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON posts(created_utc)")

    # Keep the standalone FTS index synchronized even if posts are edited outside
    # this importer. Triggers are created only after the FTS schema is correct.
    # Remove any legacy triggers before rebuilding the standalone FTS table.
    # An older trigger may reference the previous external-content schema.
    conn.executescript("""
        DROP TRIGGER IF EXISTS posts_ai;
        DROP TRIGGER IF EXISTS posts_ad;
        DROP TRIGGER IF EXISTS posts_au;
        CREATE TRIGGER posts_ai AFTER INSERT ON posts BEGIN
            INSERT INTO posts_fts(post_id, title, body)
            VALUES (new.id, coalesce(new.title, ''), coalesce(new.body, ''));
        END;
        CREATE TRIGGER posts_ad AFTER DELETE ON posts BEGIN
            DELETE FROM posts_fts WHERE post_id = old.id;
        END;
        CREATE TRIGGER posts_au AFTER UPDATE ON posts BEGIN
            DELETE FROM posts_fts WHERE post_id = old.id;
            INSERT INTO posts_fts(post_id, title, body)
            VALUES (new.id, coalesce(new.title, ''), coalesce(new.body, ''));
        END;
    """)
    # Heal stale/incomplete indexes produced by previous importer versions.
    conn.execute("DELETE FROM posts_fts")
    conn.execute("INSERT INTO posts_fts(post_id, title, body) SELECT id, coalesce(title,''), coalesce(body,'') FROM posts")
    conn.commit()


def insert_post(conn: sqlite3.Connection, post: dict) -> None:
    if not post.get("id"):
        raise ValueError("record has no stable id")
    raw_text = json.dumps(post.get("raw", {}), ensure_ascii=False)
    conn.execute("""
        INSERT INTO posts (
            id, type, subreddit, title, author, body, permalink, url,
            created_utc, scraped_utc, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            type=excluded.type,
            subreddit=excluded.subreddit,
            title=excluded.title,
            author=excluded.author,
            body=excluded.body,
            permalink=excluded.permalink,
            url=excluded.url,
            created_utc=excluded.created_utc,
            scraped_utc=excluded.scraped_utc,
            raw_json=excluded.raw_json
    """, (
        str(post["id"]), post.get("type", "submission"), post.get("subreddit"),
        post.get("title", ""), post.get("author", ""), post.get("body", ""),
        post.get("permalink"), post.get("url"), post.get("created_utc"),
        int(datetime.now(timezone.utc).timestamp()), raw_text,
    ))


def build_db(input_dir: str, output_db: str, vacuum: bool = False) -> dict:
    root = Path(input_dir).expanduser().resolve()
    db_path = Path(output_db).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    processed = failed = skipped = 0
    try:
        ensure_schema(conn)
        for path in iter_archive_files(root):
            try:
                if path.suffix.lower() == ".md":
                    post = parse_markdown(path, root)
                else:
                    obj = json.loads(path.read_text(encoding="utf-8"))
                    post = normalize_json(obj, path)
                if not post:
                    skipped += 1
                    continue
                with conn:
                    insert_post(conn, post)
                processed += 1
            except Exception as exc:
                failed += 1
                print(f"FAILED: {path}: {exc}")
        if vacuum:
            conn.execute("VACUUM")
        print(f"Import complete: {processed} imported, {skipped} skipped, {failed} failed")
        return {"processed": processed, "skipped": skipped, "failed": failed}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="reddit_dropbox/", help="Markdown archive directory")
    parser.add_argument("--output", default="reddit_archive.db", help="SQLite output database")
    parser.add_argument("--vacuum", action="store_true")
    args = parser.parse_args()
    try:
        result = build_db(args.input, args.output, vacuum=args.vacuum)
        return 1 if result["failed"] else 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
