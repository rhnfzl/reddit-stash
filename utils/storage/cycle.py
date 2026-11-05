"""
The coordinated backup run behind storage_utils.py --cycle.

Takes the S3 lease, downloads file_log.json, runs the backup, uploads and releases the
lease, so several runners (e.g. a homelab plus GitHub Actions) never sync at the same time.
Exit codes: 0 success, 2 failure, 3 skipped (lease held, lost race, or fresh).
"""

import argparse
import logging
import os
import shutil
import signal
import socket
import sys
from datetime import datetime, timedelta, timezone
from typing import NoReturn
from collections.abc import Callable

import requests

from utils.env_config import load_settings
from utils.storage.base import StorageProvider
from utils.storage.factory import (
    StorageConfig,
    get_remote_directory,
    get_storage_provider,
    load_local_dir,
    load_storage_config,
    load_sync_check_type,
)
from utils.storage.s3_lease import S3Lease
from utils.storage.s3_provider import S3StorageProvider

logger = logging.getLogger(__name__)

EXIT_CYCLE_FAILED = 2
EXIT_CYCLE_SKIPPED = 3


def coordination_enabled() -> bool:
    return os.environ.get("STORAGE_COORDINATION", "").strip().lower() in ("1", "true", "yes")


def _coordination_problems(config: StorageConfig) -> list[str]:
    """Settings that coordinated runs do not support (yet), as readable messages."""
    problems = []
    if config.provider != StorageProvider.S3:
        problems.append("coordination needs provider = s3")
    if config.s3_endpoint_url and "amazonaws.com" not in config.s3_endpoint_url:
        problems.append("coordination is only verified on AWS S3, not custom endpoints")
    if not config.remote_directory.strip("/"):
        problems.append("coordination needs a non-empty dropbox_directory (the S3 key prefix)")
    parser = load_settings()
    if parser.getboolean("Settings", "unsave_after_download", fallback=False):
        problems.append("unsave_after_download = true can lose items if an upload fails")
    if parser.getboolean("Settings", "process_gdpr", fallback=False):
        problems.append("process_gdpr = true needs the CSV export on every runner")
    if load_sync_check_type() != "LOG":
        problems.append("coordination needs check_type = LOG")
    return problems


def _default_runner() -> str:
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        return f"github:{run_id}:{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return socket.gethostname()


def _ping_healthcheck(success: bool) -> None:
    """Optional dead-man's-switch ping (e.g. healthchecks.io); never fails the run."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        requests.get(url if success else f"{url.rstrip('/')}/fail", timeout=10)
    except Exception as exc:
        # Only the type: the message would contain the ping URL, which is a secret.
        logger.warning(f"Healthcheck ping failed: {type(exc).__name__}")


def _sync_cycle(provider: S3StorageProvider, lease: S3Lease, prefix: str, local_dir: str,
                require_stale: float | None, run_backup: Callable[[], bool]) -> int:
    """Body of --cycle while holding the lease; returns the exit code."""
    if require_stale is not None:
        last = lease.last_success(lease.body)
        if last and datetime.now(timezone.utc) - last < timedelta(hours=require_stale):
            logger.info(f"Last successful run was at {last:%Y-%m-%d %H:%M} UTC, within "
                        f"{require_stale}h; nothing to do.")
            return EXIT_CYCLE_SKIPPED

    result = provider.download_log(prefix, local_dir)
    if result.failed or result.errors:
        logger.error(f"Download failed: {result.errors[:3]}")
        return EXIT_CYCLE_FAILED

    if not run_backup():
        return EXIT_CYCLE_FAILED

    if not lease.still_held():
        logger.error("Lease expired or was taken over during processing; not uploading.")
        return EXIT_CYCLE_FAILED

    result = provider.upload_directory(local_dir, prefix)
    if result.failed or result.errors:
        for err in result.errors[:5]:
            logger.error(f"  - {err}")
        return EXIT_CYCLE_FAILED
    return 0


class _Terminated(BaseException):
    """Raised on SIGTERM so cleanup runs; BaseException so `except Exception` cannot swallow it."""


def _raise_terminated(signum, frame) -> NoReturn:
    raise _Terminated()


def _has_files(path: str) -> bool:
    return any(files for _root, _dirs, files in os.walk(path))


def _clear_dir(path: str) -> None:
    """Empty the save directory this cycle filled (it was empty when the cycle started)."""
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.remove(full)


def _run_cycle_with_lease(args: argparse.Namespace, run_backup: Callable[[], bool]) -> int:
    """Check the run may start, take the lease, sync, then release and clean up; returns the exit code."""
    if not coordination_enabled():
        logger.error("--cycle needs STORAGE_COORDINATION=true.")
        return EXIT_CYCLE_FAILED
    config = load_storage_config()
    problems = _coordination_problems(config)
    local_dir = load_local_dir()
    if _has_files(local_dir):
        # Leftover local files may be older than S3 and would be uploaded over newer copies.
        problems.append(f"the save directory '{local_dir}' must be empty; use a separate "
                        "folder (e.g. storage_utils.py --download) for a local copy")
    if problems:
        logger.error("Coordinated run refused:")
        for problem in problems:
            logger.error(f"  - {problem}")
        return EXIT_CYCLE_FAILED

    provider = get_storage_provider(config)
    if not isinstance(provider, S3StorageProvider):
        raise TypeError("coordinated runs need the S3 provider")
    provider.connect()
    provider.require_log_snapshot = True
    prefix = get_remote_directory(StorageProvider.S3, config)
    lease = provider.lease(prefix, args.runner or _default_runner())

    if not lease.acquire():
        return EXIT_CYCLE_SKIPPED
    code = EXIT_CYCLE_FAILED
    try:
        code = _sync_cycle(provider, lease, prefix, local_dir, args.require_stale, run_backup)
    finally:
        # Release even on errors or SIGTERM; only a successful run refreshes last_success.
        # A SIGTERM from here to exit must not interrupt the release or the cleanup, nor
        # turn a finished run into a failed one, so the handler is never put back.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            released = lease.release(success=code == 0)
        except Exception as exc:
            logger.error(f"Lease release failed: {type(exc).__name__}: {exc}")
            released = False
        if released:
            logger.info("Lease released.")
        else:
            logger.warning("Lease not released; unless another runner took it over, it stays held until it expires.")
        if code == 0 and not released:
            logger.error("The lease was not released cleanly, so this run is not reported as successful.")
            code = EXIT_CYCLE_FAILED
        if code != EXIT_CYCLE_SKIPPED:
            _ping_healthcheck(code == 0)
        _clear_dir(local_dir)
    return code


def cmd_cycle(args: argparse.Namespace, run_backup: Callable[[], bool]) -> NoReturn:
    """Lease -> download log -> back up -> upload -> release, in one process."""
    signal.signal(signal.SIGTERM, _raise_terminated)
    try:
        code = _run_cycle_with_lease(args, run_backup)
    except _Terminated:
        logger.error("Terminated (SIGTERM).")
        code = EXIT_CYCLE_FAILED
    except Exception as exc:
        logger.error(f"Coordinated run failed: {type(exc).__name__}: {exc}")
        code = EXIT_CYCLE_FAILED
    sys.exit(code)
