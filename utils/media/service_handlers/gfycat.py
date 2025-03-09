import re
import logging
import requests
import time
import random
import json
from ..media_core import ignore_ssl_errors, CONFIG
from ..rate_limiting import last_request_time

# Gfycat API endpoints
GFYCAT_API_BASE = "https://api.gfycat.com/v1"
GFYCAT_API_TOKEN = f"{GFYCAT_API_BASE}/oauth/token"
GFYCAT_API_GFYCAT = f"{GFYCAT_API_BASE}/gfycats"

# Get Gfycat settings
gfycat_client_id = CONFIG.get('Gfycat', 'client_id', fallback=None)
gfycat_client_secret = CONFIG.get('Gfycat', 'client_secret', fallback=None)

# Initialize token storage
gfycat_token = None
gfycat_token_expiry = 0

def get_gfycat_token():
    """Get a Gfycat API token."""
    global gfycat_token, gfycat_token_expiry
    
    # Check if we have a valid token
    current_time = time.time()
    if gfycat_token and current_time < gfycat_token_expiry:
        return gfycat_token
    
    # Check if we have credentials
    if not gfycat_client_id or not gfycat_client_secret:
        logging.warning("No Gfycat API credentials configured. Using anonymous access.")
        return None
    
    # Get a new token
    try:
        # Check if we need to enforce a minimum time between API calls
        if 'gfycat_api' in last_request_time:
            time_since_last_request = current_time - last_request_time['gfycat_api']
            min_interval = 10  # 10 seconds between Gfycat API calls
            
            if time_since_last_request < min_interval:
                wait_time = min_interval - time_since_last_request
                logging.info(f"Enforcing minimum interval for Gfycat API: waiting {wait_time:.2f}s")
                time.sleep(wait_time)
        
        payload = {
            "grant_type": "client_credentials",
            "client_id": gfycat_client_id,
            "client_secret": gfycat_client_secret
        }
        
        response = requests.post(GFYCAT_API_TOKEN, json=payload, verify=not ignore_ssl_errors, timeout=10)
        response.raise_for_status()
        
        # Update the last request time
        last_request_time['gfycat_api'] = time.time()
        
        token_data = response.json()
        gfycat_token = token_data.get('access_token')
        
        # Set token expiry (subtract 60 seconds for safety)
        expires_in = token_data.get('expires_in', 3600)
        gfycat_token_expiry = time.time() + expires_in - 60
        
        logging.info(f"Got new Gfycat API token, expires in {expires_in} seconds")
        return gfycat_token
    except requests.RequestException as e:
        logging.error(f"Failed to get Gfycat API token: {e}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error getting Gfycat API token: {e}")
        return None

def extract_gfycat_id(url):
    """Extract the Gfycat ID from a URL."""
    # Handle various Gfycat URL formats
    patterns = [
        r'gfycat\.com/(?:gifs/detail/)?([a-zA-Z0-9]+)',  # Standard URL
        r'gfycat\.com/(?:ifr/)?([a-zA-Z0-9]+)',  # Iframe URL
        r'thumbs\.gfycat\.com/([a-zA-Z0-9]+)-',  # Thumbnail URL
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def get_gfycat_info(gfycat_id):
    """Get information about a Gfycat using the API."""
    token = get_gfycat_token()
    
    # Check if we need to enforce a minimum time between API calls
    current_time = time.time()
    if 'gfycat_api' in last_request_time:
        time_since_last_request = current_time - last_request_time['gfycat_api']
        min_interval = 5  # 5 seconds between Gfycat API calls
        
        if time_since_last_request < min_interval:
            wait_time = min_interval - time_since_last_request
            logging.info(f"Enforcing minimum interval for Gfycat API: waiting {wait_time:.2f}s")
            time.sleep(wait_time)
    
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    
    try:
        api_url = f"{GFYCAT_API_GFYCAT}/{gfycat_id}"
        response = requests.get(api_url, headers=headers, verify=not ignore_ssl_errors, timeout=10)
        response.raise_for_status()
        
        # Update the last request time
        last_request_time['gfycat_api'] = time.time()
        
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Failed to get Gfycat info for {gfycat_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response status: {e.response.status_code}")
            try:
                logging.error(f"Response content: {e.response.text}")
            except:
                pass
        return None
    except Exception as e:
        logging.error(f"Unexpected error getting Gfycat info for {gfycat_id}: {e}")
        return None

def extract_gfycat_url(gfycat_id, prefer_mp4=True):
    """Extract the direct video URL from a Gfycat ID."""
    # Try API first
    gfycat_info = get_gfycat_info(gfycat_id)
    
    if gfycat_info and 'gfyItem' in gfycat_info:
        item = gfycat_info['gfyItem']
        
        # Check if the item is available
        if item.get('isDeleted', False):
            logging.warning(f"Gfycat {gfycat_id} has been deleted")
            return None
        
        # Get the best URL based on preference
        if prefer_mp4 and 'mp4Url' in item:
            logging.info(f"Using mp4 URL for Gfycat {gfycat_id}")
            return item['mp4Url']
        elif 'webmUrl' in item:
            logging.info(f"Using webm URL for Gfycat {gfycat_id}")
            return item['webmUrl']
        elif 'gifUrl' in item:
            logging.info(f"Using gif URL for Gfycat {gfycat_id}")
            return item['gifUrl']
        elif 'mobileUrl' in item:
            logging.info(f"Using mobile URL for Gfycat {gfycat_id}")
            return item['mobileUrl']
    
    # If API fails or no suitable URL found, try direct URL construction
    logging.info(f"API failed, trying direct URL construction for Gfycat {gfycat_id}")
    
    # Try different formats
    urls_to_try = [
        f"https://giant.gfycat.com/{gfycat_id}.mp4",
        f"https://thumbs.gfycat.com/{gfycat_id}-size_restricted.gif",
        f"https://zippy.gfycat.com/{gfycat_id}.mp4",
        f"https://fat.gfycat.com/{gfycat_id}.mp4",
    ]
    
    for url in urls_to_try:
        try:
            response = requests.head(url, verify=not ignore_ssl_errors, timeout=5)
            if response.status_code == 200:
                logging.info(f"Found working direct URL for Gfycat {gfycat_id}: {url}")
                return url
        except requests.RequestException:
            continue
                
    # If all direct URLs fail, return the most likely URL as a last resort
    last_resort_url = f"https://thumbs.gfycat.com/{gfycat_id}-size_restricted.gif"
    logging.warning(f"All Gfycat fallbacks failed, returning last resort URL: {last_resort_url}")
    return last_resort_url

# Alias for backward compatibility
get_gfycat_mp4_url = extract_gfycat_url

def retry_gfycat_download(gfycat_id, save_directory, item_id, media_type='video'):
    """Retry downloading a Gfycat video."""
    logging.info(f"Retrying Gfycat download for ID: {gfycat_id}")
    
    # Try to get the direct URL
    direct_url = extract_gfycat_url(gfycat_id)
    if not direct_url:
        logging.error(f"Failed to get direct URL for Gfycat ID: {gfycat_id}")
        return None
    
    # Import here to avoid circular imports
    from ..download import download_media
    
    # Download the media
    return download_media(direct_url, save_directory, item_id, media_type)

def recover_gfycat_content(url, item_id, save_directory):
    """Try to recover deleted Gfycat content using web archives."""
    gfycat_id = extract_gfycat_id(url)
    if not gfycat_id:
        logging.error(f"Failed to extract Gfycat ID from URL: {url}")
        return None
    
    logging.info(f"Attempting to recover deleted Gfycat content: {gfycat_id}")
    
    # Try the Wayback Machine
    try:
        # Import here to avoid circular imports
        from ..http_utils import get_service_headers
        
        headers = get_service_headers('wayback', use_random_ua=True)
        wayback_url = f"https://web.archive.org/web/2/https://gfycat.com/{gfycat_id}"
        
        logging.info(f"Checking Wayback Machine for Gfycat {gfycat_id}: {wayback_url}")
        
        response = requests.get(wayback_url, headers=headers, verify=not ignore_ssl_errors, timeout=15)
        
        # Look for the video URL in the page
        video_url_match = re.search(r'"contentUrl":\s*"([^"]+)"', response.text)
        if video_url_match:
            video_url = video_url_match.group(1)
            logging.info(f"Found archived Gfycat video URL: {video_url}")
            
            # Import here to avoid circular imports
            from ..download import download_media
            
            # Download the media
            return download_media(video_url, save_directory, item_id, 'video')
    except Exception as e:
        logging.error(f"Failed to recover Gfycat content from Wayback Machine: {e}")
    
    logging.warning(f"Failed to recover deleted Gfycat content: {gfycat_id}")
    return None 