import time
import random
import logging
from .media_core import get_domain_from_url
from utils.time_utilities import dynamic_sleep

# Track request failures for specific domains
domain_failures = {}

# Track the last time we made a request to specific domains
last_request_time = {
    'imgur.com': 0,
    'i.imgur.com': 0,
    'gfycat.com': 0,
    'redd.it': 0,
    'reddit.com': 0,
    'imgur_api': 0
}

# Minimum time between requests to the same domain (in seconds)
MIN_REQUEST_INTERVAL = {
    'imgur.com': 15,  # 15 seconds between Imgur requests
    'i.imgur.com': 15,
    'gfycat.com': 10,
    'redd.it': 5,
    'reddit.com': 5
}

class ApiKeyRotator:
    """Rotate through multiple API keys to avoid rate limits."""
    
    def __init__(self, credentials):
        """
        Initialize with a list of credential tuples.
        
        Args:
            credentials: List of (client_id, client_secret) tuples
        """
        self.credentials = credentials if credentials else []
        self.current_index = 0
        self.usage_counts = {i: 0 for i in range(len(credentials))}
        self.last_used = {i: 0 for i in range(len(credentials))}  # Timestamp of last use
        
    def get_next_key(self):
        """Get the next API key in the rotation."""
        if not self.credentials:
            return None, None
            
        idx = self.current_index
        creds = self.credentials[idx]
        self.usage_counts[idx] += 1
        self.last_used[idx] = time.time()
        self.current_index = (self.current_index + 1) % len(self.credentials)
        return creds
        
    def get_least_used_key(self):
        """Get the least recently used API key."""
        if not self.credentials:
            return None, None
            
        # Sort indices by last used timestamp
        sorted_indices = sorted(range(len(self.credentials)), key=lambda i: self.last_used[i])
        idx = sorted_indices[0]
        creds = self.credentials[idx]
        self.usage_counts[idx] += 1
        self.last_used[idx] = time.time()
        return creds
        
    def get_random_key(self):
        """Get a random API key."""
        if not self.credentials:
            return None, None
            
        idx = random.randrange(len(self.credentials))
        creds = self.credentials[idx]
        self.usage_counts[idx] += 1
        self.last_used[idx] = time.time()
        return creds
        
    def has_keys(self):
        """Check if there are any API keys available."""
        return len(self.credentials) > 0

def apply_rate_limiting(url):
    """Apply rate limiting based on domain to avoid hitting API limits."""
    domain = get_domain_from_url(url)
    if not domain:
        return
        
    # Initialize failure count if not present
    if domain not in domain_failures:
        domain_failures[domain] = 0
    
    # Check if we need to enforce a minimum time between requests
    current_time = time.time()
    base_domain = domain
    
    # Extract base domain for checking against our tracking dictionaries
    for tracked_domain in last_request_time.keys():
        if tracked_domain in domain:
            base_domain = tracked_domain
            break
    
    # If we've made a request to this domain recently, wait until the minimum interval has passed
    if base_domain in last_request_time:
        time_since_last_request = current_time - last_request_time[base_domain]
        min_interval = MIN_REQUEST_INTERVAL.get(base_domain, 0)
        
        if time_since_last_request < min_interval:
            wait_time = min_interval - time_since_last_request
            logging.info(f"Enforcing minimum interval for {base_domain}: waiting {wait_time:.2f}s")
            time.sleep(wait_time)
            
    # Apply dynamic sleep based on domain and failure count
    content_length = len(url)  # Use URL length as a proxy for content size
    sleep_time = dynamic_sleep(content_length, domain_failures[domain])
        
    # Add extra delay for known rate-limited domains
    if 'imgur.com' in domain or 'i.imgur.com' in domain:
        # More aggressive rate limiting for Imgur
        # Base delay of 10-15 seconds plus additional delay based on failure count
        base_delay = random.uniform(10.0, 15.0)
        failure_multiplier = min(10, domain_failures.get(domain, 0))  # Cap at 10x
        sleep_time = max(sleep_time, base_delay * (1 + failure_multiplier * 0.5))
        logging.debug(f"Applied Imgur rate limiting: {sleep_time:.2f}s delay for {domain}")
    elif 'redd.it' in domain or 'reddit.com' in domain:
        # More aggressive rate limiting for Reddit
        base_delay = random.uniform(3.0, 5.0)
        failure_multiplier = min(5, domain_failures.get(domain, 0))
        sleep_time = max(sleep_time, base_delay * (1 + failure_multiplier * 0.5))
    elif 'gfycat.com' in domain:
        sleep_time = max(sleep_time, 5.0)  # At least 5 seconds for Gfycat
        
    # Add some randomness to avoid detection patterns
    sleep_time *= random.uniform(0.9, 1.1)
    
    logging.debug(f"Rate limiting for {domain}: sleeping for {sleep_time:.2f}s")
    time.sleep(sleep_time)
    
    # Update the last request time for this domain
    if base_domain in last_request_time:
        last_request_time[base_domain] = time.time()
        
    return domain 