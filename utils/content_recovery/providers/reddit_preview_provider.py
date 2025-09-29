"""
Reddit Preview provider for content recovery.

This module implements content recovery using Reddit's own preview services
(preview.redd.it and external-preview.redd.it). These are low-quality previews
but may be the only remaining copy of external content.

Known Limitations:
- Requires session management and proper headers
- May return 403 errors for direct access
- Preview quality is limited
- URL encoding issues common
"""

import time
import requests
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse, quote, unquote
import re

from utils.content_recovery.recovery_metadata import RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality
from utils.rate_limiter import rate_limited


class RedditPreviewProvider:
    """Provider for recovering content from Reddit's preview services."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()

        # Enhanced headers to mimic browser behavior
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0'
        })

        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Reddit preview domains
        self.preview_domains = [
            'preview.redd.it',
            'external-preview.redd.it'
        ]

    @rate_limited('reddit_previews', timeout=30)
    def attempt_recovery(self, url: str) -> RecoveryResult:
        """
        Attempt to recover content from Reddit preview services.

        Args:
            url: Original URL to recover

        Returns:
            RecoveryResult with status and recovered URL if successful
        """
        start_time = time.time()

        try:
            self._logger.debug(f"Attempting Reddit preview recovery for: {url}")

            # Check if this is already a Reddit preview URL
            if self._is_reddit_preview_url(url):
                result = self._verify_preview_url(url)
                if result:
                    duration = time.time() - start_time
                    return self._create_success_result(url, result, duration)

            # Try to find preview URLs through Reddit API or reconstruct them
            preview_candidates = self._generate_preview_candidates(url)

            for candidate_url in preview_candidates:
                result = self._verify_preview_url(candidate_url)
                if result:
                    duration = time.time() - start_time
                    return self._create_success_result(url, result, duration)

            # No working preview found
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"No accessible Reddit preview found (search duration: {duration:.2f}s)",
                RecoverySource.REDDIT_PREVIEWS
            )

        except requests.exceptions.Timeout:
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"Reddit preview request timed out after {duration:.2f}s",
                RecoverySource.REDDIT_PREVIEWS
            )
        except requests.exceptions.RequestException as e:
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"Reddit preview request failed: {str(e)} (duration: {duration:.2f}s)",
                RecoverySource.REDDIT_PREVIEWS
            )
        except Exception as e:
            duration = time.time() - start_time
            self._logger.error(f"Unexpected error in Reddit preview recovery: {e}")
            return RecoveryResult.failure_result(
                f"Unexpected error: {str(e)} (duration: {duration:.2f}s)",
                RecoverySource.REDDIT_PREVIEWS
            )

    def _is_reddit_preview_url(self, url: str) -> bool:
        """Check if URL is already a Reddit preview URL."""
        try:
            parsed = urlparse(url)
            return any(domain in parsed.netloc.lower() for domain in self.preview_domains)
        except Exception:
            return False

    def _generate_preview_candidates(self, url: str) -> list[str]:
        """Generate potential Reddit preview URLs for the given URL."""
        candidates = []

        try:
            # For external URLs, Reddit might have generated previews
            # These are speculative reconstructions based on Reddit's preview patterns

            # external-preview.redd.it pattern
            encoded_url = quote(url, safe='')
            candidates.extend([
                f"https://external-preview.redd.it/{encoded_url}",
                f"https://preview.redd.it/{encoded_url}",
            ])

            # Try with different encoding patterns
            double_encoded = quote(quote(url, safe=''), safe='')
            candidates.extend([
                f"https://external-preview.redd.it/{double_encoded}",
                f"https://preview.redd.it/{double_encoded}",
            ])

            # If it's an image URL, try common preview patterns
            if self._is_image_url(url):
                domain_hash = str(hash(urlparse(url).netloc))[-8:]
                candidates.extend([
                    f"https://preview.redd.it/preview-{domain_hash}.jpg",
                    f"https://external-preview.redd.it/preview-{domain_hash}.jpg",
                ])

        except Exception as e:
            self._logger.debug(f"Error generating preview candidates: {e}")

        # Remove duplicates while preserving order
        seen = set()
        unique_candidates = []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                unique_candidates.append(candidate)

        return unique_candidates

    def _is_image_url(self, url: str) -> bool:
        """Check if URL appears to be an image."""
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
            return any(path.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp'])
        except Exception:
            return False

    def _verify_preview_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Verify that a preview URL is accessible and return metadata."""
        try:
            # Make a HEAD request first to check accessibility
            head_response = self.session.head(url, timeout=self.timeout, allow_redirects=True)

            if head_response.status_code == 200:
                content_type = head_response.headers.get('content-type', '').lower()
                content_length = head_response.headers.get('content-length')

                # Verify it's actually content (not an error page)
                if 'image' in content_type or 'video' in content_type:
                    self._logger.debug(f"Found accessible Reddit preview: {url}")
                    return {
                        'url': url,
                        'content_type': content_type,
                        'content_length': content_length,
                        'status_code': head_response.status_code,
                        'headers': dict(head_response.headers)
                    }
                elif content_length and int(content_length) > 1000:  # Reasonable content size
                    # Might be accessible content even if not media
                    self._logger.debug(f"Found accessible Reddit preview: {url}")
                    return {
                        'url': url,
                        'content_type': content_type,
                        'content_length': content_length,
                        'status_code': head_response.status_code,
                        'headers': dict(head_response.headers)
                    }

        except requests.exceptions.RequestException as e:
            self._logger.debug(f"Preview URL verification failed for {url}: {e}")
        except Exception as e:
            self._logger.debug(f"Unexpected error verifying preview URL {url}: {e}")

        return None

    def _create_success_result(self, original_url: str, preview_data: Dict[str, Any],
                             duration: float) -> RecoveryResult:
        """Create a successful recovery result with metadata."""

        # Reddit previews are always lower quality than originals
        quality = self._assess_preview_quality(preview_data)

        metadata = RecoveryMetadata(
            source=RecoverySource.REDDIT_PREVIEWS,
            recovered_url=preview_data['url'],
            recovery_timestamp=time.time(),
            content_quality=quality,
            attempt_duration=duration,
            additional_metadata={
                'content_type': preview_data.get('content_type'),
                'content_length': preview_data.get('content_length'),
                'status_code': preview_data.get('status_code'),
                'is_reddit_preview': True,
                'original_url': original_url
            }
        )

        return RecoveryResult.success_result(preview_data['url'], metadata)

    def _assess_preview_quality(self, preview_data: Dict[str, Any]) -> RecoveryQuality:
        """Assess the quality of Reddit preview content."""
        content_type = preview_data.get('content_type', '').lower()
        content_length = preview_data.get('content_length')

        try:
            size = int(content_length) if content_length else 0

            # Images
            if 'image' in content_type:
                if size > 500000:  # > 500KB
                    return RecoveryQuality.MEDIUM_QUALITY
                elif size > 50000:  # > 50KB
                    return RecoveryQuality.LOW_QUALITY
                else:
                    return RecoveryQuality.THUMBNAIL

            # Videos
            elif 'video' in content_type:
                if size > 5000000:  # > 5MB
                    return RecoveryQuality.MEDIUM_QUALITY
                else:
                    return RecoveryQuality.LOW_QUALITY

            # Other content
            else:
                if size > 100000:  # > 100KB
                    return RecoveryQuality.LOW_QUALITY
                else:
                    return RecoveryQuality.THUMBNAIL

        except (ValueError, TypeError):
            pass

        # Default for preview content
        return RecoveryQuality.THUMBNAIL

    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about this recovery provider."""
        return {
            'name': 'Reddit Previews',
            'source': RecoverySource.REDDIT_PREVIEWS,
            'description': 'Reddit preview services (preview.redd.it, external-preview.redd.it)',
            'rate_limits': 'No specific limits, but subject to Reddit rate limiting',
            'ethics': 'Ethical - using Reddit\'s own preview system',
            'reliability': 'Low - often returns 403 errors, session-dependent',
            'coverage': 'Limited to content Reddit has generated previews for'
        }