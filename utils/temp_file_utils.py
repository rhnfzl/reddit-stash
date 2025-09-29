"""
Temporary file management utilities for guaranteed cleanup.

This module provides context managers and utilities to ensure temporary files
are properly cleaned up even when exceptions occur, addressing security and
disk space concerns from PR feedback.
"""

import os
import tempfile
import logging
from contextlib import contextmanager
from typing import Generator, List, Union
from pathlib import Path


logger = logging.getLogger(__name__)


@contextmanager
def temp_files_cleanup(*paths: Union[str, Path]) -> Generator[None, None, None]:
    """
    Context manager that ensures temporary files are cleaned up.

    Guarantees cleanup of specified file paths when the context exits,
    whether due to successful completion or exceptions.

    Args:
        *paths: Variable number of file paths to clean up

    Example:
        with temp_files_cleanup(temp_video, temp_audio):
            # Do operations with temp files
            pass
        # Files are guaranteed to be cleaned up
    """
    try:
        yield
    finally:
        for path in paths:
            if path:
                try:
                    path_str = str(path)
                    if os.path.exists(path_str):
                        os.remove(path_str)
                        logger.debug(f"Cleaned up temporary file: {path_str}")
                except OSError as e:
                    logger.warning(f"Failed to clean up temporary file {path_str}: {e}")


@contextmanager
def temp_directory_cleanup(path: Union[str, Path]) -> Generator[None, None, None]:
    """
    Context manager that ensures a temporary directory is cleaned up.

    Args:
        path: Path to temporary directory to clean up

    Example:
        import tempfile
        temp_dir = tempfile.mkdtemp()
        with temp_directory_cleanup(temp_dir):
            # Use temp_dir
            pass
        # Directory is guaranteed to be cleaned up
    """
    try:
        yield
    finally:
        if path:
            try:
                import shutil
                path_str = str(path)
                if os.path.exists(path_str) and os.path.isdir(path_str):
                    shutil.rmtree(path_str)
                    logger.debug(f"Cleaned up temporary directory: {path_str}")
            except OSError as e:
                logger.warning(f"Failed to clean up temporary directory {path_str}: {e}")


def safe_temp_file(suffix: str = "", prefix: str = "reddit_stash_",
                   dir: str = None, delete: bool = False) -> str:
    """
    Create a temporary file with guaranteed cleanup tracking.

    Args:
        suffix: File suffix/extension
        prefix: File prefix
        dir: Directory to create temp file in
        delete: Whether file should auto-delete on close

    Returns:
        Path to temporary file

    Note:
        Use with temp_files_cleanup() context manager for guaranteed cleanup.
    """
    fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=dir)
    os.close(fd)  # Close file descriptor, keep the file

    if delete:
        # Register for cleanup when Python exits
        import atexit
        atexit.register(lambda: _safe_remove(path))

    logger.debug(f"Created temporary file: {path}")
    return path


def _safe_remove(path: str) -> None:
    """Safely remove a file without raising exceptions."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass  # Silent cleanup for atexit scenarios