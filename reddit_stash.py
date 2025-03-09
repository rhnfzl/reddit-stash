import praw
import configparser
import logging
from utils.file_path_validate import validate_and_set_directory
from utils.file_operations import save_user_activity
from utils.env_config import load_config_and_env
from utils.log_utils import load_file_log, setup_logging
from utils.gdpr_processor import process_gdpr_export
from utils.media import process_imgur_retry_queue, update_retry_queue_save_directory
from utils.media.retry_queue import ensure_retry_queue_file_exists, debug_retry_queue_file, cleanup_imgur_retry_queue

def main():
    # Set up logging with duplicate filter
    setup_logging()
    
    # Load configuration
    config_parser = configparser.ConfigParser()
    config_parser.read('settings.ini')

    # Fetch settings
    unsave_setting = config_parser.getboolean('Settings', 'unsave_after_download', fallback=False)
    save_directory = config_parser.get('Settings', 'save_directory', fallback='reddit/')
    process_api = config_parser.getboolean('Settings', 'process_api', fallback=True)
    process_gdpr = config_parser.getboolean('Settings', 'process_gdpr', fallback=False)
    ignore_ssl_errors = config_parser.getboolean('Settings', 'ignore_ssl_errors', fallback=False)

    # Validate directory
    save_directory = validate_and_set_directory(save_directory)
    
    # Update the retry queue save directory
    # This ensures the retry queue uses the correct save directory
    # See implementation in utils/media/retry_queue.py
    update_retry_queue_save_directory(save_directory)
    
    # Ensure the retry queue file exists
    # This creates an empty file if it doesn't exist yet
    ensure_retry_queue_file_exists()
    
    # Debug the retry queue file
    debug_retry_queue_file()
    
    # Clean up the retry queue by removing items that have exceeded the maximum retry count
    removed_count = cleanup_imgur_retry_queue()
    if removed_count > 0:
        print(f"Cleaned up retry queue: removed {removed_count} items that exceeded maximum retry attempts")

    # Initialize Reddit
    client_id, client_secret, username, password = load_config_and_env()
    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        password=password,
        user_agent=f'Reddit Saved Saver by /u/{username}'
    )

    # Print warning if SSL verification is disabled
    if ignore_ssl_errors:
        print("WARNING: SSL certificate verification is disabled. This may pose security risks.")
        print("Only use this option if you understand the implications.")
        print("SSL verification warnings will be suppressed.")

    # Process the Imgur retry queue
    # This processes any failed Imgur downloads from previous runs
    # The retry queue is used to handle Imgur's rate limiting by retrying downloads
    # with exponential backoff. See implementation in utils/media/retry_queue.py
    # (Original implementation was in utils/media_utils.py.bak)
    print("Processing Imgur retry queue...")
    process_imgur_retry_queue()

    # Load the log file
    file_log = load_file_log(save_directory)
    # Process API accessible items
    if process_api:
        print("Processing items from Reddit API...")
        api_stats = save_user_activity(reddit, save_directory, file_log, unsave=unsave_setting)
        
        total_processed = api_stats[0]
        total_skipped = api_stats[1]
        total_size = api_stats[2]

    # Process GDPR export if enabled
    if process_gdpr:
        # Initialize tracking sets
        existing_files = set(file_log.keys())
        created_dirs_cache = set()
        
        print("\nProcessing GDPR export data...")
        gdpr_stats = process_gdpr_export(reddit, save_directory, existing_files, 
                                       created_dirs_cache, file_log)
        total_processed += gdpr_stats[0]
        total_skipped += gdpr_stats[1]
        total_size += gdpr_stats[2]

    # Print final statistics
    print(f"\nProcessing completed. {total_processed} items processed, "
          f"{total_skipped} items skipped.")
    print(f"Total size of processed markdown file data: "
          f"{total_size / (1024 * 1024):.2f} MB")

if __name__ == "__main__":
    main()