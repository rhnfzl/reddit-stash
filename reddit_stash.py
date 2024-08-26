import os
import sys
import time
import praw
from praw.models import Submission, Comment
import configparser
from tqdm import tqdm
from utils.file_path_validate import validate_and_set_directory
from utils.file_operations import save_user_activity, save_submission, save_comment_and_context
from utils.time_utilities import dynamic_sleep
from utils.env_config import load_config_and_env  # Import the new utility function

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

# Initialize statistics
processed_count = 0  # Counter for processed items
skipped_count = 0  # Counter for skipped items
total_size = 0  # Total size of processed data in bytes

if __name__ == "__main__":
    # Process saved items
    for saved_item in tqdm(reddit.user.me().saved(limit=1000), desc="Processing Saved Items"):
        sub_dir = os.path.join(save_directory, saved_item.subreddit.display_name)
        if not os.path.exists(sub_dir):
            os.makedirs(sub_dir)

        # Use a detailed naming convention
        if isinstance(saved_item, Submission):
            file_name = f"POST_{saved_item.id}.md"
        elif isinstance(saved_item, Comment):
            file_name = f"COMMENT_{saved_item.id}.md"
        
        file_path = os.path.join(sub_dir, file_name)

        if os.path.exists(file_path):
            skipped_count += 1  # Increment skipped count if the file already exists
            continue

        with open(file_path, 'w', encoding="utf-8") as f:
            if isinstance(saved_item, Submission):
                save_submission(saved_item, f)
            elif isinstance(saved_item, Comment):
                save_comment_and_context(saved_item, f)

        processed_count += 1  # Increment processed count
        total_size += os.path.getsize(file_path)  # Accumulate total size of processed files

        time.sleep(dynamic_sleep(len(saved_item.body if isinstance(saved_item, Comment) else saved_item.selftext or saved_item.url)))

    # Process user activity (submissions and comments)
    save_user_activity(reddit, save_directory)

    # Print final statistics
    print(f"Processing completed. {processed_count} items processed, {skipped_count} items skipped.")
    print(f"Total size of processed data: {total_size / (1024 * 1024):.2f} MB")