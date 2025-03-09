import random
import logging
from .media_core import ignore_ssl_errors

# Service-specific referrers and origins
SERVICE_REFERRERS = {
    'imgur': {'referer': 'https://imgur.com/', 'origin': 'https://imgur.com'},
    'gfycat': {'referer': 'https://gfycat.com/', 'origin': 'https://gfycat.com'},
    'gyazo': {'referer': 'https://gyazo.com/', 'origin': 'https://gyazo.com'},
    'reddit': {'referer': 'https://www.reddit.com/', 'origin': 'https://www.reddit.com'},
    'default': {'referer': None, 'origin': None}
}

# Service-specific accept headers
SERVICE_ACCEPT_HEADERS = {
    'imgur': 'image/webp,image/apng,image/*,*/*;q=0.8',
    'gfycat': 'video/webm,video/mp4,video/*,*/*;q=0.8',
    'gyazo': 'image/webp,image/apng,image/*,*/*;q=0.8',
    'reddit': '*/*',
    'default': '*/*'
}

def generate_random_user_agent():
    """Generate a random user agent string."""
    chrome_version = random.randint(80, 110)
    build = random.randint(1000, 9999)
    sub_build = random.randint(100, 999)
    
    platforms = [
        f'Windows NT 10.0; Win64; x64',
        f'Macintosh; Intel Mac OS X 10_{random.randint(12, 15)}_{random.randint(1, 7)}',
        f'X11; Linux x86_64',
        f'Windows NT 6.1; Win64; x64',
    ]
    
    platform = random.choice(platforms)
    
    user_agents = [
        f'Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.{build}.{sub_build} Safari/537.36',
        f'Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.{build}.{sub_build} Safari/537.36 Edg/{chrome_version}.0.{build}.{sub_build}',
        f'Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.{build}.{sub_build} Safari/537.36 OPR/{chrome_version}.0.{build}.{sub_build}',
    ]
    
    return random.choice(user_agents)

# Alias for backward compatibility
get_random_user_agent = generate_random_user_agent

def get_service_headers(service='default', use_random_ua=False):
    """
    Get appropriate headers for a specific service.
    
    Args:
        service: The service name ('imgur', 'gfycat', 'gyazo', 'reddit', 'default')
        use_random_ua: Whether to use a random user agent
        
    Returns:
        A dictionary of headers
    """
    # Default headers
    headers = {
        'User-Agent': generate_random_user_agent() if use_random_ua else 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': SERVICE_ACCEPT_HEADERS.get(service, SERVICE_ACCEPT_HEADERS['default']),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Pragma': 'no-cache'
    }
    
    # Add service-specific headers
    if service in SERVICE_REFERRERS:
        referer = SERVICE_REFERRERS[service]['referer']
        origin = SERVICE_REFERRERS[service]['origin']
        
        if referer:
            headers['Referer'] = referer
        if origin:
            headers['Origin'] = origin
    
    # Add special headers for specific services
    if service == 'imgur':
        headers['X-Requested-With'] = 'XMLHttpRequest'
    elif service == 'reddit':
        headers['X-Reddit-User-Agent'] = 'web:reddit-stash:v1.0 (by /u/reddit-stash)'
    
    return headers

def generate_fallback_urls(service, content_id):
    """
    Generate a list of fallback URLs for a given service and content ID.
    
    Args:
        service: The service name (e.g., 'imgur', 'gfycat')
        content_id: The content ID to use in the URL patterns
        
    Returns:
        A list of fallback URLs to try
    """
    # Each service has a list of patterns where {id} will be replaced with the content ID
    FALLBACK_URL_PATTERNS = {
        'imgur': [
            'https://i.imgur.com/{id}.jpg',
            'https://i.imgur.com/{id}.png',
            'https://i.imgur.com/{id}.gif',
            'https://i.imgur.com/{id}.mp4',
            'https://imgur.com/download/{id}',
        ],
        'gfycat': [
            # Primary domains
            'https://giant.gfycat.com/{id}.mp4',
            'https://thumbs.gfycat.com/{id}-mobile.mp4',
            'https://thumbs.gfycat.com/{id}.mp4',
            # Alternative domains (some may be deprecated but worth trying)
            'https://zippy.gfycat.com/{id}.mp4',
            'https://fat.gfycat.com/{id}.mp4',
            # GIF versions
            'https://thumbs.gfycat.com/{id}-size_restricted.gif',
            'https://giant.gfycat.com/{id}.gif',
            # Alternative domain structure
            'https://gfycat.com/{id}/mp4',
            # Direct CDN URLs
            'https://assets.gfycat.com/gifs/{id}.mp4',
            'https://media.gfycat.com/{id}.mp4',
            # Legacy formats
            'https://gfycat.com/ifr/{id}',
        ],
        'gyazo': [
            'https://i.gyazo.com/{id}.png',
            'https://i.gyazo.com/{id}.jpg',
            'https://i.gyazo.com/{id}.gif',
        ],
        'reddit': [
            'https://i.redd.it/{id}.jpg',
            'https://i.redd.it/{id}.png',
            'https://preview.redd.it/{id}.jpg',
            'https://preview.redd.it/{id}.png',
        ],
    }
    
    if service not in FALLBACK_URL_PATTERNS:
        return []
        
    return [pattern.format(id=content_id) for pattern in FALLBACK_URL_PATTERNS[service]] 