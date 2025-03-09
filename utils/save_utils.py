import os
import requests
import configparser
import warnings
import time
from urllib3.exceptions import InsecureRequestWarning
from datetime import datetime
from praw.models import Submission, Comment
from utils.time_utilities import lazy_load_comments, dynamic_sleep, exponential_backoff
from utils.media import (
    download_image, download_video, download_audio, 
    generate_thumbnail, get_domain_from_url,
    detect_and_download_media
)
from utils.media.service_handlers import (
    identify_service,
    is_album_or_gallery,
    download_album_or_gallery
)
import re

# Dynamically determine the path to the root directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Construct the full path to the settings.ini file
config_path = os.path.join(BASE_DIR, 'settings.ini')

# Load settings from the settings.ini file
config = configparser.ConfigParser()
config.read(config_path)
ignore_ssl_errors = config.getboolean('Settings', 'ignore_ssl_errors', fallback=False)
download_videos = config.getboolean('Media', 'download_videos', fallback=True)
download_images = config.getboolean('Media', 'download_images', fallback=True)
download_audio = config.getboolean('Media', 'download_audio', fallback=True)

# Suppress InsecureRequestWarning when ignore_ssl_errors is enabled
if ignore_ssl_errors:
    warnings.simplefilter('ignore', InsecureRequestWarning)

# Track request failures for specific domains
domain_failures = {}

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

def get_domain_from_url(url):
    """Extract the domain from a URL."""
    try:
        # Handle URLs without scheme
        if not url.startswith('http'):
            return None
        
        from urllib.parse import urlparse
        parsed_url = urlparse(url)
        return parsed_url.netloc
    except:
        return None

def download_image(image_url, save_directory, submission_id):
    """Download an image from the given URL and save it locally."""
    try:
        # Handle URLs without scheme
        if not image_url.startswith('http'):
            image_url = 'http://' + image_url
            
        # Apply rate limiting based on domain
        domain = get_domain_from_url(image_url)
        if domain:
            # Initialize failure count if not present
            if domain not in domain_failures:
                domain_failures[domain] = 0
                
            # Apply dynamic sleep based on domain and failure count
            # Higher failure count = longer sleep time
            content_length = len(image_url)  # Use URL length as a proxy for content size
            sleep_time = dynamic_sleep(content_length, domain_failures[domain])
            
            # Add extra delay for known rate-limited domains
            if 'imgur.com' in domain:
                sleep_time = max(sleep_time, 5.0)  # At least 5 seconds for Imgur
            elif 'i.redd.it' in domain:
                sleep_time = max(sleep_time, 2.0)  # At least 2 seconds for Reddit images
                
            time.sleep(sleep_time)
        
        # Use the ignore_ssl_errors setting when making the request
        response = requests.get(image_url, verify=not ignore_ssl_errors)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)
        
        # Reset failure count on success
        if domain and domain in domain_failures:
            domain_failures[domain] = 0
        
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
        # Increment failure count for this domain
        if domain and domain in domain_failures:
            domain_failures[domain] += 1
            
        print(f"Failed to download image from {image_url}: {e}")
        return None

def save_submission(submission, f, unsave=False):
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
            # Detect and download media
            media_type, media_path, thumbnail_path = detect_and_download_media(submission, os.path.dirname(f.name))
            
            if media_type == 'image':
                if thumbnail_path:
                    # Use thumbnail with link to full image
                    f.write(f"[![Image]({thumbnail_path})]({media_path})\n")
                    f.write(f"**Original Image URL:** [Link]({submission.url})\n")
                elif media_path:
                    f.write(f"![Image]({media_path})\n")
                    f.write(f"**Original Image URL:** [Link]({submission.url})\n")
                else:
                    f.write(f"![Image]({submission.url})\n")  # Fallback to the URL if download fails
            
            elif media_type == 'video':
                if media_path:
                    f.write(f"[Video]({media_path})\n")
                    f.write(f"**Original Video URL:** [Link]({submission.url})\n")
                else:
                    f.write(f"[Video]({submission.url})\n")  # Fallback to the URL if download fails
            
            elif media_type == 'audio':
                if media_path:
                    f.write(f"[Audio]({media_path})\n")
                    f.write(f"**Original Audio URL:** [Link]({submission.url})\n")
                else:
                    f.write(f"[Audio]({submission.url})\n")  # Fallback to the URL if download fails
            
            elif media_type == 'album':
                if media_path:
                    # For albums, link to the index file
                    album_dir = os.path.dirname(media_path)
                    album_rel_path = os.path.relpath(album_dir, os.path.dirname(f.name))
                    index_file = os.path.basename(media_path)
                    
                    f.write(f"[Imgur Album]({os.path.join(album_rel_path, index_file)})\n")
                    f.write(f"**Original Album URL:** [Link]({submission.url})\n")
                    f.write(f"*This album has been downloaded locally. Click the link above to view it.*\n")
                else:
                    f.write(f"[Imgur Album]({submission.url})\n")  # Fallback to the URL if download fails
            
            elif media_type == 'youtube':
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

def save_comment_and_context(comment, f, unsave=False):
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
            save_submission(parent, f)  # Save the parent post context

        elif isinstance(parent, Comment):
            f.write(f'## Context: Parent Comment by /u/{parent.author.name if parent.author else "[deleted]"}\n')
            f.write(f'- **Upvotes:** {parent.score} | **Permalink:** [Link](https://reddit.com{parent.permalink})\n')
            f.write(f'{parent.body}\n\n')

            # Recursively save the parent comment's context
            save_comment_and_context(parent, f)

        # Save child comments if any exist
        if comment.replies:
            f.write('\n\n## Child Comments:\n\n')
            process_comments(comment.replies, f)

        # Unsave the comment if requested
        if unsave:
            try:
                comment.unsave()
                print(f"Unsaved comment: {comment.id}")
            except Exception as e:
                print(f"Failed to unsave comment {comment.id}: {e}")

    except Exception as e:
        print(f"Error saving comment {comment.id}: {e}")

def process_comments(comments, f, depth=0, simple_format=False):
    """Process all comments and visualize depth using indentation."""
    for i, comment in enumerate(comments):
        if isinstance(comment, Comment):
            indent = '    ' * depth
            f.write(f'{indent}### Comment {i+1} by /u/{comment.author.name if comment.author else "[deleted]"}\n')
            f.write(f'{indent}- **Upvotes:** {comment.score} | **Permalink:** [Link](https://reddit.com{comment.permalink})\n')

            # Check for media URLs in the comment body
            comment_text = comment.body
            
            # Extract URLs from the comment
            urls = re.findall(r'(https?://[^\s]+)', comment_text)
            
            # Process each URL for media
            for url in urls:
                # Clean up URL (remove trailing punctuation)
                clean_url = url.rstrip('.,;:!?)')
                
                # Check for image URLs
                if any(clean_url.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                    image_path = download_image(clean_url, os.path.dirname(f.name), comment.id)
                    if image_path:
                        # Generate thumbnail for large images
                        thumbnail_path = generate_thumbnail(image_path, os.path.dirname(f.name), comment.id)
                        if thumbnail_path:
                            # Replace URL with thumbnail link in the comment text
                            comment_text = comment_text.replace(
                                url, f"[![Image]({thumbnail_path})]({image_path}) (Original: {clean_url})"
                            )
                        else:
                            # Replace URL with image link in the comment text
                            comment_text = comment_text.replace(
                                url, f"![Image]({image_path}) (Original: {clean_url})"
                            )
                
                # Check for video URLs
                elif (any(clean_url.endswith(ext) for ext in ['.mp4', '.webm', '.mov']) or
                      'v.redd.it' in clean_url or 'gfycat.com' in clean_url or
                      ('imgur.com' in clean_url and '.gifv' in clean_url) or
                      'streamable.com' in clean_url):
                    
                    video_path = download_video(clean_url, os.path.dirname(f.name), comment.id)
                    if video_path:
                        # Replace URL with video link in the comment text
                        comment_text = comment_text.replace(
                            url, f"[Video]({video_path}) (Original: {clean_url})"
                        )
                
                # Check for audio URLs
                elif any(clean_url.endswith(ext) for ext in ['.mp3', '.wav', '.ogg', '.m4a', '.flac']):
                    audio_path = download_audio(clean_url, os.path.dirname(f.name), comment.id)
                    if audio_path:
                        # Replace URL with audio link in the comment text
                        comment_text = comment_text.replace(
                            url, f"[Audio]({audio_path}) (Original: {clean_url})"
                        )
                
                # Handle YouTube URLs
                elif "youtube.com" in clean_url or "youtu.be" in clean_url:
                    video_id = extract_video_id(clean_url)
                    if video_id:
                        # Replace URL with YouTube thumbnail in the comment text
                        comment_text = comment_text.replace(
                            url, f"[![Video](https://img.youtube.com/vi/{video_id}/0.jpg)]({clean_url})"
                        )
            
            # Write the processed comment text
            f.write(f'{indent}{comment_text}\n\n')

            # Recursively process child comments
            if not simple_format and comment.replies:
                process_comments(comment.replies, f, depth + 1)

            f.write(f'{indent}---\n\n')