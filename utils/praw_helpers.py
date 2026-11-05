"""PRAW read helpers: keep what a listing returned before Reddit refused the rest, and
retry a rate-limited comment load."""

import logging
from collections.abc import Iterable, Iterator

import prawcore
from praw.models import Comment, Submission

from utils.constants import COMMENT_FETCH_MAX_ATTEMPTS
from utils.time_utilities import exponential_backoff

logger = logging.getLogger(__name__)


def fetch_until_unavailable(listing: Iterable, label: str) -> tuple[list, bool]:
    """Read a PRAW listing until it ends or Reddit answers 404/403.

    A failed page ends the whole listing, because PRAW cannot resume past it, so
    items after that point are never seen.

    Returns:
        (items, stopped_early): the items read, and whether the listing ended early.
    """
    items = []
    try:
        for item in listing:
            items.append(item)
    except (prawcore.exceptions.NotFound, prawcore.exceptions.Forbidden) as error:
        logger.warning(f"{label} listing stopped early after {len(items)} items: {error}")
        return items, True
    logger.info(f"Fetched {len(items)} {label} items")
    return items, False


def lazy_load_comments(submission: Submission) -> Iterator[Comment]:
    """Yield a submission's top-level comments, fetching them on first use.

    Top level only: the markdown writer reaches the rest through each comment's replies.
    """
    attempt = 0
    while True:
        try:
            yield from submission.comments
            return
        except prawcore.exceptions.TooManyRequests:
            attempt += 1
            if attempt >= COMMENT_FETCH_MAX_ATTEMPTS:
                raise
            exponential_backoff(attempt - 1)
