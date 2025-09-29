"""
URL Transformer - Centralized URL preprocessing for direct downloads

Handles simple URL transformations that convert viewer/preview URLs to direct download URLs
without requiring authentication, API keys, or page parsing. Focuses on commonly shared
platforms on Reddit.

Usage:
    transformer = URLTransformer()
    result = transformer.transform(url)
    if result.transformed:
        # Use result.url and result.headers for download
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional, Dict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


@dataclass
class TransformResult:
    """Result of URL transformation"""
    url: str
    transformed: bool = False
    platform: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    notes: Optional[str] = None


class URLTransformer:
    """Centralized URL transformer for direct download access"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

        # Registry of URL transformation patterns
        # Each tuple contains: (pattern, transform_function, platform_name, notes)
        self.transformations = [
            # GitHub blob to raw
            (
                r'github\.com/([^/]+)/([^/]+)/blob/(.+)',
                self._transform_github_blob,
                'GitHub',
                'Converts blob viewer to raw file access'
            ),

            # GitHub Gist to raw
            (
                r'gist\.github\.com/([^/]+)/([^/]+)/?$',
                self._transform_github_gist,
                'GitHub Gist',
                'Converts gist viewer to raw text access'
            ),

            # GitLab blob to raw
            (
                r'gitlab\.com/(.+?)/-/blob/(.+)',
                self._transform_gitlab_blob,
                'GitLab',
                'Converts blob viewer to raw file access'
            ),

            # Bitbucket src to raw
            (
                r'bitbucket\.org/([^/]+)/([^/]+)/src/(.+)',
                self._transform_bitbucket_src,
                'Bitbucket',
                'Converts src viewer to raw file access'
            ),

            # Dropbox dl parameter
            (
                r'dropbox\.com/.*',
                self._transform_dropbox_dl,
                'Dropbox',
                'Forces direct download instead of preview'
            ),

            # Google Drive (small files only)
            (
                r'drive\.google\.com/file/d/([^/]+)/view',
                self._transform_google_drive,
                'Google Drive',
                'Direct download for public files <25MB only'
            ),

            # Pastebin to raw
            (
                r'pastebin\.com/([^/]+)$',
                self._transform_pastebin_raw,
                'Pastebin',
                'Converts paste viewer to raw text'
            ),

            # PostImages to direct
            (
                r'postimg\.cc/([^/]+)$',
                self._transform_postimages,
                'PostImages',
                'Converts viewer page to direct image access'
            ),

            # ImgBB to direct
            (
                r'imgbb\.com/([^/]+)$',
                self._transform_imgbb,
                'ImgBB',
                'Converts viewer page to direct image access'
            ),

            # Ubuntu Paste to plain
            (
                r'paste\.ubuntu\.com/([^/]+)/?$',
                self._transform_ubuntu_paste,
                'Ubuntu Paste',
                'Converts paste viewer to plain text'
            ),
        ]

    def transform(self, url: str) -> TransformResult:
        """
        Transform URL if a known pattern is found

        Args:
            url: Original URL to transform

        Returns:
            TransformResult with transformed URL or original if no transformation applies
        """
        if not url:
            return TransformResult(url=url)

        for pattern, transform_func, platform, notes in self.transformations:
            match = re.search(pattern, url, re.IGNORECASE)
            if match:
                try:
                    transformed_url = transform_func(url, match)
                    if transformed_url and transformed_url != url:
                        self.logger.debug(f"Transformed {platform} URL: {url} -> {transformed_url}")
                        return TransformResult(
                            url=transformed_url,
                            transformed=True,
                            platform=platform,
                            notes=notes
                        )
                except Exception as e:
                    self.logger.warning(f"Failed to transform {platform} URL {url}: {e}")
                    continue

        return TransformResult(url=url)

    def get_domain_info(self, url: str) -> Optional[str]:
        """Get platform name for domain-aware error messages"""
        domain_map = {
            'github.com': 'GitHub',
            'gist.github.com': 'GitHub Gist',
            'gitlab.com': 'GitLab',
            'bitbucket.org': 'Bitbucket',
            'dropbox.com': 'Dropbox',
            'drive.google.com': 'Google Drive',
            'pastebin.com': 'Pastebin',
            'postimg.cc': 'PostImages',
            'imgbb.com': 'ImgBB',
            'paste.ubuntu.com': 'Ubuntu Paste',
        }

        try:
            parsed = urlparse(url)
            return domain_map.get(parsed.netloc.lower())
        except Exception:
            return None

    # Transform functions for each platform

    def _transform_github_blob(self, url: str, match: re.Match) -> str:
        """Transform GitHub blob URL to raw.githubusercontent.com"""
        user, repo, path = match.groups()
        return f"https://raw.githubusercontent.com/{user}/{repo}/{path}"

    def _transform_github_gist(self, url: str, match: re.Match) -> str:
        """Transform GitHub Gist to raw access"""
        user, gist_id = match.groups()
        return f"https://gist.githubusercontent.com/{user}/{gist_id}/raw"

    def _transform_gitlab_blob(self, url: str, match: re.Match) -> str:
        """Transform GitLab blob URL to raw"""
        project_path, file_path = match.groups()
        return f"https://gitlab.com/{project_path}/-/raw/{file_path}"

    def _transform_bitbucket_src(self, url: str, match: re.Match) -> str:
        """Transform Bitbucket src URL to raw"""
        user, repo, path = match.groups()
        return f"https://bitbucket.org/{user}/{repo}/raw/{path}"

    def _transform_dropbox_dl(self, url: str, match: re.Match) -> str:
        """Transform Dropbox URL to force direct download"""
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)

        # Set dl=1 for direct download
        query_params['dl'] = ['1']

        # Remove raw parameter if present (conflicts with dl)
        query_params.pop('raw', None)

        new_query = urlencode(query_params, doseq=True)
        return urlunparse((
            parsed.scheme, parsed.netloc, parsed.path,
            parsed.params, new_query, parsed.fragment
        ))

    def _transform_google_drive(self, url: str, match: re.Match) -> str:
        """Transform Google Drive file URL to direct download (small files only)"""
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    def _transform_pastebin_raw(self, url: str, match: re.Match) -> str:
        """Transform Pastebin URL to raw text"""
        paste_id = match.group(1)
        return f"https://pastebin.com/raw/{paste_id}"

    def _transform_postimages(self, url: str, match: re.Match) -> str:
        """Transform PostImages viewer to direct image (best effort)"""
        image_id = match.group(1)
        # Note: We can't determine exact filename/extension without page parsing
        # This transformation points to the CDN but may require the exact filename
        return f"https://i.postimg.cc/{image_id}/"

    def _transform_imgbb(self, url: str, match: re.Match) -> str:
        """Transform ImgBB viewer to direct image (best effort)"""
        image_id = match.group(1)
        # Note: Similar to PostImages, exact filename may be needed
        return f"https://i.ibb.co/{image_id}/"

    def _transform_ubuntu_paste(self, url: str, match: re.Match) -> str:
        """Transform Ubuntu Paste to plain text"""
        paste_id = match.group(1)
        return f"https://paste.ubuntu.com/{paste_id}/plain/"


# Global instance for easy access
url_transformer = URLTransformer()