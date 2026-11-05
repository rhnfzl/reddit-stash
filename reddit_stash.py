import praw
import sys

from utils.path_security import ensure_writable_directory
from utils.file_operations import RunStats, SaveContext, activity_save_context, save_user_activity
from utils.env_config import MissingCredentialsError, load_reddit_credentials, load_settings
from utils.log_utils import FileLog, configure_logging, load_file_log, save_file_log, get_existing_keys, get_given_up_keys
from utils.gdpr_processor import process_gdpr_export
from utils.config_validator import validate_configuration
from utils.feature_flags import get_feature_summary
from utils.storage.factory import get_storage_summary
from utils.constants import MAX_ITEM_FAILURES
from utils.media_services.download_manager import get_media_manager
from utils.media_services.retry_queue import get_retry_queue
from utils.save_utils import relink_media_line


def create_reddit_client() -> praw.Reddit:
    """Create a Reddit client from the configured credentials."""
    client_id, client_secret, username, password = load_reddit_credentials()
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        password=password,
        user_agent=f'Reddit Saved Saver by /u/{username}'
    )


def retry_pending_media() -> None:
    """Retry media downloads that failed earlier and are due for another attempt."""
    queue = get_retry_queue()
    # Only this pass marks rows in_progress; any left over are from an earlier run that
    # crashed or could not record an outcome. They must count as due below.
    queue.reset_stale_in_progress()
    if not queue.get_pending_retries(limit=1):
        return
    stats = get_media_manager().process_pending_retries(on_recovered=relink_media_line)
    print(f"Media retries: {stats['successful']} recovered, {stats['failed']} still failing.")
    if stats['skipped']:
        print(f"  {stats['skipped']} queued retries skipped this run; see the log.")
    if stats['unlinked']:
        print(f"  {stats['unlinked']} recovered file(s) saved, but the post's markdown keeps the remote link.")


def validate_config_or_exit() -> None:
    """Report configuration problems and stop before any work starts."""
    print("Validating configuration...")
    try:
        validation_result = validate_configuration()

        if validation_result['warnings']:
            print("\nConfiguration Warnings:")
            for warning in validation_result['warnings']:
                print(f"  ⚠ {warning}")

        print("✅ Configuration validated successfully")
        print(f"📊 {get_feature_summary()}\n{get_storage_summary()}")

    except Exception as e:
        print(f"❌ Configuration validation failed: {e}")
        print("\nFor detailed configuration information, check your settings.ini file.")
        sys.exit(1)


def connect_reddit(process_api: bool, process_gdpr: bool) -> praw.Reddit | None:
    """Return a Reddit client when one is needed; GDPR-only runs may go without credentials."""
    if process_api:
        return create_reddit_client()
    if not process_gdpr:
        return None
    # Try to load credentials for GDPR enrichment, but don't fail if missing
    try:
        return create_reddit_client()
    except MissingCredentialsError:
        print("No API credentials available. GDPR export will run in CSV-only mode.")
        return None


def print_run_summary(totals: RunStats, file_log: FileLog) -> None:
    """Print final statistics with separate storage reporting."""
    print(f"\nProcessing completed. {totals.processed} items processed, "
          f"{totals.skipped} items skipped, {totals.failed} items failed.")
    if totals.failed:
        # Failed items stay in the log as failures, so later runs retry them.
        print(f"⚠ {totals.failed} item(s) failed; each is retried on later runs "
              f"up to {MAX_ITEM_FAILURES} times.")
    if totals.truncated_listings:
        print(f"⚠ Reddit refused part of these listings (404/403), so older items in them "
              f"were not checked this run: {', '.join(totals.truncated_listings)}")
    given_up = get_given_up_keys(file_log)
    if given_up:
        print(f"⚠ {len(given_up)} item(s) failed {MAX_ITEM_FAILURES} times and are no longer retried "
              f"(delete their entries in file_log.json to try again): {', '.join(given_up[:10])}")
    print(f"Markdown file storage: {totals.total_size / (1024 * 1024):.2f} MB")
    print(f"Media file storage: {totals.media_size / (1024 * 1024):.2f} MB")
    print(f"Total combined storage: {(totals.total_size + totals.media_size) / (1024 * 1024):.2f} MB")


def main() -> None:
    configure_logging()
    validate_config_or_exit()

    config_parser = load_settings()

    unsave_setting = config_parser.getboolean('Settings', 'unsave_after_download', fallback=False)
    save_directory = config_parser.get('Settings', 'save_directory', fallback='reddit/')
    process_api = config_parser.getboolean('Settings', 'process_api', fallback=True)
    process_gdpr = config_parser.getboolean('Settings', 'process_gdpr', fallback=False)

    save_directory = ensure_writable_directory(save_directory)

    reddit = connect_reddit(process_api, process_gdpr)

    if not process_api and not process_gdpr:
        print("Both process_api and process_gdpr are disabled. Nothing to do.")
        return

    file_log = load_file_log(save_directory)

    totals = RunStats()

    if process_api:
        print("Processing items from Reddit API...")
        totals += save_user_activity(reddit, activity_save_context(save_directory, file_log), unsave=unsave_setting)
        # Before GDPR runs, so its failure cannot lose this pass's log entries.
        save_file_log(file_log, save_directory)

    # Process GDPR export if enabled (works with or without API credentials)
    if process_gdpr:
        print("\nProcessing GDPR export data...")
        # Always log keys, whatever check_type says: a directory scan knows no GDPR_ files.
        ctx = SaveContext(save_directory, get_existing_keys(file_log), file_log, set())
        totals += process_gdpr_export(reddit, ctx)

    # Final save of file log to persist any remaining batched entries
    save_file_log(file_log, save_directory)

    # Best effort, after the log is safe: a retry problem must not fail a finished backup.
    try:
        retry_pending_media()
    except Exception as e:
        print(f"⚠ Media retry pass skipped: {e}")

    print_run_summary(totals, file_log)


if __name__ == "__main__":
    main()
