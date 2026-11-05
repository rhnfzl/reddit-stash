import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import astuple, dataclass
from enum import Enum
from functools import partial
from typing import Any, NamedTuple, TypeVar
from collections.abc import Callable, Iterable

import praw
import prawcore
from tqdm import tqdm
from praw.models import Submission, Comment
from utils.env_config import load_settings
from utils.log_utils import FileLog, record_saved_file, get_existing_keys, get_given_up_keys, record_failure
from utils.save_utils import save_submission, save_comment_and_context
from utils.time_utilities import dynamic_sleep
from utils.path_security import create_reddit_file_path
from utils.praw_helpers import fetch_until_unavailable


logger = logging.getLogger(__name__)

RedditItem = Submission | Comment
T = TypeVar('T')

# Lock protecting concurrent access to created_dirs_cache (check-then-create pattern)
_dir_cache_lock = threading.Lock()


class SaveStatus(Enum):
    """Outcome of one save_to_file call."""
    SAVED = 'saved'
    SKIPPED = 'skipped'
    FAILED = 'failed'


@dataclass(frozen=True)
class SaveContext:
    """State shared by every save in one run."""
    save_directory: str
    existing_keys: set[str]
    file_log: FileLog
    created_dirs_cache: set[str]


@dataclass(frozen=True)
class RunStats:
    """Counters for a save run. Sums with +, field by field."""
    processed: int = 0
    skipped: int = 0
    total_size: int = 0
    media_size: int = 0
    failed: int = 0
    truncated_listings: tuple = ()  # labels of listings that ended early on a 404/403

    def __add__(self, other: "RunStats") -> "RunStats":
        return RunStats(*(mine + theirs for mine, theirs in zip(astuple(self), astuple(other))))


_DIR_CATEGORIES = ("UPVOTE_COMMENT", "SAVED_COMMENT", "UPVOTE_POST", "SAVED_POST", "COMMENT", "POST")

def get_existing_keys_from_dir(save_directory: str) -> set[str]:
    """Build the set of saved keys from the files on disk.

    Keys use the same "{id}-{subreddit}-{type}-{category}" format as save_to_file(),
    otherwise every DIR run re-saves (and on failure deletes) existing backups.
    """
    existing_keys = set()
    for root, dirs, files in os.walk(save_directory):
        subreddit_name = os.path.basename(root)
        for file in files:
            filename = os.path.splitext(file)[0]
            for category in _DIR_CATEGORIES:
                if filename.startswith(f"{category}_"):
                    file_id = filename[len(category) + 1:]
                    content_type = "Comment" if category.endswith("COMMENT") else "Submission"
                    existing_keys.add(f"{file_id}-{subreddit_name}-{content_type}-{category}")
                    break
    return existing_keys

def save_to_file(content: RedditItem, file_path: str, save_function: Callable[..., int], ctx: SaveContext,
                 category: str = "POST", unsave: bool = False) -> tuple[SaveStatus, int]:
    """Save content to a file using save_function, which returns the media bytes it downloaded.

    Returns (SaveStatus, media_size).
    """
    file_id = content.id  # Assuming `id` is unique for each Reddit content
    subreddit_name = content.subreddit.display_name  # Get the subreddit name

    unique_key = f"{file_id}-{subreddit_name}-{type(content).__name__}-{category}"

    if unique_key in ctx.existing_keys:
        return SaveStatus.SKIPPED, 0

    # Callers resolve file_path through path_security, so its directory is already safe.
    sub_dir = os.path.dirname(file_path)
    with _dir_cache_lock:
        if sub_dir not in ctx.created_dirs_cache:
            os.makedirs(sub_dir, exist_ok=True)
            ctx.created_dirs_cache.add(sub_dir)

    previous_entry = ctx.file_log.get(unique_key)
    try:
        with open(file_path, 'w', encoding="utf-8") as f:
            media_size = save_function(content, f, unsave=unsave)

        file_info = {
            'subreddit': subreddit_name,
            'type': type(content).__name__,
            'file_path': file_path  # This will be converted to relative in record_saved_file
        }

        record_saved_file(ctx.file_log, unique_key, file_info, ctx.save_directory)

        return SaveStatus.SAVED, media_size
    except Exception as e:
        logger.error(f"Failed to save {file_path}: {e}")
        # Clean up partial file so it's not left as corrupt content
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError as cleanup_error:
            logger.warning(f"Could not remove partial file {file_path}: {cleanup_error}")
        # Replaces any entry record_saved_file() inserted before a failed checkpoint, so the
        # item is retried next run (up to MAX_ITEM_FAILURES) instead of looking saved.
        record_failure(ctx.file_log, unique_key, e, previous_entry)
        return SaveStatus.FAILED, 0


def save_item(item: RedditItem, save_function: Callable[..., int], ctx: SaveContext,
              path_category: str, log_category: str, unsave: bool = False) -> RunStats:
    """Resolve an item's path, save it, and return its one-item RunStats.

    path_category names the file ("<category>_<id>.md"); log_category goes in its
    file_log key. They differ only for GDPR API mode, which logs under POST.
    """
    path_result = create_reddit_file_path(
        ctx.save_directory, item.subreddit.display_name, path_category, item.id
    )
    if not path_result.is_safe:
        logger.error(f"Unsafe path for item {item.id}: {path_result.issues}")
        return RunStats(failed=1)

    file_path = path_result.safe_path
    status, media_size = save_to_file(
        item, file_path, save_function, ctx, category=log_category, unsave=unsave
    )
    if status is SaveStatus.FAILED:
        return RunStats(failed=1)
    if status is SaveStatus.SKIPPED:
        return RunStats(skipped=1)

    try:
        size = os.path.getsize(file_path)
    except OSError:
        size = 0
    sleep_for_item(item)
    return RunStats(processed=1, total_size=size, media_size=media_size)

def sleep_for_item(item: RedditItem) -> None:
    """Sleep for a time scaled to the length of the item's text."""
    if isinstance(item, Submission) and item.is_self and item.selftext:
        dynamic_sleep(len(item.selftext))
    elif isinstance(item, Comment) and item.body:
        dynamic_sleep(len(item.body))
    else:
        dynamic_sleep(0)  # Minimal or no sleep for other types of posts


def _clone_reddit(reddit: praw.Reddit) -> praw.Reddit:
    """Create a new PRAW Reddit instance with same credentials for thread-safe parallel use.

    PRAW's Reddit instance is NOT thread-safe (shares a single requests.Session).
    Each thread needs its own instance with separate OAuth token and rate limit budget.
    """
    return praw.Reddit(
        client_id=reddit.config.client_id,
        client_secret=reddit.config.client_secret,
        username=reddit.config.username,
        password=reddit.config.password,
        user_agent=reddit.config.user_agent,
    )


def _fetch_items(reddit_instance: praw.Reddit, fetch_method_name: str, limit: int,
                 label: str) -> tuple[list[Any], bool]:
    """Fetch a PRAW user listing, retrying a 404 while keeping what can be fetched.

    Uses the given PRAW instance only, so each thread can fetch with its own.
    Returns (items, stopped_early); stopped_early means later items were never seen.
    A 403 on the first read raises: it fails the run, so --cycle does not record a success.
    """
    user = reddit_instance.user.me()

    def make_iter() -> Iterable[Any]:
        if fetch_method_name in ('submissions', 'comments'):
            return getattr(user, fetch_method_name).new(limit=limit)
        return getattr(user, fetch_method_name)(limit=limit)

    try:
        return list(make_iter()), False
    except prawcore.exceptions.NotFound as error:
        logger.warning(f"Batch fetch of {label} failed ({error}), retrying and keeping partial results")
        return fetch_until_unavailable(make_iter(), label)


class _SaveSource(NamedTuple):
    """One Reddit listing and how to save what it returns."""
    method: str
    label: str
    post_category: str
    comment_category: str
    desc: str
    honours_unsave: bool = False


_SUBMISSIONS = _SaveSource('submissions', 'submission', 'POST', 'COMMENT', 'Submissions')
_COMMENTS = _SaveSource('comments', 'comment', 'POST', 'COMMENT', 'Comments')
_SAVED = _SaveSource('saved', 'saved', 'SAVED_POST', 'SAVED_COMMENT', 'Saved Items', honours_unsave=True)
_UPVOTED = _SaveSource('upvoted', 'upvoted', 'UPVOTE_POST', 'UPVOTE_COMMENT', 'Upvoted Items')

_SAVE_PLANS = {
    'ALL': (_SUBMISSIONS, _COMMENTS, _SAVED, _UPVOTED),
    'ACTIVITY': (_SUBMISSIONS, _COMMENTS),
    'SAVED': (_SAVED,),
    'UPVOTED': (_UPVOTED,),
}


def _save_listing_items(items: list[Any], ctx: SaveContext, source: _SaveSource, unsave: bool,
                   tqdm_position: int) -> RunStats:
    """Save one listing's items in a single thread and return their RunStats.

    Items from one PRAW fetch share the same reddit instance, so they must be
    processed in one thread to avoid concurrent access to the shared session.
    """
    stats = RunStats()
    for item in tqdm(items, desc=source.desc, position=tqdm_position, leave=True):
        if isinstance(item, Submission):
            category, save_fn = source.post_category, save_submission
        elif isinstance(item, Comment):
            category, save_fn = source.comment_category, save_comment_and_context
        else:
            continue
        stats += save_item(item, save_fn, ctx, category, category, unsave=unsave)
    return stats


def _run_listing_calls(calls: list[Callable[[], T]]) -> list[T]:
    """Run each call in its own thread and return the results in order.

    A lone call stays in this thread, so a signal's exception (SIGTERM during --cycle)
    stops it at once; leaving a worker's pool would wait for the whole listing first.
    """
    if len(calls) == 1:
        return [calls[0]()]
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


def activity_save_context(save_directory: str, file_log: FileLog) -> SaveContext:
    """The SaveContext for save_user_activity, finding existing items as check_type says."""
    check_type = load_settings().get('Settings', 'check_type', fallback='LOG').upper()

    if check_type == 'LOG':
        logger.info("Check type is LOG. Using JSON log to find existing files.")
        existing_keys = get_existing_keys(file_log)
    elif check_type == 'DIR':
        logger.info("Check type is DIR. Using directory scan to find existing files.")
        existing_keys = get_existing_keys_from_dir(save_directory) | set(get_given_up_keys(file_log))
    else:
        raise ValueError(f"Unknown check_type: {check_type}")
    return SaveContext(save_directory, existing_keys, file_log, set())


def save_user_activity(reddit: praw.Reddit, ctx: SaveContext, unsave: bool = False) -> RunStats:
    """Save user's posts, comments, saved items, and upvoted content.

    Each listing in the save_type's plan is fetched and then processed. Several listings
    get a thread and a PRAW instance each; one listing uses this thread and reddit as is.
    Returns RunStats; the caller persists the file log.
    """
    save_type = load_settings().get('Settings', 'save_type', fallback='ALL').upper()

    if save_type not in _SAVE_PLANS:
        raise ValueError(f"Unknown save_type: {save_type}")
    sources = _SAVE_PLANS[save_type]

    fetched = _run_listing_calls([
        partial(_fetch_items, _clone_reddit(reddit) if len(sources) > 1 else reddit,
                source.method, 1000, source.label)
        for source in sources
    ])

    # 429 rate-limit errors are safe here: partial files are cleaned up and the
    # item is retried on the next run.
    stats = sum(_run_listing_calls([
        partial(_save_listing_items, items, ctx, source, unsave and source.honours_unsave, position)
        for position, (source, (items, _)) in enumerate(zip(sources, fetched))
    ]), RunStats())

    truncated = tuple(source.label for source, (_, stopped_early) in zip(sources, fetched) if stopped_early)

    return stats + RunStats(truncated_listings=truncated)
