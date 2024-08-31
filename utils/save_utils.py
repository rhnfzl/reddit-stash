import os
from datetime import datetime
from praw.models import Submission, Comment
from utils.time_utilities import lazy_load_comments

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