"""
Media download services for Reddit Stash.

This package contains implementations of various media download services
following the Protocol-based architecture defined in service_abstractions.
"""

from .reddit_media import RedditMediaDownloader
from .base_downloader import BaseHTTPDownloader

__all__ = [
    'RedditMediaDownloader',
    'BaseHTTPDownloader'
]