"""
Centralized constants for Reddit Stash configuration.

This module defines all configurable constants used throughout the application,
replacing magic numbers to improve maintainability and allow easier tuning.
Addresses PR feedback regarding hardcoded values.
"""

# Download Configuration
DOWNLOAD_CHUNK_SIZE = 65536  # 64KB chunks for streaming downloads
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_CONNECT_TIMEOUT = 5.0
FFMPEG_TIMEOUT_SECONDS = 300  # 5 minutes for video merging
DEFAULT_MAX_FILE_SIZE = 52428800  # 50MB default limit

# Disk Space Management
DISK_SPACE_SAFETY_FACTOR = 1.2  # 20% extra space required beyond file size
MIN_FREE_SPACE_MB = 100  # Minimum free space to maintain (in MB)

# Rate Limiting Configuration
IMGUR_REQUESTS_PER_MINUTE = 4  # Conservative rate for IP limits
REDDIT_REQUESTS_PER_MINUTE = 100  # Aligned with API limits
GENERIC_REQUESTS_PER_MINUTE = 30  # Conservative for unknown sites

# Content Recovery Rate Limits
WAYBACK_REQUESTS_PER_MINUTE = 60
PULLPUSH_REQUESTS_PER_MINUTE = 12  # Under 15 soft limit
REDDIT_PREVIEWS_REQUESTS_PER_MINUTE = 30
REVEDDIT_REQUESTS_PER_MINUTE = 20

# Retry Configuration
DEFAULT_MAX_RETRIES = 3
EXPONENTIAL_BACKOFF_MULTIPLIER = 1
EXPONENTIAL_BACKOFF_MIN_SECONDS = 1
EXPONENTIAL_BACKOFF_MAX_SECONDS = 10

# Rate Limiter Timeouts
RATE_LIMIT_TIMEOUT_SECONDS = 90  # Increased for Imgur backoff periods
DEFAULT_RATE_LIMIT_TIMEOUT = 15

# SQLite Configuration
SQLITE_CACHE_SIZE_KB = 64000  # 64MB cache for better performance
SQLITE_TIMEOUT_SECONDS = 10.0
RECOVERY_CACHE_TTL_HOURS = 24  # Cache recovery results for 24 hours

# Connection Pooling
HTTP_POOL_CONNECTIONS = 10
HTTP_POOL_MAXSIZE = 20
RETRY_TOTAL_ATTEMPTS = 3
RETRY_BACKOFF_FACTOR = 0.3

# File Processing
MIN_MEDIA_FILE_SIZE = 100  # Minimum size for media files (avoid empty/truncated)
IMAGE_VALIDATION_ENABLED = True
HASH_ALGORITHM_PREFERENCE = "BLAKE3"  # "BLAKE3" or "SHA256"

# Jitter Configuration
REDDIT_API_JITTER_PERCENT = 10  # ±10% for Reddit API calls
HTTP_SERVICE_JITTER_PERCENT = 25  # ±25% for HTTP services

# Content Type Validation
EXPECTED_IMAGE_CONTENT_TYPES = {
    'image/jpeg', 'image/jpg', 'image/png', 'image/gif',
    'image/webp', 'image/bmp', 'image/tiff'
}
EXPECTED_VIDEO_CONTENT_TYPES = {
    'video/mp4', 'video/webm', 'video/quicktime', 'video/avi'
}
EXPECTED_AUDIO_CONTENT_TYPES = {
    'audio/mpeg', 'audio/mp4', 'audio/wav', 'audio/ogg'
}

# Error Handling
MAX_RETRY_QUEUE_SIZE = 10000  # Maximum items in persistent retry queue
SESSION_BLACKLIST_SIZE = 1000  # Maximum URLs to blacklist per session

# Logging
DEFAULT_LOG_LEVEL = "INFO"
DEBUG_CHUNK_LOGGING = False  # Log individual chunk downloads (very verbose)

# File Extensions
DEFAULT_IMAGE_EXTENSION = '.jpg'
DEFAULT_VIDEO_EXTENSION = '.mp4'
DEFAULT_AUDIO_EXTENSION = '.mp3'

VALID_IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff'
}
VALID_VIDEO_EXTENSIONS = {
    '.mp4', '.webm', '.mov', '.avi', '.mkv'
}
VALID_AUDIO_EXTENSIONS = {
    '.mp3', '.m4a', '.wav', '.ogg', '.flac'
}

# User Agent
DEFAULT_USER_AGENT = "Reddit Stash Media Downloader/1.0"
CHROME_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# URL Processing
MAX_URL_LENGTH = 2048
MIN_URL_LENGTH = 10
URL_VALIDATION_TIMEOUT = 5.0  # Timeout for URL validation checks

# Processing Pipeline
DYNAMIC_SLEEP_BASE_SECONDS = 0.005  # Courtesy delay between items (PRAW handles API rate limiting)
DYNAMIC_SLEEP_MAX_SECONDS = 0.5  # Cap on adaptive sleep time
FILE_LOG_CHECKPOINT_INTERVAL = 200  # Save file_log.json every N items (trade-off: crash loss vs I/O)

# Trusted Media Domains (skip URL validation for these)
TRUSTED_MEDIA_DOMAINS = frozenset({
    'i.redd.it', 'v.redd.it', 'preview.redd.it',
    'external-preview.redd.it', 'i.imgur.com',
})

# Development and Testing
ENABLE_PERFORMANCE_METRICS = False
ENABLE_DETAILED_LOGGING = False
TEST_MODE = False  # Set to True for test environments