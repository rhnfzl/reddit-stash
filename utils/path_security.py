"""
Path security module for Reddit Stash.

This module provides secure path handling to prevent directory traversal attacks
and ensure all file operations stay within designated directories. Implements
2024 security best practices for file path validation and sanitization.
"""

import os
import re
import logging
from typing import Optional
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass
class PathValidationResult:
    """Result of path validation with security details."""
    is_safe: bool
    safe_path: Optional[str] = None
    issues: list = None
    sanitized_component: Optional[str] = None

    def __post_init__(self):
        if self.issues is None:
            self.issues = []


class SecurePathHandler:
    """
    Secure path handler for preventing directory traversal attacks.

    Features:
    - Path traversal prevention using realpath validation
    - Input sanitization for filenames and directory names
    - Safe character allowlisting
    - Length validation
    - Reserved name detection
    """

    # Safe characters for filenames and directory names
    SAFE_CHARS_PATTERN = re.compile(r'^[a-zA-Z0-9._\-\s]+$')

    # Characters to remove/replace
    DANGEROUS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

    # Path traversal patterns
    TRAVERSAL_PATTERNS = [
        re.compile(r'\.\.'),           # Any .. sequence
        re.compile(r'[\\/]\.\.[\\/]'), # ../  or ..\
        re.compile(r'^\.\.[\\/]'),     # Starting with ../
        re.compile(r'[\\/]\.\.$'),     # Ending with /..
    ]

    # Reserved filenames (Windows-specific but good to avoid everywhere)
    RESERVED_NAMES = {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
    }

    def __init__(self, max_component_length: int = 255, max_path_length: int = 4096):
        """
        Initialize the secure path handler.

        Args:
            max_component_length: Maximum length for individual path components
            max_path_length: Maximum total path length
        """
        self.max_component_length = max_component_length
        self.max_path_length = max_path_length
        logger.info("Secure Path Handler initialized")

    def sanitize_path_component(self, component: str) -> PathValidationResult:
        """
        Sanitize a single path component (filename or directory name).

        Args:
            component: Path component to sanitize

        Returns:
            PathValidationResult with sanitized component or validation errors
        """
        if not component or not isinstance(component, str):
            return PathValidationResult(
                is_safe=False,
                issues=["Component is empty or not a string"]
            )

        issues = []

        # Strip whitespace
        component = component.strip()

        if not component:
            return PathValidationResult(
                is_safe=False,
                issues=["Component is empty after stripping whitespace"]
            )

        # Check length
        if len(component) > self.max_component_length:
            issues.append(f"Component too long ({len(component)} > {self.max_component_length})")
            component = component[:self.max_component_length]

        # Check for traversal patterns
        for pattern in self.TRAVERSAL_PATTERNS:
            if pattern.search(component):
                issues.append(f"Directory traversal pattern detected: {pattern.pattern}")

        # Check for reserved names
        component_upper = component.upper()
        if component_upper in self.RESERVED_NAMES:
            issues.append(f"Reserved filename: {component}")
            component = f"_{component}"  # Prefix with underscore

        # Remove dangerous characters
        original_length = len(component)
        component = self.DANGEROUS_CHARS.sub('_', component)
        if len(component) != original_length:
            issues.append("Dangerous characters replaced with underscores")

        # Additional cleanup
        component = self._clean_component(component)

        # Final validation
        if not component or component in ['.', '..']:
            return PathValidationResult(
                is_safe=False,
                issues=issues + ["Component resolved to unsafe value"]
            )

        # Check if it's safe enough
        has_critical_issues = any("traversal" in issue.lower() or "reserved" in issue.lower()
                                for issue in issues)

        return PathValidationResult(
            is_safe=not has_critical_issues,
            safe_path=component,
            issues=issues,
            sanitized_component=component
        )

    def _clean_component(self, component: str) -> str:
        """
        Additional cleaning for path components.

        Args:
            component: Component to clean

        Returns:
            Cleaned component
        """
        # Remove leading/trailing dots and spaces
        component = component.strip('. ')

        # Replace multiple underscores with single
        component = re.sub(r'_+', '_', component)

        # Ensure it doesn't start with a dot (hidden files)
        if component.startswith('.'):
            component = 'dot_' + component[1:]

        return component

    def create_safe_path(self, base_directory: str, *path_components: str) -> PathValidationResult:
        """
        Create a safe path by joining components securely.

        Args:
            base_directory: Base directory (must be absolute)
            *path_components: Path components to join

        Returns:
            PathValidationResult with safe path or validation errors
        """
        if not base_directory or not os.path.isabs(base_directory):
            return PathValidationResult(
                is_safe=False,
                issues=["Base directory must be an absolute path"]
            )

        # Normalize base directory
        try:
            base_directory = os.path.realpath(base_directory)
        except Exception as e:
            return PathValidationResult(
                is_safe=False,
                issues=[f"Failed to resolve base directory: {str(e)}"]
            )

        # Sanitize all components
        safe_components = []
        all_issues = []

        for component in path_components:
            if not component:
                continue

            result = self.sanitize_path_component(component)
            if not result.is_safe:
                return PathValidationResult(
                    is_safe=False,
                    issues=all_issues + result.issues
                )

            safe_components.append(result.sanitized_component)
            all_issues.extend(result.issues)

        # Build the path
        try:
            safe_path = os.path.join(base_directory, *safe_components)
            safe_path = os.path.realpath(safe_path)
        except Exception as e:
            return PathValidationResult(
                is_safe=False,
                issues=all_issues + [f"Failed to construct path: {str(e)}"]
            )

        # Verify the path is within base directory
        if not self._is_path_within_base(safe_path, base_directory):
            return PathValidationResult(
                is_safe=False,
                issues=all_issues + ["Path escapes base directory"]
            )

        # Check total path length
        if len(safe_path) > self.max_path_length:
            return PathValidationResult(
                is_safe=False,
                issues=all_issues + [f"Total path too long ({len(safe_path)} > {self.max_path_length})"]
            )

        return PathValidationResult(
            is_safe=True,
            safe_path=safe_path,
            issues=all_issues
        )

    def _is_path_within_base(self, path: str, base: str) -> bool:
        """
        Check if a path is within the base directory.

        Args:
            path: Path to check
            base: Base directory

        Returns:
            True if path is within base directory
        """
        try:
            # Use os.path.commonpath to check containment
            return os.path.commonpath([path, base]) == base
        except ValueError:
            # Different drives on Windows or other issues
            return False

    def validate_existing_path(self, path: str, base_directory: str) -> PathValidationResult:
        """
        Validate an existing path for security.

        Args:
            path: Path to validate
            base_directory: Expected base directory

        Returns:
            PathValidationResult
        """
        if not path or not isinstance(path, str):
            return PathValidationResult(
                is_safe=False,
                issues=["Path is empty or not a string"]
            )

        try:
            # Resolve both paths
            resolved_path = os.path.realpath(path)
            resolved_base = os.path.realpath(base_directory)

            # Check containment
            if not self._is_path_within_base(resolved_path, resolved_base):
                return PathValidationResult(
                    is_safe=False,
                    issues=["Path is outside base directory"]
                )

            return PathValidationResult(
                is_safe=True,
                safe_path=resolved_path
            )

        except Exception as e:
            return PathValidationResult(
                is_safe=False,
                issues=[f"Path validation failed: {str(e)}"]
            )

    def create_reddit_file_path(self, base_directory: str, subreddit_name: str,
                              content_type: str, content_id: str) -> PathValidationResult:
        """
        Create a safe file path for Reddit content.

        Args:
            base_directory: Base directory for saving files
            subreddit_name: Subreddit name (from Reddit API)
            content_type: Type of content (POST, COMMENT, etc.)
            content_id: Reddit content ID

        Returns:
            PathValidationResult with safe file path
        """
        # Validate content type
        valid_types = {'POST', 'COMMENT', 'SAVED_POST', 'SAVED_COMMENT',
                      'UPVOTE_POST', 'UPVOTE_COMMENT', 'GDPR_POST', 'GDPR_COMMENT'}

        if content_type not in valid_types:
            return PathValidationResult(
                is_safe=False,
                issues=[f"Invalid content type: {content_type}"]
            )

        # Create filename
        filename = f"{content_type}_{content_id}.md"

        # Create safe path
        return self.create_safe_path(base_directory, subreddit_name, filename)


# Global instance
_global_path_handler = None


def get_path_handler() -> SecurePathHandler:
    """
    Get the global secure path handler instance.

    Returns:
        Global SecurePathHandler instance
    """
    global _global_path_handler
    if _global_path_handler is None:
        _global_path_handler = SecurePathHandler()
    return _global_path_handler


def create_safe_path(base_directory: str, *components: str) -> PathValidationResult:
    """
    Convenience function to create a safe path.

    Args:
        base_directory: Base directory
        *components: Path components

    Returns:
        PathValidationResult
    """
    return get_path_handler().create_safe_path(base_directory, *components)


def create_reddit_file_path(base_directory: str, subreddit_name: str,
                          content_type: str, content_id: str) -> PathValidationResult:
    """
    Convenience function to create safe Reddit file path.

    Args:
        base_directory: Base directory
        subreddit_name: Subreddit name
        content_type: Content type
        content_id: Content ID

    Returns:
        PathValidationResult
    """
    return get_path_handler().create_reddit_file_path(
        base_directory, subreddit_name, content_type, content_id
    )