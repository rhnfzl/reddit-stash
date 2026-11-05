"""
Context manager that removes temporary files even when an exception occurs.
"""

import os
import logging
from contextlib import contextmanager
from collections.abc import Generator
from pathlib import Path


logger = logging.getLogger(__name__)


@contextmanager
def temp_files_cleanup(*paths: str | Path) -> Generator[None, None, None]:
    """Remove the given files on exit, skipping empty paths and logging any that stay behind."""
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
