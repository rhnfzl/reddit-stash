import os
import requests
import urllib3
from datetime import datetime
from praw.models import Submission, Comment
from utils.time_utilities import lazy_load_comments
from utils.env_config import get_ignore_tls_errors

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

def download_image(image_url, save_directory, submission_id, ignore_tls_errors=None):
    """Download an image from the given URL and save it locally."""
    try:
        # Load ignore_tls_errors setting if not provided
        if ignore_tls_errors is None:
            ignore_tls_errors = get_ignore_tls_errors()

        # Configure request parameters
        request_kwargs = {}
        if ignore_tls_errors:
            # Disable SSL warnings if ignoring TLS errors
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            request_kwargs['verify'] = False

        response = requests.get(image_url, **request_kwargs)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)
        
        # Determine the image extension from the URL
        extension = os.path.splitext(image_url)[1]
        if extension.lower() not in ['.jpg', '.jpeg', '.png', '.gif']:
            extension = '.jpg'  # Default to .jpg if the extension is unusual
        
        # Save the image with a unique name
        image_filename = f"{submission_id}{extension}"
        image_path = os.path.join(save_directory, image_filename)
        
        with open(image_path, 'wb') as f:
            f.write(response.content)
        
        return image_path
    except Exception as e:
        print(f"Failed to download image from {image_url}: {e}")
        return None

def save_submission(submission, f, unsave=False, ignore_tls_errors=None):
    """Save a submission and its metadata, optionally unsaving it after."""
    try:
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
                # Download and save the image locally
                image_path = download_image(submission.url, os.path.dirname(f.name), submission.id, ignore_tls_errors)
                if image_path:
                    f.write(f"![Image]({image_path})\n")
                    f.write(f"**Original Image URL:** [Link]({submission.url})\n")
                else:
                    f.write(f"![Image]({submission.url})\n")  # Fallback to the URL if download fails
            elif "youtube.com" in submission.url or "youtu.be" in submission.url:
                video_id = extract_video_id(submission.url)
                f.write(f"[![Video](https://img.youtube.com/vi/{video_id}/0.jpg)]({submission.url})")
            else:
                f.write(submission.url if submission.url else '[Deleted Post]')

        f.write('\n\n## Comments:\n\n')
        lazy_comments = lazy_load_comments(submission)
        process_comments(lazy_comments, f)

        # Unsave the submission if requested
        if unsave:
            try:
                submission.unsave()
                print(f"Unsaved submission: {submission.id}")
            except Exception as e:
                print(f"Failed to unsave submission {submission.id}: {e}")

    except Exception as e:
        print(f"Error saving submission {submission.id}: {e}")

def save_comment_and_context(comment, f, unsave=False, ignore_tls_errors=None):
    """Save a comment, its context, and any child comments."""
    try:
        # Save the comment itself
        f.write('---\n')  # Start of frontmatter
        f.write(f'Comment by /u/{comment.author.name if comment.author else "[deleted]"}\n')
        f.write(f'- **Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n')
        f.write(f'{comment.body}\n\n')
        f.write('---\n\n')  # End of frontmatter

        # Save the parent context
        parent = comment.parent()
        if isinstance(parent, Submission):
            f.write(f'## Context: Post by /u/{parent.author.name if parent.author else "[deleted]"}\n')
            f.write(f'- **Title:** {parent.title}\n')
            f.write(f'- **Upvotes:** {parent.score} | **Permalink:** [Link](https://reddit.com{parent.permalink})\n')
            if parent.is_self:
                f.write(f'{parent.selftext}\n\n')
            else:
                f.write(f'[Link to post content]({parent.url})\n\n')

            # Save the full submission context, including all comments
            f.write('\n\n## Full Post Context:\n\n')
            save_submission(parent, f, ignore_tls_errors=ignore_tls_errors)  # Save the parent post context

        elif isinstance(parent, Comment):
            f.write(f'## Context: Parent Comment by /u/{parent.author.name if parent.author else "[deleted]"}\n')
            f.write(f'- **Upvotes:** {parent.score} | **Permalink:** [Link](https://reddit.com{parent.permalink})\n')
            f.write(f'{parent.body}\n\n')

            # Recursively save the parent comment's context
            save_comment_and_context(parent, f, ignore_tls_errors=ignore_tls_errors)

        # Save child comments if any exist
        if comment.replies:
            f.write('\n\n## Child Comments:\n\n')
            process_comments(comment.replies, f, ignore_tls_errors=ignore_tls_errors)

        # Unsave the comment if requested
        if unsave:
            try:
                comment.unsave()
                print(f"Unsaved comment: {comment.id}")
            except Exception as e:
                print(f"Failed to unsave comment {comment.id}: {e}")

    except Exception as e:
        print(f"Error saving comment {comment.id}: {e}")

def process_comments(comments, f, depth=0, simple_format=False, ignore_tls_errors=None):
    """Process all comments with tree-like visual hierarchy without triggering markdown code blocks."""
    for i, comment in enumerate(comments):
        if isinstance(comment, Comment):
            # Create tree structure using Unicode box drawing characters
            if depth == 0:
                tree_prefix = ""
            elif i == len(comments) - 1:  # Last comment at this level
                tree_prefix = "└── "
            else:
                tree_prefix = "├── "

            # Add vertical lines for deeper nesting
            if depth > 0:
                parent_lines = "│   " * (depth - 1)
                tree_prefix = parent_lines + tree_prefix

            # Write comment header without problematic indentation
            f.write(f'\n{tree_prefix}**Comment by /u/{comment.author.name if comment.author else "[deleted]"}**\n')
            f.write(f'{tree_prefix}*Upvotes: {comment.score} | [Permalink](https://reddit.com{comment.permalink})*\n\n')

            # Process comment body with blockquotes for nested content
            comment_body = comment.body if comment.body else '[deleted]'

            # Check for image URLs in the comment body
            if any(comment.body.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
                image_url = comment.body.split()[-1]  # Assuming the URL is the last word in the comment
                image_path = download_image(image_url, os.path.dirname(f.name), comment.id, ignore_tls_errors)
                if image_path:
                    # Apply blockquote formatting for nested images
                    blockquote_prefix = "> " * max(1, depth) if depth > 0 else ""
                    f.write(f'{blockquote_prefix}![Image]({image_path})\n')
                    f.write(f'{blockquote_prefix}*Original Image URL: [Link]({image_url})*\n\n')
                else:
                    blockquote_prefix = "> " * max(1, depth) if depth > 0 else ""
                    f.write(f'{blockquote_prefix}![Image]({image_url})\n\n')
            else:
                # Apply blockquote formatting for nested text content
                lines = comment_body.split('\n')
                for line in lines:
                    if depth > 0:
                        # Use blockquotes for nested content
                        blockquote_prefix = "> " * depth
                        f.write(f'{blockquote_prefix}{line}\n')
                    else:
                        f.write(f'{line}\n')
                f.write('\n')

            # Recursively process child comments
            if not simple_format and comment.replies:
                process_comments(comment.replies, f, depth + 1, simple_format, ignore_tls_errors)

            # Add separator only for top-level comments to reduce clutter
            if depth == 0:
                f.write('---\n')