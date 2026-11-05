"""Progress reporting for long directory syncs."""

import logging
import time

from utils.storage.base import SyncTally

logger = logging.getLogger(__name__)

SMALL_RUN_FILES = 20
PROGRESS_INTERVAL_SECONDS = 10


def format_size(nbytes: float) -> str:
    """Format byte count as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs:02d}s"


class SyncProgress:
    """Logs sync progress per file on small runs, else periodically and at the end."""

    def __init__(self, verb: str, total_files: int, started: float):
        self._verb = verb
        self._total = total_files
        self._started = started
        self._last_report = time.time()

    def report(self, tally: SyncTally) -> None:
        now = time.time()
        done = tally.done
        if (self._total > SMALL_RUN_FILES and done != self._total
                and now - self._last_report < PROGRESS_INTERVAL_SECONDS):
            return
        self._last_report = now
        eta = ""
        if 0 < done < self._total:
            eta_secs = ((now - self._started) / done) * (self._total - done)
            eta = f" | ETA: ~{_format_duration(eta_secs)}"
        logger.info(
            f"  [{done}/{self._total}] {done / self._total * 100:.0f}% | "
            f"{format_size(tally.bytes_moved)} {self._verb}, "
            f"{tally.skipped} skipped, {tally.failed} failed{eta}"
        )
