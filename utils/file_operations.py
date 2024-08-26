import os
import time
import configparser
from tqdm import tqdm
from datetime import datetime

import prawcore
from praw.models import Submission, Comment
from utils.time_utilities import dynamic_sleep, exponential_backoff, lazy_load_comments

# Dynamically determine the path to the root directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Construct the full path to the settings.ini file
config_path = os.path.join(BASE_DIR, 'settings.ini')

# Load settings from the settings.ini file
config = configparser.ConfigParser()
config.read(config_path)
save_type = config.get('Settings', 'save_type', fallback='ALL').upper()

# Initialize statistics
processed_count = 0  # Counter for processed items
skipped_count = 0  # Counter for skipped items
total_size = 0  # Total size of processed data in bytes

def format_date(timestamp):
    """Format a UTC timestamp into a human-readable date."""
    return datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

def extract_video_id(url):
    """Extract the video ID from a YouTube URL."""
    if "youtube.com" in url:
        return url.split("v=")[-1]
    elif "youtu.be" in url:
        return url.split("/")[-1]
    return None

def create_directory(subreddit_name, save_directory):
    """Create the directory for saving data if it does not exist."""
    sub_dir = os.path.join(save_directory, subreddit_name)
    if not os.path.exists(sub_dir):
        os.makedirs(sub_dir)
    return sub_dir

def save_to_file(content, file_path, save_function):
    """Save content to a file using the specified save function."""
    if os.path.exists(file_path):
        return
    try:
        with open(file_path, 'w', encoding="utf-8") as f:
            save_function(content, f)
    except Exception as e:
        print(f"Failed to save {file_path}: {e}")

def save_submission(submission, f):
    """Save a submission and its metadata."""
    f.write('---\n')  # Start of frontmatter
    f.write(f'id: {submission.id}\n')
    f.write(f'subreddit: /r/{submission.subreddit.display_name}\n')
    f.write(f'timestamp: {format_date(submission.created_utc)}\n')
    f.write(f'author: /u/{submission.author.name if submission.author else "[deleted]"}\n')
    
    if submission.link_flair_text:  # Check if flair exists and is not None
        f.write(f'flair: {submission.link_flair_text}\n')
        
    f.write(f'comments: {submission.num_comments}\n')
    f.write(f'permalink: https://reddit.com{submission.permalink}\n')
    f.write('---\n\n')  # End of frontmatter
    f.write(f'# {submission.title}\n\n')
    f.write(f'**Upvotes:** {submission.score} | **Permalink:** [Link](https://reddit.com{submission.permalink})\n\n')

    if submission.is_self:
        f.write(submission.selftext if submission.selftext else '[Deleted Post]')
    else:
        if submission.url.endswith(('.jpg', '.jpeg', '.png', '.gif')):
            f.write(f"![Image]({submission.url})")
        elif "youtube.com" in submission.url or "youtu.be" in submission.url:
            video_id = extract_video_id(submission.url)
            f.write(f"[![Video](https://img.youtube.com/vi/{video_id}/0.jpg)]({submission.url})")
        else:
            f.write(submission.url if submission.url else '[Deleted Post]')

    f.write('\n\n## Comments:\n\n')
    lazy_comments = lazy_load_comments(submission)
    process_comments(lazy_comments, f)

def save_comment_and_context(comment, f):
    """Save a comment and its context."""
    f.write('---\n')  # Start of frontmatter
    f.write(f'Comment by /u/{comment.author.name if comment.author else "[deleted]"}\n')
    f.write(f'- **Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n')
    f.write(f'{comment.body}\n\n')
    f.write('---\n\n')  # End of frontmatter

    parent = comment.parent()
    if isinstance(parent, Submission):
        f.write(f'## Context: Post by /u/{parent.author.name if parent.author else "[deleted]"}\n')
        f.write(f'- **Title:** {parent.title}\n')
        f.write(f'- **Upvotes:** {parent.score} | **Permalink:** [Link](https://reddit.com{parent.permalink})\n')
        if parent.is_self:
            f.write(f'{parent.selftext}\n\n')
        else:
            f.write(f'[Link to post content]({parent.url})\n\n')
    elif isinstance(parent, Comment):
        f.write(f'## Context: Parent Comment by /u/{parent.author.name if parent.author else "[deleted]"}\n')
        f.write(f'- **Upvotes:** {parent.score} | **Permalink:** [Link](https://reddit.com{parent.permalink})\n')
        f.write(f'{parent.body}\n\n')

def process_comments(comments, f, depth=0, simple_format=False):
    """Process all comments and visualize depth using indentation."""
    for i, comment in enumerate(comments):
        if isinstance(comment, Comment):
            indent = '    ' * depth
            f.write(f'{indent}### Comment {i+1} by /u/{comment.author.name if comment.author else "[deleted]"}\n')
            f.write(f'{indent}- **Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n')
            f.write(f'{indent}{comment.body}\n\n')

            if not simple_format and comment.replies:
                process_comments(comment.replies, f, depth + 1)

            f.write(f'{indent}---\n\n')

def save_user_activity(reddit, save_directory):
    """Save user's posts and comments based on the save_type setting."""
    user = reddit.user.me()

    # Retrieve all necessary data
    submissions = list(user.submissions.new(limit=1000))
    comments = list(user.comments.new(limit=1000))
    saved_items = list(user.saved(limit=1000))

    if save_type == 'ALL':
        # Process all submissions, comments and saved items
        save_all_user_activity(submissions, comments, save_directory)
        save_saved_user_activity(saved_items, save_directory)
    elif save_type == 'SAVED':
        # Only process saved items
        save_saved_user_activity(saved_items, save_directory)

def save_all_user_activity(submissions, comments, save_directory):
    """Save all user posts and comments."""
    # Save submissions
    for submission in tqdm(submissions, desc="Processing Submissions"):
        sub_dir = create_directory(submission.subreddit.display_name, save_directory)
        file_path = os.path.join(sub_dir, f"POST_{submission.id}.md")
        save_to_file(submission, file_path, save_submission)

    # Save comments
    for comment in tqdm(comments, desc="Processing Comments"):
        sub_dir = create_directory(comment.subreddit.display_name, save_directory)
        file_path = os.path.join(sub_dir, f"COMMENT_{comment.id}.md")
        save_to_file(comment, file_path, save_comment_and_context)
        time.sleep(dynamic_sleep(len(comment.body)))

def save_saved_user_activity(saved_items, save_directory):
    """Save only saved user posts and comments."""
    # Process saved submissions and comments in one loop
    for item in tqdm(saved_items, desc="Processing Saved Items"):
        if isinstance(item, Submission):
            sub_dir = create_directory(item.subreddit.display_name, save_directory)
            file_path = os.path.join(sub_dir, f"SAVED_POST_{item.id}.md")
            save_to_file(item, file_path, save_submission)
        elif isinstance(item, Comment):
            sub_dir = create_directory(item.subreddit.display_name, save_directory)
            file_path = os.path.join(sub_dir, f"SAVED_COMMENT_{item.id}.md")
            save_to_file(item, file_path, save_comment_and_context)
            time.sleep(dynamic_sleep(len(item.body)))