import time
import random
import logging

from utils.constants import (
    DYNAMIC_SLEEP_BASE_SECONDS,
    DYNAMIC_SLEEP_CHARS_PER_STEP,
    DYNAMIC_SLEEP_MAX_SECONDS,
)

logger = logging.getLogger(__name__)

def exponential_backoff(attempt: int) -> None:
    """Implement exponential backoff with jitter."""
    wait_time = min(120, (2 ** attempt) + random.uniform(0, 1))
    logger.info(f"Retrying in {wait_time:.2f} seconds...")
    time.sleep(wait_time)

def dynamic_sleep(content_length: int, request_failures: int = 0,
                  max_sleep_time: float = DYNAMIC_SLEEP_MAX_SECONDS) -> float:
    """
    Dynamically adjust sleep time based on content length and other factors.
    PRAW handles Reddit API rate limiting internally, so this primarily
    prevents overwhelming local I/O and provides minimal courtesy delays.

    :param content_length: Length of the content being processed.
    :param request_failures: Number of failed requests in a row (optional).
    :param max_sleep_time: Maximum sleep time allowed (optional).
    :return: Sleep time in seconds.
    """
    base_sleep_time = DYNAMIC_SLEEP_BASE_SECONDS

    # Mild scaling factor
    sleep_time = base_sleep_time + 0.01 * (content_length // DYNAMIC_SLEEP_CHARS_PER_STEP)

    # Adjust sleep time based on the number of recent request failures, but with a lower multiplier
    if request_failures > 0:
        sleep_time *= (1.5 ** request_failures)

    # Apply a lower cap to the sleep time
    sleep_time = min(sleep_time, max_sleep_time)

    # Add a minimal jitter to avoid synchronization issues
    jitter = random.uniform(0.9, 1.1)
    sleep_time *= jitter

    # Debug, not info: this fires once per saved item
    logger.debug(f"Sleeping for {sleep_time:.2f} seconds based on content length {content_length} and {request_failures} failures.")

    time.sleep(sleep_time)

    return sleep_time
