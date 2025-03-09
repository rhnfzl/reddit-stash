import re
import logging
import requests
import time
import random
import os
from ..media_core import ignore_ssl_errors, CONFIG
from ..rate_limiting import last_request_time

# Gyazo API endpoint
GYAZO_API_BASE = "https://api.gyazo.com/api"
GYAZO_API_IMAGE = f"{GYAZO_API_BASE}/images"

# Get Gyazo settings
gyazo_access_token = CONFIG.get('Gyazo', 'access_token', fallback=None)
if not gyazo_access_token:
    # Try environment variable
    gyazo_access_token = os.environ.get('GYAZO_ACCESS_TOKEN')

if gyazo_access_token:
    logging.info("Gyazo API access token configured")
else:
    logging.warning("No Gyazo API access token configured. Some Gyazo content may not be downloaded.")

def extract_gyazo_id(url):
    """Extract the Gyazo ID from a URL."""
    # Handle various Gyazo URL formats
    patterns = [
        r'gyazo\.com/([a-f0-9]+)',  # Standard URL
        r'i\.gyazo\.com/([a-f0-9]+)\.',  # Direct image URL with extension
        r'i\.gyazo\.com/([a-f0-9]+)',  # Direct image URL without extension
        r'gyazo\.com/([a-f0-9]+)/raw',  # Raw URL
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def get_gyazo_image_info(image_id):
    """Get information about a Gyazo image using the API."""
    if not gyazo_access_token:
        logging.warning("No Gyazo API access token configured. Cannot get image info.")
        return None
    
    # Check if we need to enforce a minimum time between API calls
    current_time = time.time()
    if 'gyazo_api' in last_request_time:
        time_since_last_request = current_time - last_request_time['gyazo_api']
        min_interval = 5  # 5 seconds between Gyazo API calls
        
        if time_since_last_request < min_interval:
            wait_time = min_interval - time_since_last_request
            logging.info(f"Enforcing minimum interval for Gyazo API: waiting {wait_time:.2f}s")
            time.sleep(wait_time)
    
    headers = {
        'Authorization': f'Bearer {gyazo_access_token}'
    }
    
    try:
        api_url = f"{GYAZO_API_IMAGE}/{image_id}"
        response = requests.get(api_url, headers=headers, verify=not ignore_ssl_errors, timeout=10)
        response.raise_for_status()
        
        # Update the last request time
        last_request_time['gyazo_api'] = time.time()
        
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Failed to get Gyazo image info for {image_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response status: {e.response.status_code}")
            try:
                logging.error(f"Response content: {e.response.text}")
            except:
                pass
        return None
    except Exception as e:
        logging.error(f"Unexpected error getting Gyazo image info for {image_id}: {e}")
        return None

def extract_gyazo_image_url(image_id):
    """Extract the direct image URL from a Gyazo image ID."""
    # Try API first if access token is available
    if gyazo_access_token:
        image_info = get_gyazo_image_info(image_id)
        if image_info and 'url' in image_info:
            logging.info(f"Successfully retrieved Gyazo image {image_id} via API")
            return image_info['url']
    
    # If API fails or is not available, try direct URL construction
    logging.info(f"Trying fallback URLs for Gyazo image {image_id}")
    
    # Try different formats
    urls_to_try = [
        f"https://i.gyazo.com/{image_id}.png",
        f"https://i.gyazo.com/{image_id}.jpg",
        f"https://i.gyazo.com/{image_id}.gif",
        f"https://i.gyazo.com/{image_id}.mp4",
        f"https://i.gyazo.com/{image_id}",  # No extension
    ]
    
    for url in urls_to_try:
        try:
            response = requests.head(url, verify=not ignore_ssl_errors, timeout=5)
            if response.status_code == 200:
                logging.info(f"Found working Gyazo URL: {url}")
                return url
        except requests.RequestException:
            continue
    
    # If all direct URLs fail, try scraping the page
    try:
        # Import here to avoid circular imports
        from ..http_utils import get_service_headers
        
        headers = get_service_headers('gyazo', use_random_ua=True)
        page_url = f"https://gyazo.com/{image_id}"
        response = requests.get(page_url, headers=headers, verify=not ignore_ssl_errors, timeout=10)
        
        # Look for the image URL in the page
        image_url_match = re.search(r'<meta property="og:image" content="([^"]+)"', response.text)
        if image_url_match:
            image_url = image_url_match.group(1)
            logging.info(f"Found Gyazo image URL from page: {image_url}")
            return image_url
    except Exception as e:
        logging.error(f"Failed to scrape Gyazo page for {image_id}: {e}")
    
    # If all fallbacks fail, return the most likely URL as a last resort
    last_resort_url = f"https://i.gyazo.com/{image_id}.png"
    logging.warning(f"All Gyazo fallbacks failed, returning last resort URL: {last_resort_url}")
    return last_resort_url

# Alias for backward compatibility
get_gyazo_direct_url = extract_gyazo_image_url

def is_gyazo_gif(url):
    """Check if a Gyazo URL is a GIF."""
    # If it's already a direct .gif URL, return True
    if url.endswith('.gif'):
        return True
    
    # Extract the ID and check the content type
    image_id = extract_gyazo_id(url)
    if not image_id:
        return False
    
    # Try API first if access token is available
    if gyazo_access_token:
        image_info = get_gyazo_image_info(image_id)
        if image_info:
            # Check if it's a GIF based on the type
            if image_info.get('type') == 'gif':
                return True
            # Check if it's a GIF based on the URL
            if 'url' in image_info and image_info['url'].endswith('.gif'):
                return True
    
    # If API fails or is not available, try checking the content type
    try:
        # Try the .gif URL directly
        gif_url = f"https://i.gyazo.com/{image_id}.gif"
        response = requests.head(gif_url, verify=not ignore_ssl_errors, timeout=5)
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '')
            if 'gif' in content_type.lower():
                return True
    except requests.RequestException:
        pass
    
    return False

def retry_gyazo_download(gyazo_id, save_directory, item_id, media_type='image'):
    """Retry downloading a Gyazo image."""
    logging.info(f"Retrying Gyazo download for ID: {gyazo_id}")
    
    # Determine if it's a GIF
    if media_type == 'image' and is_gyazo_gif(f"https://gyazo.com/{gyazo_id}"):
        logging.info(f"Detected that Gyazo {gyazo_id} is a GIF, changing media type to 'gif'")
        media_type = 'gif'
    
    # Try to get the direct URL
    direct_url = extract_gyazo_image_url(gyazo_id)
    if not direct_url:
        logging.error(f"Failed to get direct URL for Gyazo ID: {gyazo_id}")
        return None
    
    # Import here to avoid circular imports
    from ..download import download_media
    
    # Download the media
    return download_media(direct_url, save_directory, item_id, media_type)

def recover_gyazo_content(url, item_id, save_directory):
    """Try to recover deleted Gyazo content using web archives."""
    gyazo_id = extract_gyazo_id(url)
    if not gyazo_id:
        logging.error(f"Failed to extract Gyazo ID from URL: {url}")
        return None
    
    logging.info(f"Attempting to recover deleted Gyazo content: {gyazo_id}")
    
    # Try the Wayback Machine
    try:
        # Import here to avoid circular imports
        from ..http_utils import get_service_headers
        
        headers = get_service_headers('wayback', use_random_ua=True)
        wayback_url = f"https://web.archive.org/web/2/https://gyazo.com/{gyazo_id}"
        
        logging.info(f"Checking Wayback Machine for Gyazo {gyazo_id}: {wayback_url}")
        
        response = requests.get(wayback_url, headers=headers, verify=not ignore_ssl_errors, timeout=15)
        
        # Look for the image URL in the page
        image_url_match = re.search(r'<meta property="og:image" content="([^"]+)"', response.text)
        if image_url_match:
            image_url = image_url_match.group(1)
            logging.info(f"Found archived Gyazo image URL: {image_url}")
            
            # Import here to avoid circular imports
            from ..download import download_media
            
            # Download the media
            return download_media(image_url, save_directory, item_id, 'image')
    except Exception as e:
        logging.error(f"Failed to recover Gyazo content from Wayback Machine: {e}")
    
    logging.warning(f"Failed to recover deleted Gyazo content: {gyazo_id}")
    return None 