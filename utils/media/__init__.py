"""
Modular media utilities package for Reddit Stash.

This package contains modular implementations of the media utilities that were
previously contained in a single file (utils/media_utils.py).

REFERENCE NOTES:
---------------
1. The original monolithic implementation is preserved in 'utils/media_utils.py.bak'
2. A compatibility layer is provided in 'utils/media_utils.py' that re-exports all
   functionality from this package for backward compatibility
3. New code should import directly from this package (utils.media) rather than
   from the compatibility layer

Module Structure:
- media_core.py: Core utilities and configuration
- download.py: Media download functionality
- retry_queue.py: Imgur retry queue functionality
- http_utils.py: HTTP-related utilities
- rate_limiting.py: Rate limiting functionality
- service_handlers/: Service-specific handlers (Imgur, Gfycat, etc.)
"""

from .media_core import (
    get_valid_extensions,
    get_default_extension,
    clean_url,
    get_domain_from_url,
    is_dns_error,
    save_directory,
    BASE_DIR
)

from .download import (
    download_media,
    download_image,
    download_video,
    download_audio,
    generate_thumbnail,
    detect_and_download_media
)

from .retry_queue import (
    process_imgur_retry_queue,
    update_retry_queue_save_directory
)

# Import service handlers
from .service_handlers import (
    identify_service,
    extract_content_id,
    is_album_or_gallery,
    download_album_or_gallery,
    get_service_handler,
    retry_download_with_fallbacks
)

# Import HTTP utilities
from .http_utils import (
    get_service_headers,
    generate_fallback_urls,
    get_random_user_agent
)

# Import rate limiting
from .rate_limiting import (
    ApiKeyRotator,
    apply_rate_limiting
)

# Re-export everything to maintain backward compatibility
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
    'detect_and_download_media',
    
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
    'apply_rate_limiting'
] 