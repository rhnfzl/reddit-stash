import logging
import time
import random
import requests
from ..media_core import ignore_ssl_errors
from ..media_core import (
    max_retries,
    retry_delay,
    retry_backoff_factor,
    retry_jitter,
)

def retry_with_exponential_backoff(func, *args, **kwargs):
    """
    Retry a function with exponential backoff.
    
    Args:
        func: The function to retry
        *args: Arguments to pass to the function
        **kwargs: Keyword arguments to pass to the function
        
    Returns:
        The result of the function call, or None if all retries failed
    """
    attempt = 0
    while attempt < max_retries:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            attempt += 1
            if attempt >= max_retries:
                logging.error(f"Failed after {max_retries} attempts: {e}")
                return None
                
            # Calculate backoff time with jitter
            delay = retry_delay * (retry_backoff_factor ** (attempt - 1))
            jittered_delay = delay * (1 + random.uniform(-retry_jitter, retry_jitter))
            
            logging.warning(f"Attempt {attempt} failed: {e}. Retrying in {jittered_delay:.2f} seconds...")
            time.sleep(jittered_delay)
    
    return None

def retry_download_with_fallbacks(service, content_id, save_directory, item_id, media_type='image'):
    """
    Generic function to retry downloading content using fallback URLs.
    
    Args:
        service: The service name ('imgur', 'gfycat', 'gyazo', 'reddit')
        content_id: The content ID
        save_directory: Directory to save the media in
        item_id: Unique ID for the media file
        media_type: Type of media ('image', 'video', 'audio', 'thumbnail', 'recovered')
        
    Returns:
        Path to the saved media file or None if download failed
    """
    logging.info(f"Retrying {service} download for ID: {content_id} with alternative methods")
    
    # Import here to avoid circular imports
    from ..http_utils import generate_fallback_urls, get_service_headers
    from ..download import download_media
    
    # Get fallback URLs for this service
    fallback_urls = generate_fallback_urls(service, content_id)
    
    for url in fallback_urls:
        try:
            # Add a delay between attempts
            time.sleep(random.uniform(5, 10))
            
            # Get appropriate headers for this service with a random user agent
            headers = get_service_headers(service, use_random_ua=True)
            
            logging.info(f"Trying alternative {service} URL: {url}")
            response = requests.get(url, headers=headers, verify=not ignore_ssl_errors, timeout=30)
            
            if response.status_code == 200:
                # Determine the file extension from the URL or content type
                import os
                from ..media_core import get_valid_extensions, get_default_extension
                
                extension = os.path.splitext(url)[1].lower()
                if not extension or extension not in get_valid_extensions(media_type):
                    content_type = response.headers.get('Content-Type', '')
                    if 'image/jpeg' in content_type:
                        extension = '.jpg'
                    elif 'image/png' in content_type:
                        extension = '.png'
                    elif 'image/gif' in content_type:
                        extension = '.gif'
                    elif 'video/mp4' in content_type:
                        extension = '.mp4'
                    else:
                        extension = get_default_extension(media_type)
                
                # Create a unique filename
                prefix = f"{media_type.upper()}_" if media_type != 'image' else ""
                filename = f"{prefix}{item_id}{extension}"
                file_path = os.path.join(save_directory, filename)
                
                # Save the media file
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                
                logging.info(f"Successfully downloaded {service} content using alternative URL: {url}")
                return file_path
        except Exception as e:
            # Skip logging for DNS errors to reduce noise
            from ..media_core import is_dns_error
            if not is_dns_error(e):
                logging.warning(f"Failed to download from alternative {service} URL {url}: {e}")
    
    # Service-specific last resort methods
    if service == 'imgur':
        # Try the Imgur API as a last resort
        try:
            from .imgur import get_imgur_client
            client = get_imgur_client()
            if client:
                try:
                    image = client.get_image(content_id)
                    if image and hasattr(image, 'link'):
                        return download_media(image.link, save_directory, item_id, media_type)
                except Exception as e:
                    logging.warning(f"Failed to get Imgur image via API: {e}")
        except Exception:
            pass
    elif service == 'gfycat':
        # Try to use a web archive as a last resort
        try:
            archive_url = f"https://web.archive.org/web/*/https://gfycat.com/{content_id}"
            logging.info(f"Trying Web Archive for Gfycat content: {archive_url}")
            # This is just informational - we can't easily extract content from web.archive.org
        except Exception:
            pass
    elif service == 'gyazo':
        # Try the Gyazo API as a last resort
        try:
            api_url = f"https://api.gyazo.com/api/oembed?url=https://gyazo.com/{content_id}"
            response = requests.get(api_url, timeout=10, verify=not ignore_ssl_errors)
            if response.status_code == 200:
                data = response.json()
                if 'url' in data:
                    image_url = data['url']
                    logging.info(f"Found Gyazo image URL via API: {image_url}")
                    return download_media(image_url, save_directory, item_id, media_type)
        except Exception as e:
            logging.warning(f"Failed to get Gyazo image URL via API: {e}")
    
    logging.error(f"All alternative {service} download methods failed for ID: {content_id}")
    return None 

def get_content_type_from_url(url):
    """
    Determine the content type based on the URL.
    
    Args:
        url: The URL to analyze
        
    Returns:
        A string indicating the content type ('image', 'video', 'gif', or None)
    """
    if not url:
        return None
        
    url_lower = url.lower()
    
    # Check for image extensions
    if any(url_lower.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp']):
        return 'image'
        
    # Check for video extensions
    if any(url_lower.endswith(ext) for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv', '.flv']):
        return 'video'
        
    # Check for GIF extensions
    if url_lower.endswith('.gif'):
        return 'gif'
        
    # Check for service-specific patterns
    if 'imgur.com' in url_lower:
        return 'image'  # Default to image for Imgur
    elif 'gfycat.com' in url_lower:
        return 'video'  # Default to video for Gfycat
    elif 'gyazo.com' in url_lower:
        return 'image'  # Default to image for Gyazo
    elif 'streamable.com' in url_lower:
        return 'video'  # Default to video for Streamable
    elif 'redd.it' in url_lower or 'reddit.com' in url_lower:
        # For Reddit, check for specific patterns
        if 'v.redd.it' in url_lower:
            return 'video'
        elif 'i.redd.it' in url_lower:
            return 'image'
            
    # If we can't determine the type, return None
    return None

def get_appropriate_extension(url, content_type=None):
    """
    Determine the appropriate file extension based on the URL and content type.
    
    Args:
        url: The URL to analyze
        content_type: Optional content type hint
        
    Returns:
        A string with the file extension (including the dot)
    """
    if not url:
        return None
        
    url_lower = url.lower()
    
    # Check if the URL already has a valid extension
    known_extensions = [
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp',
        '.mp4', '.webm', '.mov', '.avi', '.mkv', '.flv'
    ]
    
    for ext in known_extensions:
        if url_lower.endswith(ext):
            return ext
            
    # If no extension found, use the content type to determine one
    if not content_type:
        content_type = get_content_type_from_url(url)
        
    if content_type == 'image':
        return '.jpg'  # Default image extension
    elif content_type == 'video':
        return '.mp4'  # Default video extension
    elif content_type == 'gif':
        return '.gif'  # GIF extension
        
    # If we can't determine the extension, default to .jpg
    return '.jpg' 