import os
import re
import logging
import requests
import time
import random
from imgurpython import ImgurClient
from imgurpython.helpers.error import ImgurClientError
from ..media_core import ignore_ssl_errors, CONFIG
from ..rate_limiting import ApiKeyRotator, last_request_time

# Imgur specific settings
# First check environment variable, then fallback to settings.ini
imgur_client_ids_env = os.environ.get('IMGUR_CLIENT_IDS') or os.environ.get('IMGUR_CLIENT_ID')
imgur_client_secrets_env = os.environ.get('IMGUR_CLIENT_SECRETS') or os.environ.get('IMGUR_CLIENT_SECRET')

# Log the config path to ensure we're reading the right file
imgur_client_ids_config = CONFIG.get('Imgur', 'client_ids', fallback=None)
imgur_client_secrets_config = CONFIG.get('Imgur', 'client_secrets', fallback=None)

# Log the raw values for debugging
logging.info(f"Imgur client IDs from config: {imgur_client_ids_config}")
if imgur_client_ids_config and imgur_client_ids_config.lower() != 'none':
    logging.info(f"First few chars of config client ID: {imgur_client_ids_config[:10]}...")

# Use environment variable if available, otherwise use config
if imgur_client_ids_env:
    # Split by comma and strip whitespace if it contains commas, otherwise use as single value
    if ',' in imgur_client_ids_env:
        imgur_client_ids = [id.strip() for id in imgur_client_ids_env.split(',')]
    else:
        imgur_client_ids = [imgur_client_ids_env.strip()]
    logging.info(f"Using {len(imgur_client_ids)} Imgur client IDs from environment variable")
elif imgur_client_ids_config and imgur_client_ids_config.lower() != 'none':
    imgur_client_ids = [id.strip() for id in imgur_client_ids_config.split(',')]
    logging.info(f"Using {len(imgur_client_ids)} Imgur client IDs from settings.ini")
else:
    imgur_client_ids = []
    logging.warning("No Imgur client IDs configured. Some Imgur content may not be downloaded.")

# Handle client secrets (optional for anonymous usage)
if imgur_client_secrets_env:
    # Split by comma and strip whitespace if it contains commas, otherwise use as single value
    if ',' in imgur_client_secrets_env:
        imgur_client_secrets = [secret.strip() for secret in imgur_client_secrets_env.split(',')]
    else:
        imgur_client_secrets = [imgur_client_secrets_env.strip()]
    logging.info(f"Using {len(imgur_client_secrets)} Imgur client secrets from environment variable")
elif imgur_client_secrets_config and imgur_client_secrets_config.lower() != 'none':
    imgur_client_secrets = [secret.strip() for secret in imgur_client_secrets_config.split(',')]
    logging.info(f"Using {len(imgur_client_secrets)} Imgur client secrets from settings.ini")
else:
    # For anonymous usage, we can use None for all client secrets
    imgur_client_secrets = [None] * len(imgur_client_ids)
    logging.info("No Imgur client secrets configured. Using anonymous access mode.")

# Ensure the lists have the same length
if len(imgur_client_ids) > len(imgur_client_secrets):
    # Pad with None for missing secrets
    imgur_client_secrets.extend([None] * (len(imgur_client_ids) - len(imgur_client_secrets)))
elif len(imgur_client_secrets) > len(imgur_client_ids):
    # Truncate extra secrets
    imgur_client_secrets = imgur_client_secrets[:len(imgur_client_ids)]

# Create a list of (client_id, client_secret) tuples
imgur_credentials = list(zip(imgur_client_ids, imgur_client_secrets))

# Initialize the Imgur API key rotator
imgur_key_rotator = ApiKeyRotator(imgur_credentials)

# Get Imgur settings
download_albums = CONFIG.getboolean('Imgur', 'download_albums', fallback=True)
max_album_images = CONFIG.getint('Imgur', 'max_album_images', fallback=50)
recover_deleted = CONFIG.getboolean('Imgur', 'recover_deleted', fallback=True)

def get_imgur_client():
    """Get an Imgur client using the key rotator."""
    # Check if we need to enforce a minimum time between API calls
    current_time = time.time()
    if 'imgur_api' in last_request_time:
        time_since_last_request = current_time - last_request_time['imgur_api']
        min_interval = 30  # 30 seconds between Imgur API calls
        
        if time_since_last_request < min_interval:
            wait_time = min_interval - time_since_last_request
            logging.info(f"Enforcing minimum interval for Imgur API: waiting {wait_time:.2f}s")
            time.sleep(wait_time)
    
    client_id, client_secret = imgur_key_rotator.get_next_key() if imgur_key_rotator.has_keys() else (None, None)
    
    if not client_id:
        logging.warning("No Imgur API client IDs configured. Some Imgur content may not be downloaded.")
        logging.warning("To avoid 429 rate limit errors, register for free Imgur API credentials")
        logging.warning("Available credentials: " + str(len(imgur_credentials)))
        return None
    
    logging.info(f"Attempting to create Imgur client with client_id: {client_id[:4]}...")
        
    try:
        client = ImgurClient(client_id, client_secret)
        logging.info(f"Successfully created Imgur client with client_id: {client_id[:4]}...")
        
        # Update the last request time for Imgur API
        last_request_time['imgur_api'] = time.time()
        
        # Test the client with a simple API call
        try:
            credits = client.credits
            logging.info(f"Imgur API credits remaining: {credits['ClientRemaining']}/{credits['ClientLimit']}")
            logging.info(f"Imgur API reset time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(credits['UserReset']))}")
            
            # If we're running low on credits, add a longer delay
            if credits['ClientRemaining'] < 50:
                logging.warning(f"Low on Imgur API credits ({credits['ClientRemaining']}). Adding extra delay.")
                time.sleep(random.uniform(20, 30))
        except Exception as e:
            logging.warning(f"Could not retrieve Imgur API credits: {e}")
            
        return client
    except ImgurClientError as e:
        logging.error(f"Imgur API client error: {e}")
        if "401" in str(e):
            logging.error("Invalid Imgur credentials. Check your client_id and client_secret.")
        elif "429" in str(e):
            logging.error("Imgur rate limit exceeded. Consider adding more client IDs or increasing rate limiting delays.")
            # Add a long delay to recover from rate limiting
            time.sleep(random.uniform(60, 120))
        return None
    except Exception as e:
        logging.error(f"Failed to create Imgur client: {e}")
        return None

def extract_imgur_album_images(album_id):
    """Extract all image URLs from an Imgur album using the Imgur API."""
    client = get_imgur_client()
    if not client:
        # Fallback to direct URL construction for simple cases
        return [f"https://i.imgur.com/{album_id}.jpg"]
    
    try:
        images = client.get_album_images(album_id)
        
        # Limit the number of images if max_album_images is set
        if max_album_images > 0 and len(images) > max_album_images:
            logging.info(f"Limiting album {album_id} to {max_album_images} images (out of {len(images)})")
            images = images[:max_album_images]
            
        return [image.link for image in images]
    except ImgurClientError as e:
        logging.error(f"Imgur API error for album {album_id}: {e}")
        return []
    except Exception as e:
        logging.error(f"Failed to extract Imgur album images for {album_id}: {e}")
        return []

def extract_imgur_image_url(image_id):
    """Extract the direct image URL from an Imgur image ID using the API if available."""
    # Try with API first if available
    client = get_imgur_client()
    if client:
        try:
            image = client.get_image(image_id)
            if image and hasattr(image, 'link'):
                logging.info(f"Successfully retrieved Imgur image {image_id} via API")
                return image.link
        except ImgurClientError as e:
            logging.warning(f"Imgur API error for image {image_id}: {e}")
        except Exception as e:
            logging.warning(f"Failed to extract Imgur image URL for {image_id} via API: {e}")
    
    # If API fails or is not available, try direct URL construction
    logging.info(f"Trying fallback URLs for Imgur image {image_id}")
    # Import here to avoid circular imports
    from ..http_utils import generate_fallback_urls
    fallback_urls = generate_fallback_urls('imgur', image_id)
    
    for url in fallback_urls:
        try:
            test_response = requests.head(url, timeout=5, verify=not ignore_ssl_errors)
            if test_response.status_code == 200:
                logging.info(f"Found working Imgur fallback URL: {url}")
                return url
        except requests.RequestException as e:
            logging.debug(f"Failed to access Imgur fallback URL {url}: {e}")
            continue
    
    # If all fallbacks fail, return the most likely URL as a last resort
    last_resort_url = f"https://i.imgur.com/{image_id}.jpg"
    logging.warning(f"All Imgur fallbacks failed, returning last resort URL: {last_resort_url}")
    return last_resort_url

def extract_imgur_video_url(url):
    """Extract the direct video URL from an Imgur URL."""
    # Try to extract the video URL from the page
    try:
        # Import here to avoid circular imports
        from ..http_utils import get_service_headers
        
        headers = get_service_headers('imgur', use_random_ua=True)
        response = requests.get(url, headers=headers, verify=not ignore_ssl_errors, timeout=10)
        response.raise_for_status()
        
        # Look for the video URL in the page
        video_url_match = re.search(r'"contentUrl":\s*"([^"]+\.mp4)"', response.text)
        if video_url_match:
            video_url = video_url_match.group(1)
            logging.info(f"Found Imgur video URL: {video_url}")
            return video_url
            
        # Try another pattern
        video_url_match = re.search(r'<source\s+src="([^"]+\.mp4)"', response.text)
        if video_url_match:
            video_url = video_url_match.group(1)
            logging.info(f"Found Imgur video URL (alternative pattern): {video_url}")
            return video_url
            
        # Try to find the gifv URL and convert to mp4
        if url.endswith('.gifv'):
            mp4_url = url.replace('.gifv', '.mp4')
            logging.info(f"Converting Imgur gifv URL to mp4: {mp4_url}")
            return mp4_url
            
        logging.warning(f"Could not extract video URL from Imgur page: {url}")
        return None
    except Exception as e:
        logging.error(f"Failed to extract Imgur video URL: {e}")
        return None

def extract_imgur_id(url):
    """Extract the Imgur ID from a URL."""
    # Handle various Imgur URL formats
    patterns = [
        r'imgur\.com/(?:a|gallery)/([a-zA-Z0-9]+)',  # Album or gallery
        r'imgur\.com/([a-zA-Z0-9]+)',  # Direct image
        r'i\.imgur\.com/([a-zA-Z0-9]+)\.',  # i.imgur.com with extension
        r'i\.imgur\.com/([a-zA-Z0-9]+)',  # i.imgur.com without extension
        r'imgur\.com/download/([a-zA-Z0-9]+)',  # Download URL
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def extract_imgur_album_id(url):
    """Extract the Imgur album ID from a URL."""
    # Handle album URL formats
    patterns = [
        r'imgur\.com/a/([a-zA-Z0-9]+)',  # Album
        r'imgur\.com/gallery/([a-zA-Z0-9]+)',  # Gallery
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def download_imgur_album(album_url, save_directory, item_id):
    """Download all images from an Imgur album."""
    album_id = extract_imgur_album_id(album_url)
    if not album_id:
        logging.error(f"Failed to extract album ID from URL: {album_url}")
        return None
    
    logging.info(f"Downloading Imgur album: {album_id}")
    
    # Get all image URLs in the album
    image_urls = extract_imgur_album_images(album_id)
    if not image_urls:
        logging.error(f"Failed to extract images from Imgur album: {album_id}")
        return None
    
    logging.info(f"Found {len(image_urls)} images in Imgur album {album_id}")
    
    # Download each image
    downloaded_files = []
    for i, image_url in enumerate(image_urls):
        # Create a unique ID for each image in the album
        image_item_id = f"{item_id}_album_{album_id}_{i+1}"
        
        # Import here to avoid circular imports
        from ..download import download_image
        
        image_path = download_image(image_url, save_directory, image_item_id)
        if image_path:
            downloaded_files.append(image_path)
            logging.info(f"Downloaded image {i+1}/{len(image_urls)} from album {album_id}")
        else:
            logging.warning(f"Failed to download image {i+1}/{len(image_urls)} from album {album_id}")
        
        # Add a small delay between downloads to avoid rate limiting
        time.sleep(random.uniform(1, 3))
    
    logging.info(f"Downloaded {len(downloaded_files)}/{len(image_urls)} images from Imgur album {album_id}")
    
    # Return the first downloaded file as the representative file
    return downloaded_files[0] if downloaded_files else None

def recover_imgur_content(url, item_id, save_directory):
    """Try to recover deleted Imgur content."""
    # This is a placeholder for future implementation
    # Could use the Wayback Machine, archive.is, or other services
    logging.info(f"Attempting to recover deleted Imgur content: {url}")
    return None

def retry_imgur_download(imgur_id, save_directory, item_id, media_type='image'):
    """Retry downloading an Imgur image or video."""
    logging.info(f"Retrying Imgur download for ID: {imgur_id}")
    
    # Try to get the direct URL
    direct_url = extract_imgur_image_url(imgur_id)
    if not direct_url:
        logging.error(f"Failed to get direct URL for Imgur ID: {imgur_id}")
        return None
    
    # Import here to avoid circular imports
    from ..download import download_media
    
    # Download the media
    return download_media(direct_url, save_directory, item_id, media_type)

def extract_imgur_album_urls(album_id):
    """
    Extract all image URLs from an Imgur album.
    
    Args:
        album_id: The Imgur album ID
        
    Returns:
        A list of direct image URLs from the album
    """
    return extract_imgur_album_images(album_id)

def extract_imgur_gallery_urls(gallery_id):
    """
    Extract all image URLs from an Imgur gallery.
    
    Args:
        gallery_id: The Imgur gallery ID
        
    Returns:
        A list of direct image URLs from the gallery
    """
    client = get_imgur_client()
    if not client:
        # Fallback to direct URL construction for simple cases
        return [f"https://i.imgur.com/{gallery_id}.jpg"]
    
    try:
        # Try to get gallery images
        gallery = client.gallery_item(gallery_id)
        
        # Check if it's an album
        if hasattr(gallery, 'is_album') and gallery.is_album:
            return extract_imgur_album_images(gallery.id)
        
        # If it's a single image
        if hasattr(gallery, 'link'):
            return [gallery.link]
            
        return []
    except ImgurClientError as e:
        logging.error(f"Imgur API error for gallery {gallery_id}: {e}")
        return []
    except Exception as e:
        logging.error(f"Failed to extract Imgur gallery images for {gallery_id}: {e}")
        return [] 