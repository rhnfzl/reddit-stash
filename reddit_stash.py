import praw, os, config, time, sys
from praw.models import Submission, Comment
from datetime import datetime
import prawcore
from tqdm import tqdm
import time

# Reddit API configuration
client_id = os.getenv('REDDIT_CLIENT_ID')
client_secret = os.getenv('REDDIT_CLIENT_SECRET')
username = os.getenv('REDDIT_USERNAME')
password = os.getenv('REDDIT_PASSWORD')

if not all([client_id, client_secret, username, password]):
    raise Exception("One or more environment variables for Reddit API are missing.")

reddit = praw.Reddit(
    client_id=client_id,
    client_secret=client_secret,
    username=username,
    password=password,
    user_agent='Reddit Saved Saver by /u/complexrexton'
)

print("Fetching...")

try:
    saved = reddit.user.me().saved(limit=1000)
except:
    sys.exit("Failed to find your saved posts, did you add your credentials to config.py?")

top_dir = 'reddit/'

if not os.path.exists(top_dir):
    os.mkdir(top_dir)

def process_comments(comments, f, depth=0):
    """Process all comments and visualize depth using indentation."""
    for i, comment in enumerate(comments):
        if isinstance(comment, Comment):
            # Write the comment with indentation based on depth
            indent = '    ' * depth
            f.write(f'{indent}### Comment {i+1} by /u/{comment.author.name if comment.author else "[deleted]"}\n')
            f.write(f'{indent}- **Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n')
            f.write(f'{indent}{comment.body}\n\n')
            
            # Process replies recursively without limit
            if comment.replies:
                process_comments(comment.replies, f, depth + 1)

            f.write(f'{indent}---\n\n')

def dynamic_sleep(processed_count, content_length):
    """
    Dynamically adjust sleep time based on the number of submissions processed
    and the cumulative content length processed.
    
    Parameters:
    processed_count (int): The number of submissions processed so far.
    content_length (int): The estimated cumulative length of the content processed.
    
    Returns:
    float: The number of seconds to sleep.
    """
    base_sleep_time = 1  # Base time to start with
    
    # Adjust sleep based on the number of submissions processed
    if processed_count > 100:
        sleep_time = base_sleep_time * 2
    elif processed_count > 50:
        sleep_time = base_sleep_time * 1.5
    else:
        sleep_time = base_sleep_time
    
    # Further adjust sleep based on the content length
    if content_length > 10000:  # Large content length threshold
        sleep_time *= 2
    elif content_length > 5000:  # Moderate content length threshold
        sleep_time *= 1.5
    
    return sleep_time

def lazy_load_comments(submission):
    """Lazily load comments instead of replacing all at once."""
    try:
        for comment in submission.comments.list():
            yield comment
    except prawcore.exceptions.TooManyRequests:
        print("Rate limit exceeded. Sleeping for 120 seconds...")
        time.sleep(120)  # Sleep for 120 seconds before retrying
        yield from lazy_load_comments(submission)  # Retry the request

def save_comment_and_context(comment, f):
    """Save a comment and its context."""
    f.write('---\n')
    f.write(f'Comment by /u/{comment.author.name if comment.author else "[deleted]"}\n')
    f.write(f'- **Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n')
    f.write(f'{comment.body}\n\n')
    f.write('---\n\n')
    
    # Fetch and save the parent post or comment for context
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

processed_count = 0  # Counter to keep track of processed submissions

for saved_item in tqdm(saved, desc="Processing Saved Items"):
    # Determine the save location based on subreddit
    sub_dir = top_dir + saved_item.subreddit.display_name + '/'
    if not os.path.exists(sub_dir):
        os.mkdir(sub_dir)

    file_path = sub_dir + saved_item.id + '.md'

    # Check if the file already exists to avoid overwriting
    if os.path.exists(file_path):
        print(f"File {file_path} already exists. Skipping to prevent overwriting.")
        continue

    with open(file_path, 'w', encoding="utf-8") as f:
        if isinstance(saved_item, Submission):
            # Save the post and its comments
            f.write('---\n')
            f.write(f'id: {saved_item.id}\n')
            f.write(f'subreddit: /r/{saved_item.subreddit.display_name}\n')
            f.write(f'timestamp: {str(datetime.utcfromtimestamp(saved_item.created_utc))}\n')
            f.write(f'author: /u/{saved_item.author.name if saved_item.author else "[deleted]"}\n')
            f.write(f'permalink: https://reddit.com{saved_item.permalink}\n')
            f.write('---\n\n')
            f.write(f'# {saved_item.title}\n\n')
            f.write(f'**Upvotes:** {saved_item.score} | **Permalink:** [Link](https://reddit.com{saved_item.permalink})\n\n')
            if saved_item.is_self:
                f.write(saved_item.selftext if saved_item.selftext else '[Deleted Post]')
            else:
                f.write(saved_item.url if saved_item.url else '[Deleted Post]')
            f.write('\n\n## Comments:\n\n')
            lazy_comments = lazy_load_comments(saved_item)
            process_comments(lazy_comments, f)
        elif isinstance(saved_item, Comment):
            # Save the comment and its context
            save_comment_and_context(saved_item, f)
    
    processed_count += 1  # Increment the processed counter
    time.sleep(dynamic_sleep(processed_count, len(saved_item.body if isinstance(saved_item, Comment) else saved_item.selftext or saved_item.url)))

print("All saved items have been processed.")