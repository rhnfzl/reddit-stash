import os
import time
import configparser
from tqdm import tqdm
from praw.models import Submission, Comment  # Import Submission and Comment
from utils.log_utils import log_file, save_file_log
from utils.save_utils import save_submission, save_comment_and_context  # Import common functions
from utils.time_utilities import dynamic_sleep

# Dynamically determine the path to the root directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Construct the full path to the settings.ini file
config_path = os.path.join(BASE_DIR, 'settings.ini')

# Load settings from the settings.ini file
config = configparser.ConfigParser()
config.read(config_path)
save_type = config.get('Settings', 'save_type', fallback='ALL').upper()
check_type = config.get('Settings', 'check_type', fallback='DIR').upper()

def create_directory(subreddit_name, save_directory, created_dirs_cache):
    """Create the directory for saving data if it does not exist."""
    sub_dir = os.path.join(save_directory, subreddit_name)
    if sub_dir not in created_dirs_cache:
        os.makedirs(sub_dir, exist_ok=True)
        created_dirs_cache.add(sub_dir)
    return sub_dir

def get_existing_files_from_log(file_log):
    """Return a set of unique keys (subreddit + id) based on the JSON log."""
    existing_files = set(file_log.keys())
    return existing_files

def get_existing_files_from_dir(save_directory):
    """Build a set of all existing files in the save directory using os.walk."""
    existing_files = set()
    for root, dirs, files in os.walk(save_directory):
        for file in files:
            # Extract the unique key format (id-subreddit-content_type) from the file path
            filename = os.path.splitext(file)[0]
            subreddit_name = os.path.basename(root)
            content_type = None
            
            if filename.startswith("POST_"):
                file_id = filename.split("POST_")[1]
                content_type = "Submission"
            elif filename.startswith("COMMENT_"):
                file_id = filename.split("COMMENT_")[1]
                content_type = "Comment"
            elif filename.startswith("SAVED_POST_"):
                file_id = filename.split("SAVED_POST_")[1]
                content_type = "Submission"
            elif filename.startswith("SAVED_COMMENT_"):
                file_id = filename.split("SAVED_COMMENT_")[1]
                content_type = "Comment"
            else:
                continue
            
            unique_key = f"{file_id}-{subreddit_name}-{content_type}"
            existing_files.add(unique_key)
    return existing_files

def save_to_file(content, file_path, save_function, existing_files, file_log, save_directory, created_dirs_cache):
    """Save content to a file using the specified save function."""
    file_id = content.id  # Assuming `id` is unique for each Reddit content
    subreddit_name = content.subreddit.display_name  # Get the subreddit name
    
    # Create the unique key including the content type
    unique_key = f"{file_id}-{subreddit_name}-{type(content).__name__}"
    
    # If the file is already logged or exists in the directory, skip saving
    if unique_key in existing_files:
        return True  # Indicate that the file already exists and no saving was performed

    # Ensure the subreddit directory exists only if we're about to save something new
    sub_dir = os.path.join(save_directory, subreddit_name)
    if sub_dir not in created_dirs_cache:
        os.makedirs(sub_dir, exist_ok=True)
        created_dirs_cache.add(sub_dir)
    
    # Proceed with saving the file
    try:
        with open(file_path, 'w', encoding="utf-8") as f:
            save_function(content, f)
        
        # Log the file after saving successfully with the unique key
        log_file(file_log, unique_key, {  # Use the unique_key constructed in save_to_file
            'subreddit': subreddit_name,
            'type': type(content).__name__,
            'file_path': file_path  # This will be converted to relative in log_file
        }, save_directory)

        return False  # Indicate that the file was saved successfully
    except Exception as e:
        print(f"Failed to save {file_path}: {e}")
        return False  # Indicate that the file could not be saved

def handle_dynamic_sleep(item):
    """Handle dynamic sleep based on the type of Reddit item."""
    if isinstance(item, Submission) and item.is_self and item.selftext:
        time.sleep(dynamic_sleep(len(item.selftext)))
    elif isinstance(item, Comment) and item.body:
        time.sleep(dynamic_sleep(len(item.body)))
    else:
        time.sleep(dynamic_sleep(0))  # Minimal or no sleep for other types of posts


def save_user_activity(reddit, save_directory, file_log):
    """Save user's posts, comments, saved items, and upvoted content."""
    user = reddit.user.me()

    # Determine how to check for existing files based on check_type
    if check_type == 'LOG':
        print("Check type is LOG. Using JSON log to find existing files.")
        existing_files = get_existing_files_from_log(file_log)
    elif check_type == 'DIR':
        print("Check type is DIR. Using directory scan to find existing files.")
        existing_files = get_existing_files_from_dir(save_directory)
    else:
        raise ValueError(f"Unknown check_type: {check_type}")

    created_dirs_cache = set()

    processed_count = 0  # Counter for processed items
    skipped_count = 0  # Counter for skipped items
    total_size = 0  # Total size of processed data in bytes

    if save_type == 'ALL':
        # Save all user submissions and comments
        processed_count, skipped_count, total_size = save_self_user_activity(
            list(user.submissions.new(limit=1000)), 
            list(user.comments.new(limit=1000)),
            save_directory, existing_files, created_dirs_cache, 
            processed_count, skipped_count, total_size, file_log
        )
        
        # Save all saved items (posts and comments)
        processed_count, skipped_count, total_size = save_saved_user_activity(
            list(user.saved(limit=1000)), save_directory, existing_files, 
            created_dirs_cache, processed_count, skipped_count, total_size, file_log
        )
        
        # Save all upvoted posts and comments
        processed_count, skipped_count, total_size = save_upvoted_posts_and_comments(
            list(user.upvoted(limit=1000)), save_directory, existing_files, created_dirs_cache, 
            processed_count, skipped_count, total_size, file_log
        )
    
    elif save_type == 'SAVED':
        processed_count, skipped_count, total_size = save_saved_user_activity(
            list(user.saved(limit=1000)), save_directory, existing_files, 
            created_dirs_cache, processed_count, skipped_count, total_size, file_log
        )
    
    elif save_type == 'ACTIVITY':
        processed_count, skipped_count, total_size = save_self_user_activity(
            list(user.submissions.new(limit=1000)), 
            list(user.comments.new(limit=1000)),
            save_directory, existing_files, created_dirs_cache, 
            processed_count, skipped_count, total_size, file_log
        )
        
    elif save_type == 'UPVOTED':
        processed_count, skipped_count, total_size = save_upvoted_posts_and_comments(
            list(user.upvoted(limit=1000)), save_directory, existing_files, created_dirs_cache, 
            processed_count, skipped_count, total_size, file_log
        )

    # Save the updated file log
    save_file_log(file_log, save_directory)

    return processed_count, skipped_count, total_size


def save_self_user_activity(submissions, comments, save_directory, existing_files, created_dirs_cache, processed_count, skipped_count, total_size, file_log):
    """Save all user posts and comments."""
    for submission in tqdm(submissions, desc="Processing Users Submissions"):
        file_path = os.path.join(save_directory, submission.subreddit.display_name, f"POST_{submission.id}.md")
        if save_to_file(submission, file_path, save_submission, existing_files, file_log, save_directory, created_dirs_cache):
            skipped_count += 1
            continue

        processed_count += 1
        total_size += os.path.getsize(file_path)
        handle_dynamic_sleep(submission)  # Call the refactored sleep function

    for comment in tqdm(comments, desc="Processing Users Comments"):
        file_path = os.path.join(save_directory, comment.subreddit.display_name, f"COMMENT_{comment.id}.md")
        if save_to_file(comment, file_path, save_comment_and_context, existing_files, file_log, save_directory, created_dirs_cache):
            skipped_count += 1
            continue

        processed_count += 1
        total_size += os.path.getsize(file_path)
        handle_dynamic_sleep(comment)  # Call the refactored sleep function

    return processed_count, skipped_count, total_size

def save_saved_user_activity(saved_items, save_directory, existing_files, created_dirs_cache, processed_count, skipped_count, total_size, file_log):
    """Save only saved user posts and comments."""
    for item in tqdm(saved_items, desc="Processing Saved Items"):
        if isinstance(item, Submission):
            file_path = os.path.join(save_directory, item.subreddit.display_name, f"SAVED_POST_{item.id}.md")
            if save_to_file(item, file_path, save_submission, existing_files, file_log, save_directory, created_dirs_cache):
                skipped_count += 1  # Increment skipped count if the file already exists
                continue  # Skip further processing if the file already exists
        elif isinstance(item, Comment):
            file_path = os.path.join(save_directory, item.subreddit.display_name, f"SAVED_COMMENT_{item.id}.md")
            if save_to_file(item, file_path, save_comment_and_context, existing_files, file_log, save_directory, created_dirs_cache):
                skipped_count += 1  # Increment skipped count if the file already exists
                continue  # Skip further processing if the file already exists

        processed_count += 1  # Increment processed count
        total_size += os.path.getsize(file_path)  # Accumulate total size of processed files
        handle_dynamic_sleep(item)  # Call the refactored sleep function

    return processed_count, skipped_count, total_size

def save_upvoted_posts_and_comments(upvoted_items, save_directory, existing_files, created_dirs_cache, processed_count, skipped_count, total_size, file_log):
    """Save only upvoted user posts and comments."""
    for item in tqdm(upvoted_items, desc="Processing Upvoted Items"):
        if isinstance(item, Submission):
            file_path = os.path.join(save_directory, item.subreddit.display_name, f"UPVOTE_POST_{item.id}.md")
            if save_to_file(item, file_path, save_submission, existing_files, file_log, save_directory, created_dirs_cache):
                skipped_count += 1  # Increment skipped count if the file already exists
                continue  # Skip further processing if the file already exists
        elif isinstance(item, Comment):
            file_path = os.path.join(save_directory, item.subreddit.display_name, f"UPVOTE_COMMENT_{item.id}.md")
            if save_to_file(item, file_path, save_comment_and_context, existing_files, file_log, save_directory, created_dirs_cache):
                skipped_count += 1  # Increment skipped count if the file already exists
                continue  # Skip further processing if the file already exists

        processed_count += 1  # Increment processed count
        total_size += os.path.getsize(file_path)  # Accumulate total size of processed files
        handle_dynamic_sleep(item)  # Call the refactored sleep function

    return processed_count, skipped_count, total_size
