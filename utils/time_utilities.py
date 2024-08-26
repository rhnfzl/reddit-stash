import time
import random
import logging
import prawcore

def exponential_backoff(attempt: int) -> None:
    """Implement exponential backoff with jitter."""
    wait_time = min(120, (2 ** attempt) + random.uniform(0, 1))
    logging.info(f"Retrying in {wait_time:.2f} seconds...")
    time.sleep(wait_time)

def dynamic_sleep(content_length):
    """Dynamically adjust sleep time based on content length."""
    base_sleep_time = 1
    sleep_time = base_sleep_time

    if content_length > 10000:
        sleep_time *= 2
    elif content_length > 5000:
        sleep_time *= 1.5

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