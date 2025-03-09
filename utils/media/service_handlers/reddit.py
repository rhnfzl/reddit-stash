import re
import logging
import requests
import time
import random
import os
from urllib.parse import urlparse, parse_qs
from ..media_core import ignore_ssl_errors, CONFIG
from ..rate_limiting import last_request_time

# Reddit API settings
reddit_client_id = CONFIG.get('Reddit', 'client_id', fallback=None)
reddit_client_secret = CONFIG.get('Reddit', 'client_secret', fallback=None)
reddit_username = CONFIG.get('Reddit', 'username', fallback=None)
reddit_password = CONFIG.get('Reddit', 'password', fallback=None)

# Check environment variables if not in config
if not reddit_client_id:
    reddit_client_id = os.environ.get('REDDIT_CLIENT_ID')
if not reddit_client_secret:
    reddit_client_secret = os.environ.get('REDDIT_CLIENT_SECRET')
if not reddit_username:
    reddit_username = os.environ.get('REDDIT_USERNAME')
if not reddit_password:
    reddit_password = os.environ.get('REDDIT_PASSWORD')

# Initialize token storage
reddit_token = None
reddit_token_expiry = 0

def get_reddit_token():
    """Get a Reddit API token."""
    global reddit_token, reddit_token_expiry
    
    # Check if we have a valid token
    current_time = time.time()
    if reddit_token and current_time < reddit_token_expiry:
        return reddit_token
    
    # Check if we have credentials
    if not reddit_client_id or not reddit_client_secret:
        logging.warning("No Reddit API credentials configured. Using anonymous access.")
        return None
    
    # Get a new token
    try:
        # Check if we need to enforce a minimum time between API calls
        if 'reddit_api' in last_request_time:
            time_since_last_request = current_time - last_request_time['reddit_api']
            min_interval = 10  # 10 seconds between Reddit API calls
            
            if time_since_last_request < min_interval:
                wait_time = min_interval - time_since_last_request
                logging.info(f"Enforcing minimum interval for Reddit API: waiting {wait_time:.2f}s")
                time.sleep(wait_time)
        
        auth = (reddit_client_id, reddit_client_secret)
        headers = {'User-Agent': 'RedditStash/1.0'}
        
        if reddit_username and reddit_password:
            # Use password flow
            data = {
                'grant_type': 'password',
                'username': reddit_username,
                'password': reddit_password
            }
        else:
            # Use client credentials flow
            data = {
                'grant_type': 'client_credentials'
            }
        
        response = requests.post(
            'https://www.reddit.com/api/v1/access_token',
            auth=auth,
            headers=headers,
            data=data,
            verify=not ignore_ssl_errors,
            timeout=10
        )
        response.raise_for_status()
        
        # Update the last request time
        last_request_time['reddit_api'] = time.time()
        
        token_data = response.json()
        reddit_token = token_data.get('access_token')
        
        # Set token expiry (subtract 60 seconds for safety)
        expires_in = token_data.get('expires_in', 3600)
        reddit_token_expiry = time.time() + expires_in - 60
        
        logging.info(f"Got new Reddit API token, expires in {expires_in} seconds")
        return reddit_token
    except requests.RequestException as e:
        logging.error(f"Failed to get Reddit API token: {e}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error getting Reddit API token: {e}")
        return None

def extract_reddit_post_id(url):
    """Extract the Reddit post ID from a URL."""
    # Handle various Reddit URL formats
    patterns = [
        r'reddit\.com/r/[^/]+/comments/([a-z0-9]+)',  # Standard post URL
        r'redd\.it/([a-z0-9]+)',  # Short URL
        r'reddit\.com/comments/([a-z0-9]+)',  # Direct comments URL
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

# Alias for backward compatibility
extract_reddit_id = extract_reddit_post_id

def extract_reddit_media_id(url):
    """Extract the Reddit media ID from a URL."""
    # Handle various Reddit media URL formats
    patterns = [
        r'i\.redd\.it/([a-z0-9]+)\.',  # Image
        r'v\.redd\.it/([a-z0-9]+)',  # Video
        r'preview\.redd\.it/([a-z0-9]+)\.',  # Preview
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def get_reddit_post_info(post_id):
    """Get information about a Reddit post using the API."""
    token = get_reddit_token()
    
    # Check if we need to enforce a minimum time between API calls
    current_time = time.time()
    if 'reddit_api' in last_request_time:
        time_since_last_request = current_time - last_request_time['reddit_api']
        min_interval = 5  # 5 seconds between Reddit API calls
        
        if time_since_last_request < min_interval:
            wait_time = min_interval - time_since_last_request
            logging.info(f"Enforcing minimum interval for Reddit API: waiting {wait_time:.2f}s")
            time.sleep(wait_time)
    
    headers = {'User-Agent': 'RedditStash/1.0'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    
    try:
        api_url = f"https://oauth.reddit.com/api/info?id=t3_{post_id}"
        if not token:
            # Use the public API if no token
            api_url = f"https://www.reddit.com/api/info.json?id=t3_{post_id}"
        
        response = requests.get(api_url, headers=headers, verify=not ignore_ssl_errors, timeout=10)
        response.raise_for_status()
        
        # Update the last request time
        last_request_time['reddit_api'] = time.time()
        
        data = response.json()
        if 'data' in data and 'children' in data['data'] and len(data['data']['children']) > 0:
            return data['data']['children'][0]['data']
        else:
            logging.warning(f"No data found for Reddit post {post_id}")
            return None
    except requests.RequestException as e:
        logging.error(f"Failed to get Reddit post info for {post_id}: {e}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error getting Reddit post info for {post_id}: {e}")
        return None

def extract_reddit_image_url(post_id):
    """Extract the direct image URL from a Reddit post ID."""
    # Try API first
    post_info = get_reddit_post_info(post_id)
    
    if post_info:
        # Check if it's an image post
        if post_info.get('post_hint') == 'image' and 'url' in post_info:
            logging.info(f"Found image URL for Reddit post {post_id}: {post_info['url']}")
            return post_info['url']
        
        # Check if it's a gallery
        if post_info.get('is_gallery', False) and 'gallery_data' in post_info and 'media_metadata' in post_info:
            gallery_items = post_info['gallery_data']['items']
            if gallery_items:
                # Get the first image in the gallery
                first_item = gallery_items[0]
                media_id = first_item['media_id']
                if media_id in post_info['media_metadata']:
                    media_item = post_info['media_metadata'][media_id]
                    if 's' in media_item and 'u' in media_item['s']:
                        image_url = media_item['s']['u']
                        logging.info(f"Found gallery image URL for Reddit post {post_id}: {image_url}")
                        return image_url
        
        # Check if there's a thumbnail
        if 'thumbnail' in post_info and post_info['thumbnail'] not in ['self', 'default', 'nsfw', '']:
            logging.info(f"Found thumbnail URL for Reddit post {post_id}: {post_info['thumbnail']}")
            return post_info['thumbnail']
    
    # If API fails or no suitable URL found, try direct URL construction
    logging.info(f"API failed, trying direct URL construction for Reddit post {post_id}")
    
    # Try different formats
    urls_to_try = [
        f"https://i.redd.it/{post_id}.jpg",
        f"https://i.redd.it/{post_id}.png",
        f"https://i.redd.it/{post_id}.gif",
    ]
    
    for url in urls_to_try:
        try:
            response = requests.head(url, verify=not ignore_ssl_errors, timeout=5)
            if response.status_code == 200:
                logging.info(f"Found working direct URL for Reddit post {post_id}: {url}")
                return url
        except requests.RequestException:
            continue
    
    logging.error(f"Failed to extract image URL for Reddit post {post_id}")
    return None

def extract_reddit_video_url(post_id):
    """Extract the direct video URL from a Reddit post ID."""
    # Try API first
    post_info = get_reddit_post_info(post_id)
    
    if post_info and 'media' in post_info and post_info['media']:
        # Check if it's a Reddit video
        if 'reddit_video' in post_info['media']:
            video_data = post_info['media']['reddit_video']
            if 'fallback_url' in video_data:
                video_url = video_data['fallback_url']
                logging.info(f"Found video URL for Reddit post {post_id}: {video_url}")
                return video_url
    
    # If API fails or no suitable URL found, try direct URL construction
    logging.info(f"API failed, trying direct URL construction for Reddit post {post_id}")
    
    # Try different formats
    urls_to_try = [
        f"https://v.redd.it/{post_id}/DASH_720.mp4",
        f"https://v.redd.it/{post_id}/DASH_480.mp4",
        f"https://v.redd.it/{post_id}/DASH_360.mp4",
        f"https://v.redd.it/{post_id}/DASH_240.mp4",
    ]
    
    for url in urls_to_try:
        try:
            response = requests.head(url, verify=not ignore_ssl_errors, timeout=5)
            if response.status_code == 200:
                logging.info(f"Found working direct URL for Reddit post {post_id}: {url}")
                return url
        except requests.RequestException:
            continue
    
    logging.error(f"Failed to extract video URL for Reddit post {post_id}")
    return None

def extract_reddit_gallery_urls(post_id):
    """Extract all image URLs from a Reddit gallery post."""
    # Try API first
    post_info = get_reddit_post_info(post_id)
    
    if post_info and post_info.get('is_gallery', False) and 'gallery_data' in post_info and 'media_metadata' in post_info:
        gallery_items = post_info['gallery_data']['items']
        image_urls = []
        
        for item in gallery_items:
            media_id = item['media_id']
            if media_id in post_info['media_metadata']:
                media_item = post_info['media_metadata'][media_id]
                if 's' in media_item and 'u' in media_item['s']:
                    image_url = media_item['s']['u']
                    image_urls.append(image_url)
        
        if image_urls:
            logging.info(f"Found {len(image_urls)} images in Reddit gallery {post_id}")
            return image_urls
    
    logging.error(f"Failed to extract gallery URLs for Reddit post {post_id}")
    return []

def download_reddit_gallery(post_url, save_directory, item_id):
    """Download all images from a Reddit gallery."""
    post_id = extract_reddit_post_id(post_url)
    if not post_id:
        logging.error(f"Failed to extract post ID from URL: {post_url}")
        return None
    
    logging.info(f"Downloading Reddit gallery: {post_id}")
    
    # Get all image URLs in the gallery
    image_urls = extract_reddit_gallery_urls(post_id)
    if not image_urls:
        logging.error(f"Failed to extract images from Reddit gallery: {post_id}")
        return None
    
    logging.info(f"Found {len(image_urls)} images in Reddit gallery {post_id}")
    
    # Download each image
    downloaded_files = []
    for i, image_url in enumerate(image_urls):
        # Create a unique ID for each image in the gallery
        image_item_id = f"{item_id}_gallery_{post_id}_{i+1}"
        
        # Import here to avoid circular imports
        from ..download import download_image
        
        image_path = download_image(image_url, save_directory, image_item_id)
        if image_path:
            downloaded_files.append(image_path)
            logging.info(f"Downloaded image {i+1}/{len(image_urls)} from gallery {post_id}")
        else:
            logging.warning(f"Failed to download image {i+1}/{len(image_urls)} from gallery {post_id}")
        
        # Add a small delay between downloads to avoid rate limiting
        time.sleep(random.uniform(1, 3))
    
    logging.info(f"Downloaded {len(downloaded_files)}/{len(image_urls)} images from Reddit gallery {post_id}")
    
    # Return the first downloaded file as the representative file
    return downloaded_files[0] if downloaded_files else None

def is_reddit_gallery(url):
    """Check if a Reddit URL is a gallery."""
    post_id = extract_reddit_post_id(url)
    if not post_id:
        return False
    
    # Try API first
    post_info = get_reddit_post_info(post_id)
    
    if post_info:
        return post_info.get('is_gallery', False)
    
    # If API fails, try checking the URL
    return '/gallery/' in url

def retry_reddit_download(post_id, save_directory, item_id, media_type='image'):
    """Retry downloading a Reddit image or video."""
    logging.info(f"Retrying Reddit download for ID: {post_id}")
    
    # Try to get the direct URL based on media type
    direct_url = None
    if media_type == 'image':
        direct_url = extract_reddit_image_url(post_id)
    elif media_type == 'video':
        direct_url = extract_reddit_video_url(post_id)
    
    if not direct_url:
        logging.error(f"Failed to get direct URL for Reddit ID: {post_id}")
        return None
    
    # Import here to avoid circular imports
    from ..download import download_media
    
    # Download the media
    return download_media(direct_url, save_directory, item_id, media_type)

def recover_reddit_content(url, item_id, save_directory):
    """Try to recover deleted Reddit content using web archives."""
    post_id = extract_reddit_post_id(url)
    if not post_id:
        logging.error(f"Failed to extract Reddit post ID from URL: {url}")
        return None
    
    logging.info(f"Attempting to recover deleted Reddit content: {post_id}")
    
    # Try the Wayback Machine
    try:
        # Import here to avoid circular imports
        from ..http_utils import get_service_headers
        
        headers = get_service_headers('wayback', use_random_ua=True)
        wayback_url = f"https://web.archive.org/web/2/https://www.reddit.com/comments/{post_id}"
        
        logging.info(f"Checking Wayback Machine for Reddit post {post_id}: {wayback_url}")
        
        response = requests.get(wayback_url, headers=headers, verify=not ignore_ssl_errors, timeout=15)
        
        # Look for image URLs in the page
        image_url_matches = re.findall(r'https://i\.redd\.it/[a-z0-9]+\.[a-z]+', response.text)
        if image_url_matches:
            image_url = image_url_matches[0]
            logging.info(f"Found archived Reddit image URL: {image_url}")
            
            # Import here to avoid circular imports
            from ..download import download_media
            
            # Download the media
            return download_media(image_url, save_directory, item_id, 'image')
        
        # Look for video URLs in the page
        video_url_matches = re.findall(r'https://v\.redd\.it/[a-z0-9]+/DASH_[0-9]+\.mp4', response.text)
        if video_url_matches:
            video_url = video_url_matches[0]
            logging.info(f"Found archived Reddit video URL: {video_url}")
            
            # Import here to avoid circular imports
            from ..download import download_media
            
            # Download the media
            return download_media(video_url, save_directory, item_id, 'video')
    except Exception as e:
        logging.error(f"Failed to recover Reddit content from Wayback Machine: {e}")
    
    logging.warning(f"Failed to recover deleted Reddit content: {post_id}")
    return None

def get_reddit_direct_url(post_id):
    """
    Get the direct URL for a Reddit post.
    
    Args:
        post_id: The Reddit post ID
        
    Returns:
        The direct URL to the media, or None if not found
    """
    # Try to get post info from the API
    post_info = get_reddit_post_info(post_id)
    
    if post_info:
        # Check if it's an image post
        if post_info.get('post_hint') == 'image' and 'url' in post_info:
            logging.info(f"Found direct image URL for Reddit post {post_id}: {post_info['url']}")
            return post_info['url']
        
        # Check if it's a video post
        if post_info.get('post_hint') == 'hosted:video' and 'media' in post_info and post_info['media']:
            if 'reddit_video' in post_info['media']:
                video_data = post_info['media']['reddit_video']
                if 'fallback_url' in video_data:
                    video_url = video_data['fallback_url']
                    logging.info(f"Found direct video URL for Reddit post {post_id}: {video_url}")
                    return video_url
        
        # Check if it's a link post
        if 'url' in post_info and post_info['url'] != f"https://www.reddit.com/r/{post_info.get('subreddit')}/comments/{post_id}/":
            logging.info(f"Found direct link URL for Reddit post {post_id}: {post_info['url']}")
            return post_info['url']
    
    # If API fails or no suitable URL found, try direct URL construction
    logging.info(f"API failed, trying direct URL construction for Reddit post {post_id}")
    
    # Try different formats for images
    image_urls_to_try = [
        f"https://i.redd.it/{post_id}.jpg",
        f"https://i.redd.it/{post_id}.png",
        f"https://i.redd.it/{post_id}.gif",
    ]
    
    for url in image_urls_to_try:
        try:
            response = requests.head(url, verify=not ignore_ssl_errors, timeout=5)
            if response.status_code == 200:
                logging.info(f"Found working direct image URL for Reddit post {post_id}: {url}")
                return url
        except requests.RequestException:
            continue
    
    # Try video URL
    video_url = f"https://v.redd.it/{post_id}/DASH_720.mp4"
    try:
        response = requests.head(video_url, verify=not ignore_ssl_errors, timeout=5)
        if response.status_code == 200:
            logging.info(f"Found working direct video URL for Reddit post {post_id}: {video_url}")
            return video_url
    except requests.RequestException:
        pass
    
    logging.error(f"Failed to get direct URL for Reddit post {post_id}")
    return None

def get_reddit_preview_url(post_id):
    """
    Get the preview URL for a Reddit post.
    
    Args:
        post_id: The Reddit post ID
        
    Returns:
        The preview URL to the media, or None if not found
    """
    # Try to get post info from the API
    post_info = get_reddit_post_info(post_id)
    
    if post_info:
        # Check for preview images
        if 'preview' in post_info and 'images' in post_info['preview']:
            images = post_info['preview']['images']
            if len(images) > 0 and 'source' in images[0]:
                source_url = images[0]['source']['url']
                # Reddit encodes HTML entities in URLs, so we need to decode them
                source_url = source_url.replace('&amp;', '&')
                logging.info(f"Found preview image URL for Reddit post {post_id}: {source_url}")
                return source_url
        
        # Check for thumbnail
        if 'thumbnail' in post_info and post_info['thumbnail'] not in ['self', 'default', 'nsfw', '']:
            logging.info(f"Found thumbnail URL for Reddit post {post_id}: {post_info['thumbnail']}")
            return post_info['thumbnail']
    
    # If API fails or no suitable URL found, try direct URL construction
    logging.info(f"API failed, trying direct preview URL construction for Reddit post {post_id}")
    
    # Try preview URL
    preview_url = f"https://preview.redd.it/{post_id}.jpg"
    try:
        response = requests.head(preview_url, verify=not ignore_ssl_errors, timeout=5)
        if response.status_code == 200:
            logging.info(f"Found working preview URL for Reddit post {post_id}: {preview_url}")
            return preview_url
    except requests.RequestException:
        pass
    
    logging.error(f"Failed to get preview URL for Reddit post {post_id}")
    return None 