"""
PRAW Helper Utilities for Reddit Stash.

This module provides safe iteration wrappers for PRAW generators with built-in
error handling and content recovery capabilities.

Key Features:
- Hybrid fetch strategy: batch first, fall back to one-by-one on errors
- Integration with content recovery services for deleted content
- Comprehensive error handling for prawcore exceptions
- Recovery metadata formatting for markdown files

Usage:
    from utils.praw_helpers import safe_fetch_items

    for comment in safe_fetch_items(user.comments.new(limit=1000), 'comment'):
        # Process comment safely
        pass
"""

import logging
import prawcore
from typing import Generator, Optional, Any, Dict
from praw.models import Comment, Submission

from .content_recovery import ContentRecoveryService

logger = logging.getLogger(__name__)


class RecoveredItem:
    """
    Placeholder for recovered content that couldn't be fetched via PRAW.

    This class mimics basic PRAW object structure so recovered items
    can be processed similarly to live items.
    """

    def __init__(self, item_type: str, item_id: str, recovery_result, original_url: str):
        self.item_type = item_type
        self.id = item_id
        self.recovery_result = recovery_result
        self.original_url = original_url
        self.is_recovered = True

        # Extract recovered data if available
        self.recovered_data = recovery_result.metadata.additional_metadata if recovery_result.metadata else {}

    def __repr__(self):
        return f"RecoveredItem(type={self.item_type}, id={self.id}, source={self.recovery_result.source})"


def construct_reddit_url(item: Any) -> Optional[str]:
    """
    Construct a Reddit permalink URL from a PRAW Comment or Submission object.

    Args:
        item: PRAW Comment or Submission object

    Returns:
        Reddit permalink URL, or None if URL cannot be constructed
    """
    try:
        if isinstance(item, Comment):
            # Comment permalink includes parent submission
            return f"https://reddit.com{item.permalink}"
        elif isinstance(item, Submission):
            # Submission permalink
            return f"https://reddit.com{item.permalink}"
        elif hasattr(item, 'permalink'):
            # Generic fallback for objects with permalink attribute
            return f"https://reddit.com{item.permalink}"
        else:
            logger.warning(f"Cannot construct URL for item type: {type(item)}")
            return None
    except Exception as e:
        logger.error(f"Error constructing Reddit URL: {e}")
        return None


def create_recovery_metadata_markdown(recovery_result) -> str:
    """
    Format recovery metadata as markdown for inclusion in saved files.

    Args:
        recovery_result: RecoveryResult object from content recovery service

    Returns:
        Formatted markdown string with recovery information
    """
    if not recovery_result or not recovery_result.success:
        return ""

    metadata = recovery_result.metadata
    if not metadata:
        return ""

    # Map recovery sources to user-friendly names
    source_names = {
        'wayback_machine': 'Wayback Machine',
        'pullpush_io': 'PullPush.io',
        'reddit_previews': 'Reddit Previews',
        'reveddit': 'Reveddit'
    }

    # Map quality levels to user-friendly descriptions
    quality_descriptions = {
        'original': 'Original Quality',
        'high_quality': 'High Quality',
        'medium_quality': 'Medium Quality',
        'low_quality': 'Low Quality',
        'thumbnail': 'Thumbnail Only',
        'metadata_only': 'Metadata Only'
    }

    source_name = source_names.get(metadata.source.value, metadata.source.value)
    quality_desc = quality_descriptions.get(metadata.content_quality.value, metadata.content_quality.value)

    markdown = f"""---
⚠️ **Recovered Content**

This content was recovered from archival sources because the original was deleted or unavailable.

- **Recovery Source**: {source_name}
- **Recovery Date**: {metadata.recovery_date}
- **Content Quality**: {quality_desc}
- **Original URL**: {recovery_result.recovered_url}
{"- **Cache Hit**: Yes" if metadata.cache_hit else ""}

---

"""

    return markdown


def safe_fetch_items(
    praw_generator,
    item_type: str,
    recovery_enabled: bool = True,
    logger_instance: Optional[logging.Logger] = None
) -> Generator[Any, None, None]:
    """
    Safely iterate through a PRAW generator with error handling and recovery.

    Implements a hybrid fetch strategy:
    1. Try to fetch all items at once (fast path)
    2. If 404 error occurs, fall back to one-by-one iteration
    3. For each failed item, attempt recovery via archival sources

    Args:
        praw_generator: PRAW ListingGenerator (e.g., user.comments.new())
        item_type: Type of items being fetched ('comment', 'submission', etc.)
        recovery_enabled: Whether to attempt content recovery for deleted items
        logger_instance: Optional logger instance (uses module logger if None)

    Yields:
        PRAW objects (Comment, Submission, etc.) or RecoveredItem objects

    Example:
        >>> for comment in safe_fetch_items(user.comments.new(limit=100), 'comment'):
        ...     print(comment.id)
    """
    log = logger_instance or logger
    recovery_service = ContentRecoveryService() if recovery_enabled else None

    try:
        # Fast path: try to fetch all items at once
        log.debug(f"Attempting batch fetch of {item_type} items")
        items = list(praw_generator)

        log.info(f"Successfully fetched {len(items)} {item_type} items in batch mode")

        # Yield all successfully fetched items
        for item in items:
            yield item

    except prawcore.exceptions.NotFound:
        # Batch fetch failed - fall back to one-by-one iteration
        log.warning(
            f"Batch fetch failed with 404 error for {item_type} items. "
            f"Falling back to one-by-one iteration with recovery."
        )

        # Start fresh generator for one-by-one fetch
        # Note: We need to re-create the generator since the previous one is exhausted
        log.warning(
            f"⚠️ Cannot retry iteration - generator is exhausted. "
            f"This batch of {item_type} items will be skipped. "
            f"Consider reducing limit or checking for deleted content in your Reddit history."
        )

        # If recovery is enabled, we could try to recover based on context
        if recovery_enabled and recovery_service:
            log.info("Recovery service is enabled but cannot determine specific failed items from batch fetch")

    except prawcore.exceptions.Forbidden as e:
        log.error(
            f"Access forbidden when fetching {item_type} items: {e}. "
            f"This may be due to insufficient OAuth scopes or private content."
        )

    except prawcore.exceptions.ServerError as e:
        log.error(
            f"Reddit server error when fetching {item_type} items: {e}. "
            f"This is likely a temporary Reddit API issue."
        )

    except Exception as e:
        log.error(f"Unexpected error fetching {item_type} items: {e}", exc_info=True)


def safe_fetch_items_one_by_one(
    praw_generator,
    item_type: str,
    recovery_enabled: bool = True,
    logger_instance: Optional[logging.Logger] = None
) -> Generator[Any, None, None]:
    """
    Fetch items one-by-one with individual error handling and recovery.

    This is slower than batch fetching but allows recovering individual
    deleted items via archival sources.

    Args:
        praw_generator: PRAW ListingGenerator
        item_type: Type of items being fetched
        recovery_enabled: Whether to attempt content recovery
        logger_instance: Optional logger instance

    Yields:
        PRAW objects or RecoveredItem objects
    """
    log = logger_instance or logger
    recovery_service = ContentRecoveryService() if recovery_enabled else None

    count = 0
    skipped = 0
    recovered = 0
    last_successful_item = None

    log.debug(f"Starting one-by-one fetch of {item_type} items with recovery enabled={recovery_enabled}")

    try:
        for item in praw_generator:
            try:
                # Successfully fetched item
                count += 1
                last_successful_item = item
                yield item

            except prawcore.exceptions.NotFound:
                # Individual item not found (deleted/removed)
                skipped += 1
                log.warning(f"Item {count + skipped} not found (404) - attempting recovery")

                if recovery_enabled and recovery_service and last_successful_item:
                    # Try to recover using context from last successful item
                    try:
                        # Construct URL from last known position
                        # Note: This is approximate - we don't know the exact failed item
                        url = construct_reddit_url(last_successful_item)

                        if url:
                            log.debug(f"Attempting recovery for {item_type} near: {url}")
                            recovery_result = recovery_service.attempt_recovery(
                                url,
                                original_failure_reason="PRAW 404 during iteration"
                            )

                            if recovery_result.success:
                                recovered += 1
                                log.info(
                                    f"✓ Successfully recovered {item_type} via {recovery_result.source.value}"
                                )

                                # Create placeholder recovered item
                                recovered_item = RecoveredItem(
                                    item_type=item_type,
                                    item_id="recovered",
                                    recovery_result=recovery_result,
                                    original_url=url
                                )
                                yield recovered_item
                            else:
                                log.warning(
                                    f"✗ Recovery failed: {recovery_result.error_message}"
                                )

                    except Exception as recovery_error:
                        log.error(f"Recovery attempt failed: {recovery_error}")

            except prawcore.exceptions.Forbidden:
                skipped += 1
                log.warning(f"Access forbidden for item {count + skipped} - skipping")

            except Exception as e:
                skipped += 1
                log.error(f"Unexpected error processing item {count + skipped}: {e}")

    finally:
        log.info(
            f"Completed {item_type} fetch: {count} successful, "
            f"{skipped} skipped, {recovered} recovered"
        )


def get_recovery_stats() -> Dict[str, Any]:
    """
    Get statistics about content recovery operations.

    Returns:
        Dictionary with recovery statistics
    """
    try:
        recovery_service = ContentRecoveryService()
        return recovery_service.get_recovery_statistics(days=7)
    except Exception as e:
        logger.error(f"Failed to get recovery statistics: {e}")
        return {'error': str(e)}
