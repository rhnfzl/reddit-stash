import os
import csv
import logging
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

import praw
import prawcore
from praw.exceptions import ClientException
from tqdm import tqdm
from utils.content_recovery import ArchiveLookupError, ArcticShiftClient
from utils.content_recovery.providers import PullPushProvider
from utils.content_recovery.recovery_config import load_recovery_config
from utils.file_operations import RunStats, SaveContext, save_item
from utils.save_utils import save_submission, save_comment_and_context, yaml_frontmatter
from utils.path_security import create_reddit_file_path
from utils.log_utils import record_saved_file, is_file_logged, is_given_up, record_failure


logger = logging.getLogger(__name__)

def get_gdpr_directory(save_directory: str) -> str:
    """Return the path to the GDPR data directory."""
    # This function only checks for the directory's existence and
    # logs a message if it's missing. Creation should be handled
    # by the caller.
    gdpr_dir = os.path.join(save_directory, 'gdpr_data')
    if not os.path.exists(gdpr_dir):
        logger.warning(f"GDPR data directory not found at: {gdpr_dir}")
    return gdpr_dir


def _read_csv_rows(path: str) -> list[dict[str, str]]:
    """Read a GDPR export CSV into a list of row dicts using the stdlib csv module.

    Uses utf-8-sig so a byte-order mark (which pandas stripped automatically) does not
    leak into the first column name. Empty cells arrive as '' rather than a NaN float.
    """
    with open(path, newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def _save_csv_only(row: dict[str, str], ctx: SaveContext, kind: "_GdprKind",
                   archived_data: dict[str, Any] | None = None) -> RunStats:
    """Save a minimal markdown file for a GDPR row using only CSV metadata (no API)."""
    item_id = str(row['id'])
    # Exports have used both '/r/...' paths and full 'https://www.reddit.com/r/...' URLs.
    permalink = urlparse(row.get('permalink', '')).path
    unique_key = f"{kind.category}_{item_id}"

    # Use a generic subreddit folder when the CSV permalink does not name one
    subreddit = _extract_subreddit_from_permalink(permalink)
    path_result = create_reddit_file_path(ctx.save_directory, subreddit, kind.category, item_id)
    if not path_result.is_safe:
        logger.error(f"Unsafe path for GDPR {kind.noun} {item_id}: {path_result.issues}")
        return RunStats(failed=1)

    file_path = path_result.safe_path
    already_saved = is_file_logged(ctx.file_log, unique_key)
    archive_enriched = _has_archive_text(archived_data, kind.archive_fields)
    if is_given_up(ctx.file_log.get(unique_key)):
        return RunStats(skipped=1)  # failed MAX_ITEM_FAILURES times; no longer retried
    if already_saved and not (archive_enriched and _is_link_only_csv_export(file_path)):
        return RunStats(skipped=1)

    dir_path = os.path.dirname(file_path)
    if dir_path not in ctx.created_dirs_cache:
        os.makedirs(dir_path, exist_ok=True)
        ctx.created_dirs_cache.add(dir_path)

    reddit_url = f"https://www.reddit.com{permalink}" if permalink else f"https://www.reddit.com/comments/{item_id}"
    content = kind.render_csv_only(item_id, reddit_url, archived_data if archive_enriched else None)
    return _write_csv_only_export(file_path, content, unique_key, kind.category, item_id, archive_enriched, ctx)


def _is_link_only_csv_export(file_path: str) -> bool:
    """Return whether an existing CSV-only export is missing archived content."""
    try:
        with open(file_path, encoding='utf-8') as export_file:
            return 'Content was not fetched from Reddit.' in export_file.read()
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _write_csv_only_export(file_path: str, content: str, unique_key: str, content_type: str, item_id: str,
                           archive_enriched: bool, ctx: SaveContext) -> RunStats:
    """Write a new or archive-enriched CSV-only export and update its log entry."""
    previous_entry = ctx.file_log.get(unique_key)

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        file_size = os.path.getsize(file_path)
        record_saved_file(ctx.file_log, unique_key,
                 {
                     'file_path': file_path,
                     'type': content_type,
                     'id': item_id,
                     'archive_enriched': archive_enriched,
                 },
                 ctx.save_directory)
    except Exception as e:
        # Never leave a key from a failed write looking saved; retry next run.
        record_failure(ctx.file_log, unique_key, e, previous_entry)
        raise
    return RunStats(processed=1, total_size=file_size)


def _extract_subreddit_from_permalink(permalink: str) -> str:
    """Extract subreddit name from a Reddit permalink, or return a fallback."""
    if permalink:
        # Permalink format: /r/SubredditName/comments/...
        parts = permalink.strip('/').split('/')
        if len(parts) >= 2 and parts[0] == 'r':
            return f"r_{parts[1]}"
    return "r_unknown"


def _csv_only_post_markdown(post_id: str, reddit_url: str, archived_data: dict[str, Any] | None) -> str:
    """Render a CSV-only post export, including archive text when available."""
    archive_enriched = _has_archive_text(archived_data, ('title', 'selftext'))
    title = _archive_text(archived_data.get('title')) if archive_enriched else None
    title = title or f'GDPR Export Post: {post_id}'
    frontmatter = yaml_frontmatter({
        'id': post_id,
        'title': title,
        'permalink': reddit_url,
        'archive_enriched': archive_enriched,
    })
    if not archive_enriched:
        return (
            f"{frontmatter}# {title}\n\n"
            f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
            f"**Post ID:** {post_id}\n\n"
            f"---\n\n"
            f"*This is a CSV-only export (no API credentials available). "
            f"Content was not fetched from Reddit. "
            f"Visit the link above to view the full post.*\n"
        )

    body = _archive_text(archived_data.get('selftext')) or '*No post text was available in the archive.*'
    return (
        f"{frontmatter}# {title}\n\n"
        f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
        f"**Post ID:** {post_id}\n\n"
        f"**Archive:** Public Reddit archive\n\n"
        f"---\n\n"
        f"## Post\n\n{body}\n"
    )


def _csv_only_comment_markdown(comment_id: str, reddit_url: str, archived_data: dict[str, Any] | None) -> str:
    """Render a CSV-only comment export, including archive text when available."""
    archive_enriched = _has_archive_text(archived_data, ('body',))
    title = f'GDPR Export Comment: {comment_id}'
    frontmatter = yaml_frontmatter({
        'id': comment_id,
        'title': title,
        'permalink': reddit_url,
        'archive_enriched': archive_enriched,
    })
    if not archive_enriched:
        return (
            f"{frontmatter}# {title}\n\n"
            f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
            f"**Comment ID:** {comment_id}\n\n"
            f"---\n\n"
            f"*This is a CSV-only export (no API credentials available). "
            f"Content was not fetched from Reddit. "
            f"Visit the link above to view the full comment.*\n"
        )

    body = _archive_text(archived_data.get('body')) or '*No comment text was available in the archive.*'
    return (
        f"{frontmatter}# {title}\n\n"
        f"**Reddit Link:** [{reddit_url}]({reddit_url})\n\n"
        f"**Comment ID:** {comment_id}\n\n"
        f"**Archive:** Public Reddit archive\n\n"
        f"---\n\n"
        f"## Comment\n\n{body}\n"
    )


def _archive_text(value: Any) -> str | None:
    """Return non-empty archive text without turning missing values into strings."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _has_archive_text(archived_data: Any, fields: tuple) -> bool:
    """Return whether archive data contains content worth exporting."""
    return isinstance(archived_data, dict) and any(
        _archive_text(archived_data.get(field)) for field in fields
    )


def _archive_id(item_id: Any) -> str:
    """Normalize a Reddit fullname to the base36 ID used in archive responses."""
    if item_id is None or str(item_id).strip() == '':
        return ''
    normalized_id = str(item_id).strip()
    return normalized_id[3:] if normalized_id.startswith(('t1_', 't3_')) else normalized_id


def _lookup_archive(client: Any, content_type: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Ask one archive for a batch, keeping whatever it found before any failure."""
    fetch = client.fetch_posts if content_type == 'posts' else client.fetch_comments
    try:
        records = fetch(ids)
    except ArchiveLookupError as error:
        logger.warning(f'Archive lookup incomplete for GDPR {content_type}: {error}')
        records = error.records
    except Exception as error:
        logger.warning(f'Archive lookup failed for GDPR {content_type}: {error}')
        return {}
    if not isinstance(records, dict):
        return {}
    return {
        _archive_id(item_id): record
        for item_id, record in records.items()
        if isinstance(record, dict)
    }


def _fetch_from_archive(ids: list[str], content_type: str, archive_client: ArcticShiftClient | None,
                        archive_fallback: PullPushProvider | None = None) -> dict[str, dict[str, Any]]:
    """Fetch a batch from Arctic Shift, then enrich missing records from PullPush."""
    normalized_ids = list(dict.fromkeys(
        normalized_id
        for item_id in ids
        if (normalized_id := _archive_id(item_id))
    ))

    records = _lookup_archive(archive_client, content_type, normalized_ids) if archive_client else {}
    missing_ids = [item_id for item_id in normalized_ids if item_id not in records]

    if archive_fallback and missing_ids:
        records.update(_lookup_archive(archive_fallback, content_type, missing_ids))

    return records


def _fetch_reddit_items(reddit: praw.Reddit, item_ids: list[str], fullname_prefix: str) -> dict[str, Any]:
    """Fetch GDPR items in Reddit's 100-item fullnames batches."""
    items = {}
    normalized_ids = [_archive_id(item_id) for item_id in item_ids]
    for start in range(0, len(normalized_ids), 100):
        batch_ids = normalized_ids[start:start + 100]
        try:
            for item in reddit.info(fullnames=[f'{fullname_prefix}_{item_id}' for item_id in batch_ids]):
                items[str(item.id)] = item
        except Exception as error:
            logger.warning('GDPR batch lookup failed: %s', error)
    return items


def _load_from_reddit(reddit: praw.Reddit, kind: "_GdprKind", item_id: str) -> Any | None:
    """Load one item Reddit's batch lookup left out; None when Reddit no longer has it."""
    item = getattr(reddit, kind.reddit_accessor)(id=item_id)
    try:
        getattr(item, kind.archive_fields[0])  # forces PRAW's lazy fetch
    except (ClientException, prawcore.exceptions.NotFound) as error:
        logger.info(f"GDPR {kind.noun} {item_id} is gone from Reddit ({error}); trying the archives")
        return None
    return item


def _save_from_archive(row: dict[str, str], ctx: SaveContext, kind: "_GdprKind",
                       archive_client: ArcticShiftClient | None,
                       archive_fallback: PullPushProvider | None) -> RunStats:
    """Save an item Reddit no longer returns from the public archives.

    Found: saved once and never fetched again. Not found: counted as a failure, so it
    stops after MAX_ITEM_FAILURES imports instead of being tried forever.
    """
    item_id = _archive_id(row['id'])
    unique_key = f"{kind.category}_{item_id}"
    entry = ctx.file_log.get(unique_key)
    if is_given_up(entry) or is_file_logged(ctx.file_log, unique_key):
        return RunStats(skipped=1)
    archived = _fetch_from_archive([item_id], f'{kind.noun}s', archive_client, archive_fallback).get(item_id)
    if _has_archive_text(archived, kind.archive_fields):
        return _save_csv_only(row, ctx, kind, archived)
    record_failure(ctx.file_log, unique_key, 'Not on Reddit and not in the public archives', entry)
    return RunStats(failed=1)


@dataclass(frozen=True)
class _GdprKind:
    """What differs between the export CSVs this module reads."""
    noun: str
    fullname_prefix: str
    category: str
    archive_fields: tuple
    render_csv_only: Callable
    save_function: Callable
    reddit_accessor: str
    csv_name: str
    log_category: str = 'POST'  # API mode key; saved items keep POST, existing file_log keys depend on it
    upvotes_only: bool = False


_POSTS = _GdprKind('post', 't3', 'GDPR_POST', ('title', 'selftext'),
                   _csv_only_post_markdown, save_submission, 'submission', 'saved_posts.csv')
_COMMENTS = _GdprKind('comment', 't1', 'GDPR_COMMENT', ('body',),
                      _csv_only_comment_markdown, save_comment_and_context, 'comment', 'saved_comments.csv')
# Reddit's upvoted listing returns posts only, so the export is the one source of upvoted comments.
# Same file names and keys as the API upvoted path would use.
_UPVOTED_COMMENTS = _GdprKind('comment', 't1', 'UPVOTE_COMMENT', ('body',),
                              _csv_only_comment_markdown, save_comment_and_context, 'comment',
                              'comment_votes.csv', log_category='UPVOTE_COMMENT', upvotes_only=True)


def _process_gdpr_rows(kind: _GdprKind, gdpr_dir: str, reddit: praw.Reddit | None, ctx: SaveContext,
                       archive_client: ArcticShiftClient | None,
                       archive_fallback: PullPushProvider | None) -> RunStats:
    """Process one export CSV, through the archive when reddit is None, else the API."""
    export_path = os.path.join(gdpr_dir, kind.csv_name)
    if not os.path.exists(export_path):
        return RunStats()

    logger.info(f"Processing {kind.csv_name} from GDPR export...")
    rows = _read_csv_rows(export_path)
    if kind.upvotes_only:
        rows = [row for row in rows if row.get('direction') == 'up']
    ids = [row.get('id', '') for row in rows]

    if reddit is None:
        archived = _fetch_from_archive(ids, f'{kind.noun}s', archive_client, archive_fallback)

        def save_row(row: dict[str, str]) -> RunStats:
            return _save_csv_only(row, ctx, kind, archived.get(_archive_id(row['id'])))
    else:
        fetched = _fetch_reddit_items(reddit, ids, kind.fullname_prefix)

        def save_row(row: dict[str, str]) -> RunStats:
            item_id = _archive_id(row['id'])
            item = fetched.get(item_id) or _load_from_reddit(reddit, kind, item_id)
            if item is None:
                return _save_from_archive(row, ctx, kind, archive_client, archive_fallback)
            return save_item(item, kind.save_function, ctx, kind.category, kind.log_category)

    stats = RunStats()
    for row in tqdm(rows, total=len(rows), desc=f"GDPR {kind.csv_name}"):
        try:
            stats += save_row(row)
        except Exception as e:
            logger.error(f"Error processing GDPR {kind.noun} {row.get('id')}: {e}")
            stats += RunStats(failed=1)
    return stats


def process_gdpr_export(reddit: praw.Reddit | None, ctx: SaveContext,
                        archive_client: ArcticShiftClient | None = None,
                        archive_fallback: PullPushProvider | None = None) -> RunStats:
    """Process saved posts/comments and upvoted comments from GDPR export CSVs and return RunStats.

    When reddit is None (no API credentials), saves minimal markdown files using
    CSV metadata, plus archive text from the [Recovery] providers that are enabled.
    """
    if reddit is None:
        logger.info("Running in CSV-only mode (no API credentials). "
                    "Fetching archived post and comment text when it is available.")
    # API mode needs the archives too, for items Reddit no longer returns.
    if archive_client is None or archive_fallback is None:
        try:
            recovery = load_recovery_config()
        except ValueError as e:
            raise ValueError(f"Invalid value in the [Recovery] section of settings.ini: {e}") from e
        timeout = recovery.timeout_seconds
        if recovery.use_arctic_shift and archive_client is None:
            archive_client = ArcticShiftClient(timeout=timeout)
        if recovery.use_pullpush and archive_fallback is None:
            archive_fallback = PullPushProvider(timeout)

    gdpr_dir = get_gdpr_directory(ctx.save_directory)
    return (_process_gdpr_rows(_POSTS, gdpr_dir, reddit, ctx, archive_client, archive_fallback)
            + _process_gdpr_rows(_COMMENTS, gdpr_dir, reddit, ctx, archive_client, archive_fallback)
            + _process_gdpr_rows(_UPVOTED_COMMENTS, gdpr_dir, reddit, ctx, archive_client, archive_fallback))
