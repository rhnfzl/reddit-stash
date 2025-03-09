import re
import logging
import requests
import time
import random
from ..media_core import (
    ignore_ssl_errors, 
    use_wayback_machine, 
    use_pushshift_api, 
    use_reddit_previews, 
    use_reveddit_api,
    recovery_timeout,
    recovery_failure_warnings
)
from utils.time_utilities import exponential_backoff

def get_wayback_url(original_url):
    """Try to find a copy of the URL in the Wayback Machine."""
    if not use_wayback_machine:
        return None
        
    try:
        # First check if the URL exists in the Wayback Machine
        check_url = f"https://archive.org/wayback/available?url={original_url}"
        
        # Use dynamic timeout based on request complexity and add retry logic
        attempt = 0
        max_attempts = 3
        while attempt < max_attempts:
            try:
                # Calculate dynamic timeout based on URL length as a proxy for complexity
                # with a minimum of 5 seconds and maximum of recovery_timeout
                dynamic_timeout = min(recovery_timeout, max(5, len(original_url) * 0.2))
                # Add jitter to avoid thundering herd problem
                jittered_timeout = dynamic_timeout * random.uniform(0.8, 1.2)
                
                logging.info(f"Requesting Wayback Machine data with {jittered_timeout:.2f}s timeout (attempt {attempt+1}/{max_attempts})")
                response = requests.get(check_url, timeout=jittered_timeout)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if 'archived_snapshots' in data and data['archived_snapshots'] and 'closest' in data['archived_snapshots']:
                        wayback_url = data['archived_snapshots']['closest']['url']
                        logging.info(f"Found Wayback Machine snapshot for {original_url}: {wayback_url}")
                        return wayback_url
                    
                    logging.info(f"No Wayback Machine snapshot found for {original_url}")
                    break  # Success or no data found, exit the retry loop
                elif response.status_code == 429:  # Too Many Requests
                    logging.warning(f"Rate limited by Wayback Machine API (attempt {attempt+1}/{max_attempts})")
                    exponential_backoff(attempt)
                else:
                    logging.warning(f"Wayback Machine API returned status code {response.status_code} (attempt {attempt+1}/{max_attempts})")
                    break  # Non-recoverable error, exit the retry loop
            except requests.exceptions.Timeout:
                logging.warning(f"Wayback Machine API request timed out after {jittered_timeout:.2f}s (attempt {attempt+1}/{max_attempts})")
            except requests.exceptions.RequestException as e:
                logging.warning(f"Wayback Machine API request failed: {e} (attempt {attempt+1}/{max_attempts})")
            
            attempt += 1
            if attempt < max_attempts:
                exponential_backoff(attempt)
        
        return None
    except Exception as e:
        logging.error(f"Failed to check Wayback Machine for {original_url}: {e}")
        return None

def get_pushshift_url(url, item_id=None, submission_id=None):
    """Try to find media content using PullPush API (successor to Pushshift)."""
    if not use_pushshift_api:
        return None
        
    try:
        # PullPush is the successor to Pushshift
        base_url = "https://api.pullpush.io"
        
        # If we have a submission_id, we can try to get the submission data
        if submission_id:
            pushshift_url = f"{base_url}/reddit/submission/search?ids={submission_id}"
            # Use dynamic timeout based on request complexity and add retry logic
            attempt = 0
            max_attempts = 3
            while attempt < max_attempts:
                try:
                    # Calculate dynamic timeout based on submission ID length as a proxy for complexity
                    # with a minimum of 5 seconds and maximum of recovery_timeout
                    dynamic_timeout = min(recovery_timeout, max(5, len(submission_id) * 0.5))
                    # Add jitter to avoid thundering herd problem
                    jittered_timeout = dynamic_timeout * random.uniform(0.8, 1.2)
                    
                    logging.info(f"Requesting PullPush data with {jittered_timeout:.2f}s timeout (attempt {attempt+1}/{max_attempts})")
                    response = requests.get(pushshift_url, timeout=jittered_timeout)
                    
                    if response.status_code == 200:
                        data = response.json()
                        if 'data' in data and len(data['data']) > 0:
                            submission_data = data['data'][0]
                            # Check for preview images
                            if 'preview' in submission_data and 'images' in submission_data['preview']:
                                images = submission_data['preview']['images']
                                if len(images) > 0 and 'source' in images[0]:
                                    source_url = images[0]['source']['url']
                                    logging.info(f"Found PullPush preview image for submission {submission_id}: {source_url}")
                                    return source_url
                            
                            # Check for direct URL
                            if 'url' in submission_data:
                                logging.info(f"Found PullPush URL for submission {submission_id}: {submission_data['url']}")
                                return submission_data['url']
                        break  # Success, exit the retry loop
                    elif response.status_code == 429:  # Too Many Requests
                        logging.warning(f"Rate limited by PullPush API (attempt {attempt+1}/{max_attempts})")
                        exponential_backoff(attempt)
                    else:
                        logging.warning(f"PullPush API returned status code {response.status_code} (attempt {attempt+1}/{max_attempts})")
                        break  # Non-recoverable error, exit the retry loop
                except requests.exceptions.Timeout:
                    logging.warning(f"PullPush API request timed out after {jittered_timeout:.2f}s (attempt {attempt+1}/{max_attempts})")
                except requests.exceptions.RequestException as e:
                    logging.warning(f"PullPush API request failed: {e} (attempt {attempt+1}/{max_attempts})")
                
                attempt += 1
                if attempt < max_attempts:
                    exponential_backoff(attempt)
        
        # If we have a comment ID, we can try to get the comment data
        if item_id and item_id.startswith('t1_'):
            comment_id = item_id.split('_')[1]
            pushshift_url = f"{base_url}/reddit/comment/search?ids={comment_id}"
            
            # Use dynamic timeout based on request complexity and add retry logic
            attempt = 0
            max_attempts = 3
            while attempt < max_attempts:
                try:
                    # Calculate dynamic timeout based on comment ID length as a proxy for complexity
                    # with a minimum of 5 seconds and maximum of recovery_timeout
                    dynamic_timeout = min(recovery_timeout, max(5, len(comment_id) * 0.5))
                    # Add jitter to avoid thundering herd problem
                    jittered_timeout = dynamic_timeout * random.uniform(0.8, 1.2)
                    
                    logging.info(f"Requesting PullPush comment data with {jittered_timeout:.2f}s timeout (attempt {attempt+1}/{max_attempts})")
                    response = requests.get(pushshift_url, timeout=jittered_timeout)
                    
                    if response.status_code == 200:
                        data = response.json()
                        if 'data' in data and len(data['data']) > 0:
                            comment_data = data['data'][0]
                            # Extract URLs from the comment body
                            if 'body' in comment_data:
                                urls = re.findall(r'(https?://[^\s]+)', comment_data['body'])
                                for found_url in urls:
                                    # Check if the URL is an image or video
                                    if any(found_url.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.webm']):
                                        logging.info(f"Found media URL in PullPush comment {comment_id}: {found_url}")
                                        return found_url
                        break  # Success, exit the retry loop
                    elif response.status_code == 429:  # Too Many Requests
                        logging.warning(f"Rate limited by PullPush API (attempt {attempt+1}/{max_attempts})")
                        exponential_backoff(attempt)
                    else:
                        logging.warning(f"PullPush API returned status code {response.status_code} (attempt {attempt+1}/{max_attempts})")
                        break  # Non-recoverable error, exit the retry loop
                except requests.exceptions.Timeout:
                    logging.warning(f"PullPush API request timed out after {jittered_timeout:.2f}s (attempt {attempt+1}/{max_attempts})")
                except requests.exceptions.RequestException as e:
                    logging.warning(f"PullPush API request failed: {e} (attempt {attempt+1}/{max_attempts})")
                
                attempt += 1
                if attempt < max_attempts:
                    exponential_backoff(attempt)
        
        # If PullPush fails, try the Reddit Undelete service as a fallback
        if submission_id:
            undelete_url = f"https://undelete.pullpush.io/r/all/comments/{submission_id}"
            logging.info(f"Trying Reddit Undelete service: {undelete_url}")
            # This is just a URL for manual viewing, not an API endpoint
            # We can't programmatically extract content from it, but it's useful for users
            
        logging.info(f"No PullPush data found for {url}")
        return None
    except Exception as e:
        logging.error(f"Failed to check PullPush API for {url}: {e}")
        return None

def get_reddit_preview_url(url, submission=None):
    """Try to extract preview URLs from Reddit's API response."""
    if not use_reddit_previews or not submission:
        return None
        
    try:
        # Check if the submission has preview data
        if hasattr(submission, 'preview') and 'images' in submission.preview:
            images = submission.preview['images']
            if len(images) > 0 and 'source' in images[0]:
                source_url = images[0]['source']['url']
                # Reddit encodes HTML entities in URLs, so we need to decode them
                source_url = source_url.replace('&amp;', '&')
                logging.info(f"Found Reddit preview image: {source_url}")
                return source_url
                
        # Check for thumbnail
        if hasattr(submission, 'thumbnail') and submission.thumbnail.startswith('http'):
            logging.info(f"Found Reddit thumbnail: {submission.thumbnail}")
            return submission.thumbnail
            
        logging.info(f"No Reddit preview found for {url}")
        return None
    except Exception as e:
        logging.error(f"Failed to extract Reddit preview for {url}: {e}")
        return None

def get_reveddit_url(url, submission_id=None):
    """Try to find media content using Reveddit API."""
    if not use_reveddit_api or not submission_id:
        return None
        
    try:
        # Reveddit API for retrieving removed content
        reveddit_url = f"https://api.reveddit.com/v1/submission?ids={submission_id}"
        
        # Use dynamic timeout based on request complexity and add retry logic
        attempt = 0
        max_attempts = 3
        while attempt < max_attempts:
            try:
                # Calculate dynamic timeout based on submission ID length as a proxy for complexity
                # with a minimum of 5 seconds and maximum of recovery_timeout
                dynamic_timeout = min(recovery_timeout, max(5, len(submission_id) * 0.5))
                # Add jitter to avoid thundering herd problem
                jittered_timeout = dynamic_timeout * random.uniform(0.8, 1.2)
                
                logging.info(f"Requesting Reveddit data with {jittered_timeout:.2f}s timeout (attempt {attempt+1}/{max_attempts})")
                response = requests.get(reveddit_url, timeout=jittered_timeout)
                
                if response.status_code == 200:
                    data = response.json()
                    if submission_id in data:
                        submission_data = data[submission_id]
                        
                        # Check for URL
                        if 'url' in submission_data:
                            media_url = submission_data['url']
                            if media_url and (media_url.startswith('http') or media_url.startswith('https')):
                                logging.info(f"Found Reveddit URL for submission {submission_id}: {media_url}")
                                return media_url
                        
                        # Check for media metadata
                        if 'media_metadata' in submission_data:
                            for media_id, media_info in submission_data['media_metadata'].items():
                                if 's' in media_info and 'u' in media_info['s']:
                                    media_url = media_info['s']['u']
                                    logging.info(f"Found Reveddit media URL for submission {submission_id}: {media_url}")
                                    return media_url
                    break  # Success or no data found, exit the retry loop
                elif response.status_code == 429:  # Too Many Requests
                    logging.warning(f"Rate limited by Reveddit API (attempt {attempt+1}/{max_attempts})")
                    exponential_backoff(attempt)
                else:
                    logging.warning(f"Reveddit API returned status code {response.status_code} (attempt {attempt+1}/{max_attempts})")
                    break  # Non-recoverable error, exit the retry loop
            except requests.exceptions.Timeout:
                logging.warning(f"Reveddit API request timed out after {jittered_timeout:.2f}s (attempt {attempt+1}/{max_attempts})")
            except requests.exceptions.RequestException as e:
                logging.warning(f"Reveddit API request failed: {e} (attempt {attempt+1}/{max_attempts})")
            
            attempt += 1
            if attempt < max_attempts:
                exponential_backoff(attempt)
        
        logging.info(f"No Reveddit data found for {url}")
        return None
    except Exception as e:
        logging.error(f"Failed to check Reveddit API for {url}: {e}")
        return None

def recover_deleted_media(url, item_id, save_directory, submission=None):
    """Try multiple methods to recover deleted media content."""
    logging.info(f"Attempting to recover deleted media from {url}")
    
    # Try method 1: Check Reddit's cached previews
    if submission:
        preview_url = get_reddit_preview_url(url, submission)
        if preview_url:
            logging.info(f"Recovering media using Reddit preview: {preview_url}")
            # Import here to avoid circular imports
            from ..download import download_media
            return download_media(preview_url, save_directory, f"recovered_{item_id}", 'recovered')
    
    # Try method 2: Check PullPush API (successor to Pushshift)
    submission_id = None
    if submission:
        submission_id = submission.id
    pushshift_url = get_pushshift_url(url, item_id, submission_id)
    if pushshift_url:
        logging.info(f"Recovering media using PullPush API: {pushshift_url}")
        # Import here to avoid circular imports
        from ..download import download_media
        return download_media(pushshift_url, save_directory, f"recovered_{item_id}", 'recovered')
    
    # Try method 3: Check Reveddit API
    reveddit_url = get_reveddit_url(url, submission_id)
    if reveddit_url:
        logging.info(f"Recovering media using Reveddit API: {reveddit_url}")
        # Import here to avoid circular imports
        from ..download import download_media
        return download_media(reveddit_url, save_directory, f"recovered_{item_id}", 'recovered')
    
    # Try method 4: Check Wayback Machine
    wayback_url = get_wayback_url(url)
    if wayback_url:
        logging.info(f"Recovering media using Wayback Machine: {wayback_url}")
        # Import here to avoid circular imports
        from ..download import download_media
        return download_media(wayback_url, save_directory, f"recovered_{item_id}", 'recovered')
    
    # Try method 5: Try service-specific fallbacks based on URL pattern
    service = None
    content_id = None
    
    # Check for Imgur URLs
    if 'imgur.com' in url:
        service = 'imgur'
        # Import here to avoid circular imports
        from .imgur import extract_imgur_id
        imgur_id = extract_imgur_id(url)
        if imgur_id:
            content_id = imgur_id
    # Check for Gfycat URLs
    elif 'gfycat.com' in url:
        service = 'gfycat'
        # Import here to avoid circular imports
        from .gfycat import extract_gfycat_id
        gfycat_id = extract_gfycat_id(url)
        if gfycat_id:
            content_id = gfycat_id
    # Check for Gyazo URLs
    elif 'gyazo.com' in url:
        service = 'gyazo'
        # Import here to avoid circular imports
        from .gyazo import extract_gyazo_id
        gyazo_id = extract_gyazo_id(url)
        if gyazo_id:
            content_id = gyazo_id
    # Check for Reddit URLs
    elif 'redd.it' in url or 'reddit.com' in url:
        service = 'reddit'
        # Import here to avoid circular imports
        from .reddit import extract_reddit_id
        reddit_id = extract_reddit_id(url)
        if reddit_id:
            content_id = reddit_id
    
    # If we identified a service and content ID, try service-specific recovery
    if service and content_id:
        logging.info(f"Trying {service}-specific recovery for {content_id}")
        # Import here to avoid circular imports
        from .service_utils import retry_download_with_fallbacks
        result = retry_download_with_fallbacks(service, content_id, save_directory, f"recovered_{item_id}", 'recovered')
        if result:
            logging.info(f"Successfully recovered {service} content: {result}")
            return result
    
    # If all recovery methods fail, try our existing Imgur recovery as a last resort
    if 'imgur.com' in url:
        logging.info(f"Trying Imgur-specific recovery methods for {url}")
        # Import here to avoid circular imports
        from .imgur import recover_imgur_content
        return recover_imgur_content(url, item_id, save_directory)
    
    # Only log the warning once per URL
    if url not in recovery_failure_warnings:
        logging.warning(f"All recovery methods failed for {url}")
        recovery_failure_warnings.add(url)
    
    return None 