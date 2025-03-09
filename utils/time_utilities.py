import time
import math
import random
import logging
import prawcore

def exponential_backoff(attempt, base_delay=1.0, max_delay=60.0, jitter=0.2):
    """
    Perform exponential backoff with jitter.
    
    Args:
        attempt: The current attempt number (0-based)
        base_delay: The base delay in seconds
        max_delay: The maximum delay in seconds
        jitter: The jitter factor (0.0 to 1.0)
        
    Returns:
        None (sleeps for the calculated time)
    """
    # Calculate exponential backoff
    delay = min(max_delay, base_delay * (2 ** attempt))
    
    # Add jitter to avoid thundering herd problem
    jittered_delay = delay * (1 + random.uniform(-jitter, jitter))
    
    logging.debug(f"Backing off for {jittered_delay:.2f} seconds (attempt {attempt+1})")
    time.sleep(jittered_delay)

def dynamic_sleep(content_length, request_failures=0, max_sleep_time=5):
    """
    Dynamically adjust sleep time based on content length and other factors,
    with a more conservative approach to avoid slowing down the process too much.
    
    :param content_length: Length of the content being processed.
    :param request_failures: Number of failed requests in a row (optional).
    :param max_sleep_time: Maximum sleep time allowed (optional).
    :return: Sleep time in seconds.
    """
    base_sleep_time = 0.2  # Start with a lower base time

    # Use a very mild scaling factor
    sleep_time = base_sleep_time + 0.05 * (content_length // 10000)

    # Adjust sleep time based on the number of recent request failures, but with a lower multiplier
    if request_failures > 0:
        sleep_time *= (1.5 ** request_failures)

    # Apply a lower cap to the sleep time
    sleep_time = min(sleep_time, max_sleep_time)

    # Add a minimal jitter to avoid synchronization issues
    jitter = random.uniform(0.9, 1.1)
    sleep_time *= jitter

    # Logging the sleep time for monitoring and tuning
    logging.info(f"Sleeping for {sleep_time:.2f} seconds based on content length {content_length} and {request_failures} failures.")

    time.sleep(sleep_time)

    return sleep_time

def lazy_load_comments(submission):
    """Lazily load comments instead of replacing all at once."""
    attempt = 0
    while True:
        try:
            for comment in submission.comments.list():
                yield comment
            break
        except prawcore.exceptions.TooManyRequests:
            exponential_backoff(attempt)
            attempt += 1
            continue

def rate_limit(operations_per_minute=60):
    """
    Decorator to rate limit function calls.
    
    Args:
        operations_per_minute: Maximum number of operations per minute
        
    Returns:
        Decorated function
    """
    min_interval = 60.0 / operations_per_minute
    last_called = [0.0]  # Use a list to allow modification in nested scope
    
    def decorator(func):
        def wrapper(*args, **kwargs):
            current_time = time.time()
            elapsed = current_time - last_called[0]
            
            if elapsed < min_interval:
                sleep_time = min_interval - elapsed
                logging.debug(f"Rate limiting: sleeping for {sleep_time:.2f} seconds")
                time.sleep(sleep_time)
                
            result = func(*args, **kwargs)
            last_called[0] = time.time()
            return result
        return wrapper
    return decorator

def timeout(seconds, error_message="Function call timed out"):
    """
    Decorator to timeout function calls.
    
    Args:
        seconds: Maximum execution time in seconds
        error_message: Message to include in the timeout exception
        
    Returns:
        Decorated function
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            import signal
            
            def handler(signum, frame):
                raise TimeoutError(error_message)
                
            # Set the timeout handler
            signal.signal(signal.SIGALRM, handler)
            signal.alarm(int(seconds))
            
            try:
                result = func(*args, **kwargs)
            finally:
                # Cancel the timeout
                signal.alarm(0)
                
            return result
        return wrapper
    return decorator

def get_timestamp(format_string="%Y%m%d_%H%M%S"):
    """
    Get a formatted timestamp string.
    
    Args:
        format_string: The format string for the timestamp
        
    Returns:
        Formatted timestamp string
    """
    import datetime
    return datetime.datetime.now().strftime(format_string)

def parse_timestamp(timestamp_string, format_string="%Y%m%d_%H%M%S"):
    """
    Parse a timestamp string into a datetime object.
    
    Args:
        timestamp_string: The timestamp string to parse
        format_string: The format string for the timestamp
        
    Returns:
        datetime object
    """
    import datetime
    return datetime.datetime.strptime(timestamp_string, format_string)

def get_elapsed_time(start_time, end_time=None):
    """
    Get the elapsed time between two timestamps.
    
    Args:
        start_time: The start time (time.time() value)
        end_time: The end time (time.time() value), defaults to current time
        
    Returns:
        Elapsed time in seconds
    """
    if end_time is None:
        end_time = time.time()
    return end_time - start_time

def format_elapsed_time(elapsed_seconds):
    """
    Format elapsed time in a human-readable format.
    
    Args:
        elapsed_seconds: Elapsed time in seconds
        
    Returns:
        Formatted string (e.g., "2h 30m 45s")
    """
    hours, remainder = divmod(int(elapsed_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    
    return " ".join(parts)