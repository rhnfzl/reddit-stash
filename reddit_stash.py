import praw
import configparser
import sys
from utils.file_path_validate import validate_and_set_directory
from utils.file_operations import save_user_activity
from utils.env_config import load_config_and_env
from utils.log_utils import load_file_log, save_file_log, get_existing_keys, get_given_up_keys
from utils.gdpr_processor import process_gdpr_export
from utils.config_validator import validate_configuration
from utils.feature_flags import get_feature_summary
from utils.constants import MAX_ITEM_FAILURES


def create_reddit_client():
    """Create a Reddit client from the configured credentials."""
    client_id, client_secret, username, password = load_config_and_env()
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        password=password,
        user_agent=f'Reddit Saved Saver by /u/{username}'
    )


def retry_pending_media():
    """Retry media downloads that failed earlier and are due for another attempt."""
    from utils.retry_queue import get_retry_queue
    if not get_retry_queue().get_pending_retries(limit=1):
        return
    from utils.media_download_manager import get_media_manager
    stats = get_media_manager().process_pending_retries()
    print(f"Media retries: {stats['successful']} recovered, {stats['failed']} still failing.")


def main():
    # Validate configuration before proceeding
    print("Validating configuration...")
    try:
        validation_result = validate_configuration()

        # Print warnings if any
        if validation_result['warnings']:
            print("\nConfiguration Warnings:")
            for warning in validation_result['warnings']:
                print(f"  ⚠ {warning}")

        print("✅ Configuration validated successfully")
        print(f"📊 {get_feature_summary()}")

    except Exception as e:
        print(f"❌ Configuration validation failed: {e}")
        print("\nFor detailed configuration information, check your settings.ini file.")
        sys.exit(1)

    # Load configuration
    config_parser = configparser.ConfigParser()
    config_parser.read('settings.ini')

    # Fetch settings
    unsave_setting = config_parser.getboolean('Settings', 'unsave_after_download', fallback=False)
    save_directory = config_parser.get('Settings', 'save_directory', fallback='reddit/')
    process_api = config_parser.getboolean('Settings', 'process_api', fallback=True)
    process_gdpr = config_parser.getboolean('Settings', 'process_gdpr', fallback=False)

    # Validate directory
    save_directory = validate_and_set_directory(save_directory)

    # Initialize Reddit API connection (only if API processing is needed)
    reddit = None
    if process_api:
        reddit = create_reddit_client()
    elif process_gdpr:
        # Try to load credentials for GDPR enrichment, but don't fail if missing
        try:
            reddit = create_reddit_client()
        except Exception:
            print("No API credentials available. GDPR export will run in CSV-only mode.")
            reddit = None

    if not process_api and not process_gdpr:
        print("Both process_api and process_gdpr are disabled. Nothing to do.")
        return

    # Load the log file
    file_log = load_file_log(save_directory)

    # Initialize counters for processed statistics
    total_processed = 0
    total_skipped = 0
    total_failed = 0
    total_size = 0
    total_media_size = 0

    # Process API accessible items
    if process_api:
        print("Processing items from Reddit API...")
        api_stats = save_user_activity(reddit, save_directory, file_log, unsave=unsave_setting)

        total_processed = api_stats[0]
        total_skipped = api_stats[1]
        total_size = api_stats[2]
        total_media_size = api_stats[3]
        total_failed = api_stats[4]

    # Process GDPR export if enabled (works with or without API credentials)
    if process_gdpr:
        # Initialize tracking sets
        existing_files = get_existing_keys(file_log)
        created_dirs_cache = set()

        print("\nProcessing GDPR export data...")
        gdpr_stats = process_gdpr_export(reddit, save_directory, existing_files,
                                       created_dirs_cache, file_log)
        total_processed += gdpr_stats[0]
        total_skipped += gdpr_stats[1]
        total_size += gdpr_stats[2]
        total_failed += gdpr_stats[3]
        # GDPR processing currently doesn't track media downloads separately

    # Final save of file log to persist any remaining batched entries
    save_file_log(file_log, save_directory)

    # Best effort, after the log is safe: a retry problem must not fail a finished backup.
    try:
        retry_pending_media()
    except Exception as e:
        print(f"⚠ Media retry pass skipped: {e}")

    # Print final statistics with separate storage reporting
    print(f"\nProcessing completed. {total_processed} items processed, "
          f"{total_skipped} items skipped, {total_failed} items failed.")
    if total_failed:
        # Failed items are left out of the log, so the next run retries them.
        print(f"⚠ {total_failed} item(s) failed; each is retried on later runs "
              f"up to {MAX_ITEM_FAILURES} times.")
    given_up = get_given_up_keys(file_log)
    if given_up:
        print(f"⚠ {len(given_up)} item(s) failed {MAX_ITEM_FAILURES} times and are no longer retried "
              f"(delete their entries in file_log.json to try again): {', '.join(given_up[:10])}")
    print(f"Markdown file storage: {total_size / (1024 * 1024):.2f} MB")
    print(f"Media file storage: {total_media_size / (1024 * 1024):.2f} MB")
    print(f"Total combined storage: {(total_size + total_media_size) / (1024 * 1024):.2f} MB")

if __name__ == "__main__":
    main()
