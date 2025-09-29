"""
Reveddit provider for content recovery.

This module implements content recovery using Reveddit.com, which specializes
in showing moderator-deleted Reddit content. Note that it cannot show
user-deleted content and relies on Pushshift archive data.

Key Features:
- Moderator-deleted content recovery
- Cannot recover user-deleted content
- Uses Pushshift archive data
- Works by URL replacement (reddit.com -> reveddit.com)

Limitations:
- Only moderator-deleted content (not user-deleted)
- Cannot retrieve titles/links of user-deleted submissions
- May not show very old or very recent content
"""

import time
import requests
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse, quote
import re

from ..recovery_metadata import RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality
from ...rate_limiter import rate_limited


class RevedditProvider:
    """Provider for recovering moderator-deleted Reddit content using Reveddit."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Reddit Stash Content Recovery/1.0 (Personal Archive Tool)',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive'
        })
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Reveddit API endpoints (if available)
        self.reveddit_base = "https://www.reveddit.com"
        self.reveddit_api = "https://www.reveddit.com/api"

    @rate_limited('reveddit', timeout=30)
    def attempt_recovery(self, url: str) -> RecoveryResult:
        """
        Attempt to recover moderator-deleted Reddit content using Reveddit.

        Args:
            url: Original Reddit URL to recover

        Returns:
            RecoveryResult with status and recovered URL if successful
        """
        start_time = time.time()

        try:
            self._logger.debug(f"Attempting Reveddit recovery for: {url}")

            # Validate that this is a Reddit URL
            if not self._is_reddit_url(url):
                return RecoveryResult.failure_result(
                    "URL is not a valid Reddit URL",
                    RecoverySource.REVEDDIT
                )

            # Create Reveddit URL by replacing domain
            reveddit_url = self._create_reveddit_url(url)
            if not reveddit_url:
                return RecoveryResult.failure_result(
                    "Could not create valid Reveddit URL",
                    RecoverySource.REVEDDIT
                )

            # Check if Reveddit has content for this URL
            result = self._check_reveddit_content(reveddit_url, url)

            duration = time.time() - start_time

            if result:
                return self._create_success_result(url, result, duration)
            else:
                return RecoveryResult.failure_result(
                    f"Content not found or not moderator-deleted (duration: {duration:.2f}s)",
                    RecoverySource.REVEDDIT
                )

        except requests.exceptions.Timeout:
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"Reveddit request timed out after {duration:.2f}s",
                RecoverySource.REVEDDIT
            )
        except requests.exceptions.RequestException as e:
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"Reveddit request failed: {str(e)} (duration: {duration:.2f}s)",
                RecoverySource.REVEDDIT
            )
        except Exception as e:
            duration = time.time() - start_time
            self._logger.error(f"Unexpected error in Reveddit recovery: {e}")
            return RecoveryResult.failure_result(
                f"Unexpected error: {str(e)} (duration: {duration:.2f}s)",
                RecoverySource.REVEDDIT
            )

    def _is_reddit_url(self, url: str) -> bool:
        """Check if URL is a valid Reddit URL."""
        try:
            parsed = urlparse(url)
            return any(domain in parsed.netloc.lower() for domain in ['reddit.com', 'redd.it'])
        except Exception:
            return False

    def _create_reveddit_url(self, reddit_url: str) -> Optional[str]:
        """Create Reveddit URL by replacing Reddit domain."""
        try:
            # Parse the Reddit URL
            parsed = urlparse(reddit_url)

            # Replace domain with reveddit.com
            if 'reddit.com' in parsed.netloc.lower():
                reveddit_url = reddit_url.replace(parsed.netloc, 'www.reveddit.com')
            elif 'redd.it' in parsed.netloc.lower():
                # For shortened URLs, we need to construct the full path
                # This is complex and may not always work
                self._logger.debug("Shortened Reddit URL may not work with Reveddit")
                return None
            else:
                return None

            # Clean up the URL
            reveddit_url = reveddit_url.replace('//www.reddit.com', '//www.reveddit.com')

            self._logger.debug(f"Created Reveddit URL: {reveddit_url}")
            return reveddit_url

        except Exception as e:
            self._logger.debug(f"Failed to create Reveddit URL: {e}")
            return None

    def _check_reveddit_content(self, reveddit_url: str, original_url: str) -> Optional[Dict[str, Any]]:
        """Check if Reveddit has content for the URL."""
        try:
            # Try to access the Reveddit page
            response = self.session.get(reveddit_url, timeout=self.timeout, allow_redirects=True)

            # Check if we got a valid response
            if response.status_code == 200:
                content = response.text

                # Look for indicators that content was found
                # Reveddit shows deleted content differently than missing content
                content_indicators = [
                    'removed by moderator',
                    'deleted by user',
                    'removed automatically',
                    'comment score below threshold',
                    'removed by automod'
                ]

                # Check for content indicators in the response
                content_lower = content.lower()
                has_deleted_content = any(indicator in content_lower for indicator in content_indicators)

                # Also check for error indicators
                error_indicators = [
                    'no results found',
                    'nothing here',
                    'unable to find',
                    'error loading'
                ]

                has_errors = any(error in content_lower for error in error_indicators)

                if has_deleted_content and not has_errors:
                    self._logger.debug(f"Found deleted content on Reveddit: {reveddit_url}")

                    # Try to extract some metadata from the page
                    metadata = self._extract_page_metadata(content, reveddit_url)

                    return {
                        'url': reveddit_url,
                        'status_code': response.status_code,
                        'content_length': len(content),
                        'has_deleted_content': True,
                        'metadata': metadata
                    }

                # Check if page loads but no deleted content found
                elif response.status_code == 200 and len(content) > 5000:
                    # Page loaded successfully but may not have deleted content
                    # This could mean the content wasn't deleted by moderators
                    self._logger.debug(f"Reveddit page loaded but no deleted content indicators found")
                    return None

        except requests.exceptions.RequestException as e:
            self._logger.debug(f"Reveddit content check failed: {e}")
        except Exception as e:
            self._logger.debug(f"Unexpected error checking Reveddit content: {e}")

        return None

    def _extract_page_metadata(self, content: str, reveddit_url: str) -> Dict[str, Any]:
        """Extract metadata from Reveddit page content."""
        metadata = {}

        try:
            # Try to extract title from HTML
            title_match = re.search(r'<title[^>]*>([^<]+)</title>', content, re.IGNORECASE)
            if title_match:
                metadata['page_title'] = title_match.group(1).strip()

            # Look for subreddit information
            subreddit_match = re.search(r'/r/([a-zA-Z0-9_]+)', reveddit_url)
            if subreddit_match:
                metadata['subreddit'] = subreddit_match.group(1)

            # Look for thread ID
            thread_match = re.search(r'/comments/([a-zA-Z0-9]+)', reveddit_url)
            if thread_match:
                metadata['thread_id'] = thread_match.group(1)

            # Basic content analysis
            metadata['content_length'] = len(content)
            metadata['has_scripts'] = 'script' in content.lower()

        except Exception as e:
            self._logger.debug(f"Error extracting metadata: {e}")

        return metadata

    def _create_success_result(self, original_url: str, reveddit_data: Dict[str, Any],
                             duration: float) -> RecoveryResult:
        """Create a successful recovery result with metadata."""

        # Reveddit shows deleted content, so quality depends on what was recovered
        quality = self._assess_content_quality(reveddit_data)

        metadata = RecoveryMetadata(
            source=RecoverySource.REVEDDIT,
            recovered_url=reveddit_data['url'],
            recovery_timestamp=time.time(),
            content_quality=quality,
            attempt_duration=duration,
            additional_metadata={
                'status_code': reveddit_data.get('status_code'),
                'content_length': reveddit_data.get('content_length'),
                'has_deleted_content': reveddit_data.get('has_deleted_content'),
                'reveddit_metadata': reveddit_data.get('metadata', {}),
                'content_type': 'moderator_deleted_reddit_content'
            }
        )

        return RecoveryResult.success_result(reveddit_data['url'], metadata)

    def _assess_content_quality(self, reveddit_data: Dict[str, Any]) -> RecoveryQuality:
        """Assess the quality of recovered content from Reveddit."""

        # Since Reveddit shows deleted content, the quality depends on
        # what information is still available

        metadata = reveddit_data.get('metadata', {})
        content_length = reveddit_data.get('content_length', 0)

        # If we have good metadata and substantial content
        if metadata.get('page_title') and content_length > 10000:
            return RecoveryQuality.MEDIUM_QUALITY
        elif content_length > 5000:
            return RecoveryQuality.LOW_QUALITY
        else:
            # Minimal information available
            return RecoveryQuality.METADATA_ONLY

    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about this recovery provider."""
        return {
            'name': 'Reveddit',
            'source': RecoverySource.REVEDDIT,
            'description': 'Reveddit.com - shows moderator-deleted Reddit content',
            'rate_limits': 'No specific limits documented',
            'ethics': 'Ethical for personal use - shows publicly posted content',
            'reliability': 'Medium - depends on Pushshift data availability',
            'coverage': 'Moderator-deleted Reddit content only (not user-deleted)',
            'limitations': 'Cannot show user-deleted content, may miss very old/recent content'
        }