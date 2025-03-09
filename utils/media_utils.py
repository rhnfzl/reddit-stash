"""
Legacy media utilities module that re-exports functionality from the new modular structure.
This module is maintained for backward compatibility with existing code.
New code should use the utils.media module directly.

IMPORTANT REFERENCE NOTES:
--------------------------
1. This file is a compatibility layer that re-exports functionality from the new modular structure.
2. The original implementation has been preserved in 'media_utils.py.bak' for reference.
3. The functionality has been refactored into separate modules under utils/media/:
   - media_core.py: Core utilities and configuration
   - download.py: Media download functionality
   - retry_queue.py: Imgur retry queue functionality
   - http_utils.py: HTTP-related utilities
   - rate_limiting.py: Rate limiting functionality
   - service_handlers/: Service-specific handlers (Imgur, Gfycat, etc.)
4. This compatibility layer will be removed in a future version once all code has been
   updated to use the new structure directly.

For a detailed mapping of old functions to new modules, see the imports below.
"""

import os
import logging
import warnings

# Import from the new modular structure
from utils.media import (
    # Core utilities
    get_valid_extensions,
    get_default_extension,
    clean_url,
    get_domain_from_url,
    is_dns_error,
    save_directory,
    BASE_DIR,
    
    # Download functions
    download_media,
    download_image,
    download_video,
    download_audio,
    generate_thumbnail,
    
    # Retry queue
    process_imgur_retry_queue,
    update_retry_queue_save_directory,
    
    # Service handlers
    identify_service,
    extract_content_id,
    is_album_or_gallery,
    download_album_or_gallery,
    get_service_handler,
    retry_download_with_fallbacks,
    
    # HTTP utilities
    get_service_headers,
    generate_fallback_urls,
    get_random_user_agent,
    
    # Rate limiting
    ApiKeyRotator,
    apply_rate_limiting
)

# Import service-specific functions from the new structure
from utils.media.service_handlers import (
    # Imgur
    extract_imgur_id,
    extract_imgur_album_id,
    extract_imgur_image_url,
    extract_imgur_video_url,
    extract_imgur_album_images,
    download_imgur_album,
    retry_imgur_download,
    recover_imgur_content,
    
    # Gfycat
    extract_gfycat_id,
    extract_gfycat_url,
    retry_gfycat_download,
    recover_gfycat_content,
    
    # Gyazo
    extract_gyazo_id,
    extract_gyazo_image_url,
    is_gyazo_gif,
    retry_gyazo_download,
    recover_gyazo_content,
    
    # Reddit
    extract_reddit_post_id,
    extract_reddit_media_id,
    extract_reddit_image_url,
    extract_reddit_video_url,
    extract_reddit_gallery_urls,
    download_reddit_gallery,
    is_reddit_gallery,
    retry_reddit_download,
    recover_reddit_content
)

# Log a deprecation warning
logging.warning(
    "utils.media_utils is deprecated and will be removed in a future version. "
    "Please use utils.media instead."
)

# Suppress the deprecation warning after the first import
warnings.filterwarnings('ignore', message='utils.media_utils is deprecated')

# For backward compatibility, re-export everything
__all__ = [
    # Core utilities
    'get_valid_extensions',
    'get_default_extension',
    'clean_url',
    'get_domain_from_url',
    'is_dns_error',
    'save_directory',
    'BASE_DIR',
    
    # Download functions
    'download_media',
    'download_image',
    'download_video',
    'download_audio',
    'generate_thumbnail',
    
    # Retry queue
    'process_imgur_retry_queue',
    'update_retry_queue_save_directory',
    
    # Service handlers
    'identify_service',
    'extract_content_id',
    'is_album_or_gallery',
    'download_album_or_gallery',
    'get_service_handler',
    'retry_download_with_fallbacks',
    
    # HTTP utilities
    'get_service_headers',
    'generate_fallback_urls',
    'get_random_user_agent',
    
    # Rate limiting
    'ApiKeyRotator',
    'apply_rate_limiting',
    
    # Imgur
    'extract_imgur_id',
    'extract_imgur_album_id',
    'extract_imgur_image_url',
    'extract_imgur_video_url',
    'extract_imgur_album_images',
    'download_imgur_album',
    'retry_imgur_download',
    'recover_imgur_content',
    
    # Gfycat
    'extract_gfycat_id',
    'extract_gfycat_url',
    'retry_gfycat_download',
    'recover_gfycat_content',
    
    # Gyazo
    'extract_gyazo_id',
    'extract_gyazo_image_url',
    'is_gyazo_gif',
    'retry_gyazo_download',
    'recover_gyazo_content',
    
    # Reddit
    'extract_reddit_post_id',
    'extract_reddit_media_id',
    'extract_reddit_image_url',
    'extract_reddit_video_url',
    'extract_reddit_gallery_urls',
    'download_reddit_gallery',
    'is_reddit_gallery',
    'retry_reddit_download',
    'recover_reddit_content'
]