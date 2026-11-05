import os
import json
import logging
import shutil
import threading
from typing import TypedDict

from utils.constants import FILE_LOG_CHECKPOINT_INTERVAL, LOG_FILE_NAME, MAX_ITEM_FAILURES

FAILED_STATUS = 'failed'


class SavedEntry(TypedDict, total=False):
    """A saved item. API saves set subreddit; CSV-only GDPR saves set id and archive_enriched."""
    file_path: str  # relative to the save directory
    type: str
    subreddit: str
    id: str
    archive_enriched: bool


class FailedEntry(TypedDict):
    """An item that failed to save. No type or file_path, so readers of saved items skip it."""
    status: str  # FAILED_STATUS
    attempts: int
    last_error: str


FileLog = dict[str, SavedEntry | FailedEntry]

# Lock protecting concurrent access to file_log dict and checkpoint writes
_log_lock = threading.Lock()
_saves_since_checkpoint = 0

def configure_logging() -> None:
    """Show this project's INFO messages. Other libraries stay at WARNING: the Dropbox SDK logs every request at INFO."""
    logging.basicConfig()
    logging.getLogger('utils').setLevel(logging.INFO)

def get_log_file_path(save_directory: str) -> str:
    """Return the path to the log file inside the save_directory."""
    return os.path.join(save_directory, LOG_FILE_NAME)

def load_file_log(save_directory: str) -> FileLog:
    """Load the file log from a JSON file in the specified directory.

    A damaged log raises instead of reading as empty, which would re-process everything.
    """
    log_file_path = get_log_file_path(save_directory)
    if os.path.exists(log_file_path):
        try:
            with open(log_file_path, 'r') as f:
                return json.load(f)
        except ValueError as e:  # JSONDecodeError and UnicodeDecodeError
            raise ValueError(
                f"{log_file_path} is damaged ({e}). Restore it from your storage backup "
                "(python storage_utils.py --download). Deleting it instead makes the next "
                "run process every item again."
            ) from e
    return {}

def write_log_atomic(log_data: FileLog, log_file_path: str) -> None:
    """Swap in a fully written log, so a crash leaves the old file or the new one, never a partial one."""
    # Dot-prefixed: uploads skip dotfiles, so a crash cannot leave this in cloud storage.
    directory, name = os.path.split(log_file_path)
    tmp_path = os.path.join(directory, f".{name}.tmp")
    try:
        with open(tmp_path, 'w') as f:
            json.dump(log_data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(log_file_path):
            shutil.copymode(log_file_path, tmp_path)
        os.replace(tmp_path, log_file_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

def save_file_log(log_data: FileLog, save_directory: str) -> None:
    """Save the file log to a JSON file in the specified directory."""
    write_log_atomic(log_data, get_log_file_path(save_directory))

def is_retryable_failure(entry: object) -> bool:
    """True for a failure entry that has not used up its attempts yet."""
    return (isinstance(entry, dict) and entry.get('status') == FAILED_STATUS
            and entry.get('attempts', 0) < MAX_ITEM_FAILURES)

def is_file_logged(log_data: FileLog, unique_key: str) -> bool:
    """Check if a unique key is already logged (given-up failures count as logged)."""
    return unique_key in log_data and not is_retryable_failure(log_data[unique_key])

def get_existing_keys(log_data: FileLog) -> set[str]:
    """Keys to skip this run: saved items plus failures that were given up on."""
    return {key for key, entry in log_data.items() if not is_retryable_failure(entry)}

def get_given_up_keys(log_data: FileLog) -> list[str]:
    """Failure entries that reached MAX_ITEM_FAILURES and are no longer retried."""
    return [key for key, entry in log_data.items() if is_given_up(entry)]

def is_given_up(entry: object) -> bool:
    """True for a failure entry that used up all MAX_ITEM_FAILURES attempts."""
    return (isinstance(entry, dict) and entry.get('status') == FAILED_STATUS
            and entry.get('attempts', 0) >= MAX_ITEM_FAILURES)

def record_failure(log_data: FileLog, unique_key: str, error: object, previous: SavedEntry | FailedEntry | None = None) -> None:
    """Count a failed save. Failure entries carry no 'type'/'file_path', so readers
    that select saved items by type (clean_items, reddit-stash-insights) skip them.

    previous: the entry as it was before the save started; record_saved_file() may already
    have replaced it when a checkpoint fails, which would reset the attempt count.
    """
    with _log_lock:
        if previous is None:
            previous = log_data.get(unique_key)
        attempts = previous.get('attempts', 0) if isinstance(previous, dict) and previous.get('status') == FAILED_STATUS else 0
        log_data[unique_key] = {
            'status': FAILED_STATUS,
            'attempts': attempts + 1,
            'last_error': str(error)[:200],
        }

def record_saved_file(log_data: FileLog, unique_key: str, file_info: SavedEntry, save_directory: str) -> None:
    """Add a file information to the log with the provided unique key.

    Thread-safe: uses _log_lock to serialize dict mutation and checkpoint writes.
    """
    global _saves_since_checkpoint
    with _log_lock:
        relative_file_path = os.path.relpath(file_info['file_path'], start=save_directory)

        file_info['file_path'] = relative_file_path

        log_data[unique_key] = file_info

        # Periodic checkpoint: save every N items to limit data loss on crash
        # Final save happens in reddit_stash.py:main()
        _saves_since_checkpoint += 1
        if _saves_since_checkpoint >= FILE_LOG_CHECKPOINT_INTERVAL:
            # Reset first: while the disk stays full, only every Nth item should fail on this.
            _saves_since_checkpoint = 0
            save_file_log(log_data, save_directory)

def convert_to_absolute_path(relative_path: str, save_directory: str) -> str:
    """Convert a relative path from the log back to an absolute path."""
    return os.path.join(save_directory, relative_path)