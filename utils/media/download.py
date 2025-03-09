import os
import logging
import requests
import time
import random
from .media_core import (
    get_valid_extensions, 
    get_default_extension, 
    clean_url, 
    get_domain_from_url, 
    is_dns_error, 
    ignore_ssl_errors,
    download_failure_errors,
    max_image_size
)
from .http_utils import get_service_headers
from .rate_limiting import apply_rate_limiting, domain_failures
from .retry_queue import add_to_imgur_retry_queue
from .service_handlers import extract_content_id

def download_media(url, save_directory, item_id, media_type='image', service=None, content_id=None):
    """
    Generic function to download media (image, video, audio) from a URL.
    
    Args:
        url: URL of the media to download
        save_directory: Directory to save the downloaded media
        item_id: Unique ID for the media file (usually submission or comment ID)
        media_type: Type of media ('image', 'video', 'audio', 'thumbnail')
        service: The service from which the media is downloaded
        content_id: The content ID associated with the media
        
    Returns:
        Path to the downloaded file, or None if download failed
    """
    max_retries = 3
    retry_count = 0
    domain = None  # Initialize domain to None
    
    try:
        # Clean the URL
        original_url = url
        url = clean_url(url)
        if url != original_url:
            logging.debug(f"Cleaned URL: {original_url} -> {url}")
        
        # Identify the service and content ID
        if service is None:
            service = get_domain_from_url(url)
            if service:
                logging.info(f"Detected service: {service}")
        
        if content_id is None:
            detected_service, detected_content_id = extract_content_id(url)
            if detected_content_id:
                content_id = detected_content_id
                logging.info(f"Detected content ID: {content_id}")
                # Update service if it was detected
                if service is None and detected_service:
                    service = detected_service
                    logging.info(f"Updated service to: {service}")
        
        # Handle URLs without scheme
        if not url.startswith('http'):
            url = 'http://' + url
            
        # Apply rate limiting
        domain = apply_rate_limiting(url)
        
        while retry_count <= max_retries:
            try:
                # Get appropriate headers for this service
                headers = get_service_headers(service if service else 'default')
                
                # Use the ignore_ssl_errors setting when making the request
                response = requests.get(url, headers=headers, verify=not ignore_ssl_errors, timeout=30)
                
                # Handle rate limiting (429) specifically
                if response.status_code == 429:
                    logging.warning(f"Rate limited by {domain}. Status code: 429")
                    
                    # For Imgur URLs with 429 errors, add to the persistent retry queue
                    if 'imgur.com' in domain:
                        logging.warning(f"Imgur rate limit hit for URL: {url}")
                        logging.warning(f"Adding to retry queue with params: save_dir={save_directory}, item_id={item_id}, media_type={media_type}, service={service}, content_id={content_id}")
                        add_to_imgur_retry_queue(url, save_directory, item_id, media_type, service, content_id)
                        return None
                    
                    # For service-specific URLs with 429 errors, try the alternative download method
                    if service and content_id:
                        logging.warning(f"{service} rate limited (429) for {url}. Trying alternative download method.")
                        # Import here to avoid circular imports
                        from .service_handlers.service_utils import retry_download_with_fallbacks
                        alternative_result = retry_download_with_fallbacks(service, content_id, save_directory, item_id, media_type)
                        if alternative_result:
                            return alternative_result
                    
                    retry_count += 1
                    if retry_count <= max_retries:
                        # Calculate backoff time (exponential with jitter)
                        if service == 'imgur':
                            # More aggressive backoff for Imgur
                            backoff_time = min(300, (60 * (2 ** retry_count)) + random.uniform(0, 30))
                            logging.warning(f"Imgur rate limited (429) for {url}. Retrying in {backoff_time:.2f} seconds (attempt {retry_count}/{max_retries})")
                        else:
                            backoff_time = min(60, (2 ** retry_count) + random.uniform(0, 1))
                            logging.warning(f"Rate limited (429) for {url}. Retrying in {backoff_time:.2f} seconds (attempt {retry_count}/{max_retries})")
                        
                        time.sleep(backoff_time)
                        continue
                    else:
                        logging.error(f"Max retries exceeded for rate limited request: {url}")
                        
                        # For Imgur URLs, add to the persistent retry queue
                        if service == 'imgur' or 'imgur.com' in url or 'i.imgur.com' in url:
                            add_to_imgur_retry_queue(url, save_directory, item_id, media_type, service, content_id)
                        
                        raise requests.exceptions.HTTPError(f"429 Client Error: Rate Limited for url: {url}")
                
                # Handle 404 errors for service-specific URLs
                if response.status_code == 404 and service and content_id:
                    logging.warning(f"{service} URL not found (404): {url}. Trying alternative download method.")
                    # Import here to avoid circular imports
                    from .service_handlers.service_utils import retry_download_with_fallbacks
                    alternative_result = retry_download_with_fallbacks(service, content_id, save_directory, item_id, media_type)
                    if alternative_result:
                        return alternative_result
                
                # For other status codes, raise for status as usual
                response.raise_for_status()
                
                # Reset failure count on success
                if domain and domain in domain_failures:
                    domain_failures[domain] = 0
                
                # Determine the file extension from the URL
                extension = os.path.splitext(url)[1].lower()
                
                # Set default extensions based on media type if not found in URL
                if not extension or extension not in get_valid_extensions(media_type):
                    extension = get_default_extension(media_type)
                
                # Create a unique filename
                prefix = f"{media_type.upper()}_" if media_type != 'image' else ""
                filename = f"{prefix}{item_id}{extension}"
                file_path = os.path.join(save_directory, filename)
                
                # Save the media file
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                
                # If we successfully downloaded, remove from error tracking if it was there
                if url in download_failure_errors:
                    download_failure_errors.remove(url)
                
                return file_path
                
            except requests.exceptions.HTTPError as e:
                if "429" in str(e) and retry_count < max_retries:
                    # Already handled above, but catch any we missed
                    retry_count += 1
                    backoff_time = min(60, (2 ** retry_count) + random.uniform(0, 1))
                    logging.warning(f"Rate limited (429) for {url}. Retrying in {backoff_time:.2f} seconds (attempt {retry_count}/{max_retries})")
                    time.sleep(backoff_time)
                elif (service and content_id and 
                      (response.status_code == 404 or response.status_code == 403) and 
                      retry_count < max_retries):
                    # If we get a 404 or 403 and we know the service and content ID,
                    # try the next fallback URL
                    retry_count += 1
                    # Import here to avoid circular imports
                    from .http_utils import generate_fallback_urls
                    fallback_urls = generate_fallback_urls(service, content_id)
                    if retry_count <= len(fallback_urls):
                        next_url = fallback_urls[retry_count - 1]
                        logging.info(f"Original URL failed with {response.status_code}, trying fallback URL: {next_url}")
                        url = next_url
                        continue
                    else:
                        raise
                else:
                    # Check specifically for 429 errors in the exception
                    if "429" in str(e):
                        logging.warning(f"HTTP 429 error caught for {url}: {e}")
                        
                        # For Imgur URLs with 429 errors, add to the persistent retry queue
                        if 'imgur.com' in domain:
                            logging.warning(f"Imgur rate limit hit in exception handler for URL: {url}")
                            logging.warning(f"Adding to retry queue with params: save_dir={save_directory}, item_id={item_id}, media_type={media_type}, service={service}, content_id={content_id}")
                            add_to_imgur_retry_queue(url, save_directory, item_id, media_type, service, content_id)
                            return None
                    raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                # Check if this is a DNS resolution error
                if is_dns_error(e):
                    logging.warning(f"DNS resolution error for {url}: {e}")
                    
                    # For service-specific URLs with DNS errors, try the alternative download method
                    if service and content_id:
                        logging.warning(f"{service} DNS resolution error for {url}. Trying alternative download method.")
                        # Import here to avoid circular imports
                        from .service_handlers.service_utils import retry_download_with_fallbacks
                        alternative_result = retry_download_with_fallbacks(service, content_id, save_directory, item_id, media_type)
                        if alternative_result:
                            return alternative_result
                
                # For connection errors, retry with backoff
                retry_count += 1
                if retry_count <= max_retries:
                    backoff_time = min(60, (2 ** retry_count) + random.uniform(0, 1))
                    logging.warning(f"Connection error for {url}. Retrying in {backoff_time:.2f} seconds (attempt {retry_count}/{max_retries})")
                    time.sleep(backoff_time)
                else:
                    # If we've exhausted retries and have service/content_id, try service-specific recovery
                    if service and content_id:
                        # Import here to avoid circular imports
                        from .service_handlers.service_utils import retry_download_with_fallbacks
                        return retry_download_with_fallbacks(service, content_id, save_directory, item_id, media_type)
                    raise
    
    except Exception as e:
        # Increment failure count for this domain
        if domain and domain in domain_failures:
            domain_failures[domain] += 1
            
        # Only log the error once per URL
        if url not in download_failure_errors:
            download_failure_errors.add(url)
            logging.error(f"Failed to download {media_type} from {url}: {e}")
            
            # For Imgur URLs, add to the persistent retry queue
            if ('imgur.com' in domain or 'i.imgur.com' in domain) and '429' in str(e) and media_type == 'image':
                logging.warning(f"Caught 429 error in general exception handler for Imgur URL: {url}")
                logging.warning(f"Exception details: {str(e)}")
                logging.warning(f"Adding to retry queue with params: save_dir={save_directory}, item_id={item_id}, media_type={media_type}, service={service}, content_id={content_id}")
                add_to_imgur_retry_queue(url, save_directory, item_id, media_type, service, content_id)
        
        return None

def download_image(image_url, save_directory, item_id):
    """Download an image from the given URL and save it locally."""
    return download_media(image_url, save_directory, item_id, 'image')

def download_video(url, save_directory, item_id, submission=None):
    """Download a video from the given URL and save it locally."""
    return download_media(url, save_directory, item_id, 'video')

def download_audio(url, save_directory, item_id):
    """Download audio from the given URL and save it locally."""
    return download_media(url, save_directory, item_id, 'audio')

def generate_thumbnail(image_path, save_directory, item_id, max_size=None):
    """Generate a thumbnail for an image."""
    from PIL import Image
    import io
    from .media_core import thumbnail_size
    
    if not max_size:
        max_size = thumbnail_size
    
    try:
        # Check if the image exists and is accessible
        if not os.path.exists(image_path):
            logging.error(f"Cannot generate thumbnail: Image file not found: {image_path}")
            return None
            
        # Check if the image is too large to process
        file_size = os.path.getsize(image_path)
        if file_size > max_image_size:
            logging.info(f"Image {image_path} is large ({file_size / 1024 / 1024:.2f} MB), generating thumbnail")
            
            # Open the image
            with open(image_path, 'rb') as f:
                img = Image.open(io.BytesIO(f.read()))
                
                # Convert to RGB if needed (e.g., for PNGs with transparency)
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3] if img.mode == 'RGBA' else None)
                    img = background
                
                # Calculate new dimensions while maintaining aspect ratio
                width, height = img.size
                if width > height:
                    new_width = min(width, max_size)
                    new_height = int(height * (new_width / width))
                else:
                    new_height = min(height, max_size)
                    new_width = int(width * (new_height / height))
                
                # Resize the image
                img = img.resize((new_width, new_height), Image.LANCZOS)
                
                # Save the thumbnail
                thumbnail_filename = f"THUMBNAIL_{item_id}.jpg"
                thumbnail_path = os.path.join(save_directory, thumbnail_filename)
                
                img.save(thumbnail_path, 'JPEG', quality=85)
                logging.info(f"Generated thumbnail for {image_path} at {thumbnail_path}")
                
                return thumbnail_path
        else:
            logging.debug(f"Image {image_path} is not large enough to require a thumbnail")
            return None
    except Exception as e:
        logging.error(f"Failed to generate thumbnail for {image_path}: {e}")
        return None

def detect_and_download_media(submission, save_directory):
    """
    Detect and download media from a Reddit submission.
    
    Args:
        submission: The Reddit submission object
        save_directory: Directory to save the media in
        
    Returns:
        A tuple of (media_type, media_path, thumbnail_path)
    """
    url = submission.url
    media_type = None
    media_path = None
    thumbnail_path = None
    
    try:
        # Check if it's a direct image URL
        if url.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg')):
            image_path = download_media(url, save_directory, submission.id, 'image')
            thumbnail_path = None
            if image_path and os.path.exists(image_path):
                # Generate thumbnail for large images
                file_size = os.path.getsize(image_path)
                if file_size > max_image_size:
                    thumbnail_path = generate_thumbnail(image_path, save_directory, submission.id)
            return 'image', image_path, thumbnail_path
        
        # Check if it's a direct video URL
        elif url.endswith(('.mp4', '.webm', '.mov')):
            video_path = download_media(url, save_directory, submission.id, 'video')
            return 'video', video_path, None
        
        # Check if it's a direct audio URL
        elif url.endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac')):
            audio_path = download_media(url, save_directory, submission.id, 'audio')
            return 'audio', audio_path, None
        
        # Check for v.redd.it videos
        elif 'v.redd.it' in url:
            # Import here to avoid circular imports
            from .service_handlers.reddit import extract_reddit_video_url
            video_url = extract_reddit_video_url(submission)
            if video_url:
                video_path = download_media(video_url, save_directory, submission.id, 'video')
                return 'video', video_path, None
        
        # Check for Gfycat
        elif 'gfycat.com' in url:
            # Import here to avoid circular imports
            from .service_handlers.gfycat import extract_gfycat_id, extract_gfycat_url, retry_gfycat_download
            
            gfycat_id = extract_gfycat_id(url)
            if gfycat_id:
                # Try to get the direct URL first
                video_url = extract_gfycat_url(gfycat_id)
                if video_url:
                    video_path = download_media(video_url, save_directory, submission.id, 'video')
                    if video_path:
                        return 'video', video_path, None
                
                # If direct URL fails or returns None, try alternative methods
                result = retry_gfycat_download(gfycat_id, save_directory, submission.id, 'video')
                if result:
                    return 'video', result, None
        
        # Check for Gyazo
        elif 'gyazo.com' in url:
            # Import here to avoid circular imports
            from .service_handlers.gyazo import extract_gyazo_id, extract_gyazo_image_url, retry_gyazo_download
            
            gyazo_id = extract_gyazo_id(url)
            if gyazo_id:
                # Try to get the direct URL first
                direct_url = extract_gyazo_image_url(gyazo_id)
                if direct_url:
                    if direct_url.endswith(('.mp4', '.webm', '.mov')):
                        video_path = download_media(direct_url, save_directory, submission.id, 'video')
                        return 'video', video_path, None
                    else:
                        image_path = download_media(direct_url, save_directory, submission.id, 'image')
                        thumbnail_path = None
                        if image_path and os.path.exists(image_path):
                            # Generate thumbnail for large images
                            file_size = os.path.getsize(image_path)
                            if file_size > max_image_size:
                                thumbnail_path = generate_thumbnail(image_path, save_directory, submission.id)
                        return 'image', image_path, thumbnail_path
                
                # If direct URL fails, try alternative methods
                result = retry_gyazo_download(gyazo_id, save_directory, submission.id, 'image')
                if result:
                    return 'image', result, None
        
        # Check for Imgur
        elif 'imgur.com' in url:
            # Import here to avoid circular imports
            from .service_handlers.imgur import (
                extract_imgur_album_id, extract_imgur_id, extract_imgur_image_url,
                download_imgur_album, retry_imgur_download, recover_imgur_content
            )
            
            # Check if it's an album
            album_id = extract_imgur_album_id(url)
            if album_id:
                album_path = download_imgur_album(url, save_directory, submission.id)
                return 'album', album_path, None
            
            # Check if it's a single image
            image_id = extract_imgur_id(url)
            if image_id:
                # Try to get the direct URL using the API
                image_url = extract_imgur_image_url(image_id)
                if image_url:
                    if image_url.endswith(('.mp4', '.webm', '.gifv')):
                        video_path = download_media(image_url, save_directory, submission.id, 'video')
                        return 'video', video_path, None
                    else:
                        image_path = download_media(image_url, save_directory, submission.id, 'image')
                        thumbnail_path = None
                        if image_path and os.path.exists(image_path):
                            # Generate thumbnail for large images
                            file_size = os.path.getsize(image_path)
                            if file_size > max_image_size:
                                thumbnail_path = generate_thumbnail(image_path, save_directory, submission.id)
                        return 'image', image_path, thumbnail_path
                
                # If API fails, try alternative methods
                result = retry_imgur_download(image_id, save_directory, submission.id, 'image')
                if result:
                    return 'image', result, None
        
        # Check for Streamable
        elif 'streamable.com' in url:
            # Import here to avoid circular imports
            from .service_handlers.streamable import extract_streamable_video_url
            
            video_url = extract_streamable_video_url(url)
            if video_url:
                video_path = download_media(video_url, save_directory, submission.id, 'video')
                return 'video', video_path, None
        
        # If we couldn't identify the media type, try to recover it
        # Import here to avoid circular imports
        from .service_handlers.recovery import recover_deleted_media
        
        media_path = recover_deleted_media(url, submission.id, save_directory, submission)
        if media_path:
            # Try to determine the media type from the file extension
            if media_path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg')):
                return 'image', media_path, None
            elif media_path.endswith(('.mp4', '.webm', '.mov')):
                return 'video', media_path, None
            elif media_path.endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac')):
                return 'audio', media_path, None
            else:
                return 'unknown', media_path, None
    
    except Exception as e:
        logging.error(f"Error detecting and downloading media: {e}")
    
    return None, None, None 