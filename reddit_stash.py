import praw
import configparser
from utils.file_path_validate import validate_and_set_directory
from utils.file_operations import save_user_activity
from utils.env_config import load_config_and_env

# Load configuration
config_parser = configparser.ConfigParser()
config_parser.read('settings.ini')

# Fetch the save_directory from the settings.ini file with a fallback
save_directory = config_parser.get('Settings', 'save_directory', fallback='reddit/')

# Validate and set the save directory using the utility function
save_directory = validate_and_set_directory(save_directory)

# Load Reddit API credentials
client_id, client_secret, username, password = load_config_and_env()

# Initialize the Reddit API connection using PRAW (Python Reddit API Wrapper)
reddit = praw.Reddit(
    client_id=client_id,
    client_secret=client_secret,
    username=username,
    password=password,
    user_agent=f'Reddit Saved Saver by /u/{username}'
)

if __name__ == "__main__":
    # Process user activity (submissions, comments, and saved items) and get statistics
    processed_count, skipped_count, total_size = save_user_activity(reddit, save_directory)

    # Print final statistics of processing
    print(f"Processing completed. {processed_count} items processed, {skipped_count} items skipped.")
    print(f"Total size of processed data: {total_size / (1024 * 1024):.2f} MB")