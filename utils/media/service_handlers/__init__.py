"""
Service handlers for various media services.

This package contains modules for handling different media services like:
- Reddit
- Imgur
- Gfycat
- Gyazo
- Streamable
- Recovery services

Each module provides functions for extracting media IDs and URLs from the respective service.
"""

# Import all service handlers for easy access
from .reddit import extract_reddit_post_id, extract_reddit_id
from .imgur import extract_imgur_id, extract_imgur_image_url, extract_imgur_album_urls, extract_imgur_gallery_urls, extract_imgur_album_id, download_imgur_album
from .gfycat import extract_gfycat_id, extract_gfycat_url
from .gyazo import extract_gyazo_id, extract_gyazo_image_url
from .streamable import extract_streamable_video_url
from .recovery import recover_deleted_media
from .service_utils import retry_download_with_fallbacks

# Define the list of supported services
SUPPORTED_SERVICES = [
    'reddit',
    'imgur',
    'gfycat',
    'gyazo',
    'streamable'
]

# Define service-specific functions
SERVICE_HANDLERS = {
    'imgur': {
        'extract_id': extract_imgur_id,
        'extract_url': extract_imgur_image_url,
        'is_album': extract_imgur_album_id,
        'download_album': download_imgur_album
    },
    'gfycat': {
        'extract_id': extract_gfycat_id,
        'extract_url': extract_gfycat_url
    },
    'gyazo': {
        'extract_id': extract_gyazo_id,
        'extract_url': extract_gyazo_image_url
    },
    'reddit': {
        'extract_id': extract_reddit_post_id
    }
}

def identify_service(url):
    """Identify which service a URL belongs to."""
    if not url:
        return None
        
    url = url.lower()
    
    if 'imgur.com' in url or 'i.imgur.com' in url:
        return 'imgur'
    elif 'gfycat.com' in url or 'thumbs.gfycat.com' in url:
        return 'gfycat'
    elif 'gyazo.com' in url or 'i.gyazo.com' in url:
        return 'gyazo'
    elif 'reddit.com' in url or 'redd.it' in url or 'i.redd.it' in url or 'v.redd.it' in url:
        return 'reddit'
    elif 'streamable.com' in url:
        return 'streamable'
    
    return None

def extract_content_id(url):
    """Extract the content ID from a URL based on the service."""
    service = identify_service(url)
    if not service:
        return None, None
    
    extract_id_func = get_service_handler(service, 'extract_id')
    if not extract_id_func:
        return None, None
    
    content_id = extract_id_func(url)
    return service, content_id

def is_album_or_gallery(url):
    """Check if a URL is an album or gallery."""
    service = identify_service(url)
    if not service:
        return False
    
    # Check for Imgur albums
    if service == 'imgur':
        album_id = extract_imgur_album_id(url)
        return bool(album_id)
    
    # Check for Reddit galleries
    if service == 'reddit':
        # Import here to avoid circular imports
        from .reddit import is_reddit_gallery
        return is_reddit_gallery(url)
    
    return False

def download_album_or_gallery(url, save_directory, item_id):
    """Download an album or gallery based on the service."""
    service = identify_service(url)
    if not service:
        return None
    
    if service == 'imgur':
        return download_imgur_album(url, save_directory, item_id)
    
    if service == 'reddit':
        # Import here to avoid circular imports
        from .reddit import download_reddit_gallery
        return download_reddit_gallery(url, save_directory, item_id)
    
    return None

def get_service_handler(service, handler_type):
    """Get a service-specific handler function."""
    if service in SERVICE_HANDLERS and handler_type in SERVICE_HANDLERS[service]:
        return SERVICE_HANDLERS[service][handler_type]
    return None

__all__ = [
    'extract_reddit_post_id',
    'extract_reddit_id',
    'extract_imgur_id',
    'extract_imgur_image_url',
    'extract_imgur_album_urls',
    'extract_imgur_gallery_urls',
    'extract_gfycat_id',
    'extract_gfycat_url',
    'extract_gyazo_id',
    'extract_gyazo_image_url',
    'extract_streamable_video_url',
    'recover_deleted_media',
    'SUPPORTED_SERVICES',
    'identify_service',
    'extract_content_id',
    'is_album_or_gallery',
    'download_album_or_gallery',
    'get_service_handler',
    'retry_download_with_fallbacks'
] 