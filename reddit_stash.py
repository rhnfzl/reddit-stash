import praw
import configparser
from utils.file_path_validate import validate_and_set_directory
from utils.file_operations import save_user_activity
from utils.env_config import load_config_and_env
from utils.log_utils import load_file_log
from utils.gdpr_processor import process_gdpr_export

def main():
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

    # Initialize Reddit
    client_id, client_secret, username, password = load_config_and_env()
    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        password=password,
        user_agent=f'Reddit Saved Saver by /u/{username}'
    )

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