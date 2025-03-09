import os
import re
import json
import requests
import time
import logging
from urllib.parse import urlparse, parse_qs
import configparser
from PIL import Image
import io
import warnings
from urllib3.exceptions import InsecureRequestWarning
import random
import sys
import datetime

# Dynamically determine the path to the root directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load configuration
def load_config():
    """Load configuration from settings.ini file."""
    # Construct the full path to the settings.ini file
    config_path = os.path.join(BASE_DIR, 'settings.ini')
    
    # Load settings from the settings.ini file
    config = configparser.ConfigParser()
    config.read(config_path)
    
    return config

CONFIG = load_config()

# Common settings
ignore_ssl_errors = CONFIG.getboolean('Settings', 'ignore_ssl_errors', fallback=False)
thumbnail_size = CONFIG.getint('Media', 'thumbnail_size', fallback=800)
max_image_size = CONFIG.getint('Media', 'max_image_size', fallback=5000000)  # 5MB default
video_quality = CONFIG.get('Media', 'video_quality', fallback='high')

# Retry settings
max_retries = CONFIG.getint('Network', 'max_retries', fallback=3)
retry_delay = CONFIG.getfloat('Network', 'retry_delay', fallback=1.0)
retry_backoff_factor = CONFIG.getfloat('Network', 'retry_backoff_factor', fallback=2.0)
retry_jitter = CONFIG.getfloat('Network', 'retry_jitter', fallback=0.2)

# Get the save directory from settings.ini
save_directory = CONFIG.get('Settings', 'save_directory', fallback='reddit/')
# Make sure the path is absolute
if not os.path.isabs(save_directory):
    save_directory = os.path.join(BASE_DIR, save_directory)
# Ensure the directory exists
os.makedirs(save_directory, exist_ok=True)

# Recovery settings - first check environment variables, then fallback to settings.ini
try:
    # Check environment variables first
    use_wayback_machine = os.environ.get('USE_WAYBACK_MACHINE')
    if use_wayback_machine is not None:
        use_wayback_machine = use_wayback_machine.lower() == 'true'
    else:
        use_wayback_machine = CONFIG.getboolean('Recovery', 'use_wayback_machine', fallback=True)
        
    use_pushshift_api = os.environ.get('USE_PUSHSHIFT_API')
    if use_pushshift_api is not None:
        use_pushshift_api = use_pushshift_api.lower() == 'true'
    else:
        use_pushshift_api = CONFIG.getboolean('Recovery', 'use_pushshift_api', fallback=True)
        
    use_reddit_previews = os.environ.get('USE_REDDIT_PREVIEWS')
    if use_reddit_previews is not None:
        use_reddit_previews = use_reddit_previews.lower() == 'true'
    else:
        use_reddit_previews = CONFIG.getboolean('Recovery', 'use_reddit_previews', fallback=True)
        
    use_reveddit_api = os.environ.get('USE_REVEDDIT_API')
    if use_reveddit_api is not None:
        use_reveddit_api = use_reveddit_api.lower() == 'true'
    else:
        use_reveddit_api = CONFIG.getboolean('Recovery', 'use_reveddit_api', fallback=True)
        
    recovery_timeout = os.environ.get('RECOVERY_TIMEOUT')
    if recovery_timeout is not None:
        recovery_timeout = int(recovery_timeout)
    else:
        recovery_timeout = CONFIG.getint('Recovery', 'timeout_seconds', fallback=10)
except (configparser.NoSectionError, configparser.NoOptionError):
    # Default values if section doesn't exist
    use_wayback_machine = os.environ.get('USE_WAYBACK_MACHINE', 'true').lower() == 'true'
    use_pushshift_api = os.environ.get('USE_PUSHSHIFT_API', 'true').lower() == 'true'
    use_reddit_previews = os.environ.get('USE_REDDIT_PREVIEWS', 'true').lower() == 'true'
    use_reveddit_api = os.environ.get('USE_REVEDDIT_API', 'true').lower() == 'true'
    recovery_timeout = int(os.environ.get('RECOVERY_TIMEOUT', '10'))

# Suppress InsecureRequestWarning when ignore_ssl_errors is enabled
if ignore_ssl_errors:
    warnings.simplefilter('ignore', InsecureRequestWarning)

# Track URLs that have already had recovery failure warnings logged
recovery_failure_warnings = set()

# Track URLs that have already had download failure errors logged
download_failure_errors = set()

# Basic media handling functions
def get_valid_extensions(media_type):
    """Get valid file extensions for different media types."""
    if media_type == 'image':
        return ['.jpg', '.jpeg', '.png', '.gif', '.webp']
    elif media_type == 'video':
        return ['.mp4', '.webm', '.mov', '.avi', '.mkv']
    elif media_type == 'audio':
        return ['.mp3', '.wav', '.ogg', '.m4a', '.flac']
    else:
        return []

def get_default_extension(media_type):
    """Get default extension for a media type."""
    if media_type == 'image':
        return '.jpg'
    elif media_type == 'video':
        return '.mp4'
    elif media_type == 'audio':
        return '.mp3'
    elif media_type == 'thumbnail':
        return '.jpg'
    else:
        return '.bin'

def clean_url(url):
    """
    Clean a URL that might contain markdown formatting or other issues.
    
    Args:
        url: The URL to clean
        
    Returns:
        The cleaned URL
    """
    if not url:
        return url
    
    original_url = url
    
    # Special case for Gyazo URLs with markdown formatting
    if 'gyazo.com' in url and '](' in url:
        # Import here to avoid circular imports
        from .service_handlers.gyazo import extract_gyazo_id, extract_gyazo_image_url
        gyazo_id = extract_gyazo_id(url)
        if gyazo_id:
            direct_url = extract_gyazo_image_url(gyazo_id)
            if direct_url:
                logging.info(f"Converted complex Gyazo URL to direct URL: {direct_url}")
                return direct_url
    
    # Handle markdown links: [text](url)
    if '](' in url:
        try:
            url = url.split('](')[1].rstrip(')')
        except IndexError:
            logging.warning(f"Failed to parse markdown link: {original_url}")
    
    # Handle nested markdown links or other malformed URLs
    if '](' in url:
        try:
            url = url.split('](')[1].rstrip(')')
        except IndexError:
            logging.warning(f"Failed to parse nested markdown link: {original_url}")
    
    # Handle URLs with trailing brackets or parentheses
    url = url.rstrip(')]')
    
    # Handle URLs with encoded characters
    url = url.replace('%5D', ']').replace('%5B', '[').replace('%28', '(').replace('%29', ')')
    
    # Remove any text after a space (sometimes URLs have trailing text)
    if ' ' in url:
        url = url.split(' ')[0]
    
    # Handle special case for Imgur URLs with trailing brackets
    if 'imgur.com' in url and ']' in url:
        url = url.split(']')[0]
    
    # Fix URLs with triple slashes (http:///example.com)
    url = re.sub(r'(https?:)/+', r'\1//', url)
    
    # Fix Reddit URLs that are missing the domain
    if url.startswith('http://r/') or url.startswith('https://r/'):
        url = url.replace('http://r/', 'https://www.reddit.com/r/')
        url = url.replace('https://r/', 'https://www.reddit.com/r/')
    
    # Handle special case for Gyazo URLs
    if 'gyazo.com' in url and not url.startswith('https://i.gyazo.com/'):
        # Import here to avoid circular imports
        from .service_handlers.gyazo import extract_gyazo_id, extract_gyazo_image_url
        gyazo_id = extract_gyazo_id(url)
        if gyazo_id:
            direct_url = extract_gyazo_image_url(gyazo_id)
            if direct_url:
                logging.info(f"Converted Gyazo URL to direct URL: {direct_url}")
                return direct_url
    
    return url

def get_domain_from_url(url):
    """Extract the domain from a URL."""
    if not url:
        return None
        
    try:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        
        # Handle URLs without scheme
        if not domain and parsed_url.path:
            # Try to extract domain from path
            parts = parsed_url.path.split('/', 1)
            if len(parts) > 0:
                domain = parts[0]
        
        return domain.lower()
    except Exception as e:
        logging.error(f"Failed to extract domain from URL {url}: {e}")
        return None

def is_dns_error(error):
    """Check if an error is a DNS resolution error."""
    error_str = str(error).lower()
    dns_error_indicators = [
        "nodename nor servname provided",
        "name or service not known",
        "temporary failure in name resolution",
        "name resolution failed",
        "no address associated with hostname",
        "failed to establish a new connection",
        "getaddrinfo failed"
    ]
    
    for indicator in dns_error_indicators:
        if indicator in error_str:
            return True
            
    return False 