import os
import json
import threading

from utils.constants import FILE_LOG_CHECKPOINT_INTERVAL, MAX_ITEM_FAILURES

# Lock protecting concurrent access to file_log dict and checkpoint writes
_log_lock = threading.Lock()

def get_log_file_path(save_directory):
    """Return the path to the log file inside the save_directory."""
    return os.path.join(save_directory, 'file_log.json')

def load_file_log(save_directory):
    """Load the file log from a JSON file in the specified directory."""
    log_file_path = get_log_file_path(save_directory)
    if os.path.exists(log_file_path):
        with open(log_file_path, 'r') as f:
            return json.load(f)
    return {}

def save_file_log(log_data, save_directory):
    """Save the file log to a JSON file in the specified directory."""
    log_file_path = get_log_file_path(save_directory)
    with open(log_file_path, 'w') as f:
        json.dump(log_data, f, indent=4)

def is_retryable_failure(entry):
    """True for a failure entry that has not used up its attempts yet."""
    return (isinstance(entry, dict) and entry.get('status') == 'failed'
            and entry.get('attempts', 0) < MAX_ITEM_FAILURES)

def is_file_logged(log_data, unique_key):
    """Check if a unique key is already logged (given-up failures count as logged)."""
    return unique_key in log_data and not is_retryable_failure(log_data[unique_key])

def get_existing_keys(log_data):
    """Keys to skip this run: saved items plus failures that were given up on."""
    return {key for key, entry in log_data.items() if not is_retryable_failure(entry)}

def get_given_up_keys(log_data):
    """Failure entries that reached MAX_ITEM_FAILURES and are no longer retried."""
    return [key for key, entry in log_data.items() if is_given_up(entry)]

def is_given_up(entry):
    """True for a failure entry that used up all MAX_ITEM_FAILURES attempts."""
    return (isinstance(entry, dict) and entry.get('status') == 'failed'
            and entry.get('attempts', 0) >= MAX_ITEM_FAILURES)

def record_failure(log_data, unique_key, error, previous=None):
    """Count a failed save. Failure entries carry no 'type'/'file_path', so readers
    that select saved items by type (clean_items, reddit-stash-insights) skip them.

    previous: the entry as it was before the save started; log_file() may already
    have replaced it when a checkpoint fails, which would reset the attempt count.
    """
    with _log_lock:
        if previous is None:
            previous = log_data.get(unique_key)
        attempts = previous.get('attempts', 0) if isinstance(previous, dict) and previous.get('status') == 'failed' else 0
        log_data[unique_key] = {
            'status': 'failed',
            'attempts': attempts + 1,
            'last_error': str(error)[:200],
        }

def log_file(log_data, unique_key, file_info, save_directory):
    """Add a file information to the log with the provided unique key.

    Thread-safe: uses _log_lock to serialize dict mutation and checkpoint writes.
    """
    with _log_lock:
        # Convert the absolute file path to a relative one
        relative_file_path = os.path.relpath(file_info['file_path'], start=save_directory)

        # Update the file info with the relative path
        file_info['file_path'] = relative_file_path

        # Add the file info to the log with the unique key
        log_data[unique_key] = file_info

        # Periodic checkpoint: save every N items to limit data loss on crash
        # Final save happens in file_operations.py:save_user_activity()
        if len(log_data) % FILE_LOG_CHECKPOINT_INTERVAL == 0:
            save_file_log(log_data, save_directory)

def convert_to_absolute_path(relative_path, save_directory):
    """Convert a relative path from the log back to an absolute path."""
    return os.path.join(save_directory, relative_path)