"""
Centralized constants for Reddit Stash configuration.

Numeric limits, timeouts and sizes shared across the application.
"""

# Download Configuration
BYTES_PER_MB = 1024 * 1024

LOG_FILE_NAME = "file_log.json"  # the dedup log, in the save directory and at the storage root
DOWNLOAD_CHUNK_SIZE = 65536  # 64KB chunks for streaming downloads
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_CONNECT_TIMEOUT = 5.0
FFMPEG_TIMEOUT_SECONDS = 300  # 5 minutes for video merging
FFMPEG_PROBE_TIMEOUT_SECONDS = 10  # `ffmpeg -version` availability check
DEFAULT_MAX_FILE_SIZE = 52428800  # 50MB default limit

# Disk Space Management
DISK_SPACE_SAFETY_FACTOR = 1.2  # 20% extra space required beyond file size
MIN_FREE_SPACE_MB = 100  # Minimum free space to maintain (in MB)

# Retry Configuration
SECONDS_PER_DAY = 86400
DEFAULT_MAX_RETRIES = 3
EXPONENTIAL_BACKOFF_MULTIPLIER = 1
EXPONENTIAL_BACKOFF_MIN_SECONDS = 1
EXPONENTIAL_BACKOFF_MAX_SECONDS = 10

# Rate Limiter Timeouts
RATE_LIMIT_TIMEOUT_SECONDS = 90  # Increased for Imgur backoff periods
DEFAULT_RATE_LIMIT_TIMEOUT = 15

# SQLite Configuration
SQLITE_CACHE_SIZE_KB = 64000  # 64MB cache for better performance
SQLITE_MMAP_SIZE_BYTES = 134217728  # 128MB
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

# URLs ending in one of these are treated as images.
IMAGE_URL_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff')
# A comment body ending in one of these has its image downloaded. Narrower than the
# above: widening it would rewrite already-saved comments.
COMMENT_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif')
# Extensions a URL keeps in its saved file name; any other is saved as .jpg. Saved names
# are part of the backup layout, so changing this renames future downloads.
SAVED_MEDIA_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm')
# Extensions a save path keeps however the server labels the content.
TRUSTED_FILE_EXTENSIONS = SAVED_MEDIA_EXTENSIONS + ('.mov', '.mp3', '.m4a', '.wav')

# User Agent
DEFAULT_USER_AGENT = "Reddit Stash Media Downloader/1.0"
CHROME_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# URL Processing
MAX_URL_LENGTH = 2048
MIN_URL_LENGTH = 10
URL_VALIDATION_TIMEOUT = 5.0  # Timeout for URL validation checks
DNS_CACHE_SECONDS = 60  # How long a successful DNS answer is reused by URL validation

# Processing Pipeline
DYNAMIC_SLEEP_BASE_SECONDS = 0.005  # Courtesy delay between items (PRAW handles API rate limiting)
DYNAMIC_SLEEP_MAX_SECONDS = 0.5  # Cap on adaptive sleep time
DYNAMIC_SLEEP_CHARS_PER_STEP = 10000  # Sleep a little longer per this many characters of content
MAX_ITEM_FAILURES = 5  # Stop retrying a post/comment after this many failed runs
COMMENT_FETCH_MAX_ATTEMPTS = 12  # With capped backoff, about 10 minutes: outlasts Reddit's rate limit window
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