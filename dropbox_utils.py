"""Backward-compatible Dropbox-only entry point.

Use ``storage_utils.py`` for new automation. This wrapper preserves existing
Dropbox commands while routing them through the shared storage provider.
"""

import os

from storage_utils import main as storage_main


def main():
    """Run the unified storage command with the Dropbox provider selected."""
    os.environ["STORAGE_PROVIDER"] = "dropbox"
    storage_main()


if __name__ == "__main__":
    main()
