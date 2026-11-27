from __future__ import annotations

"""Reddit-Stash command-line entry point with fail-closed backup semantics."""

import configparser
import os
import tempfile
import time
from pathlib import Path

import praw

from utils.archive_index import build_file_log
from utils.config_paths import get_settings_file_path
from utils.config_validator import validate_configuration
from utils.env_config import load_config_and_env
from utils.feature_flags import get_feature_summary
from utils.file_operations import save_user_activity
from utils.gdpr_processor import process_gdpr_export
from utils.log_utils import load_file_log, save_file_log


def _load_runtime_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    settings_path = get_settings_file_path()
    read_files = config.read(settings_path)
    if not read_files:
        raise FileNotFoundError(f"Configuration file not found: {settings_path}")
    return config


def _build_reddit_client() -> praw.Reddit:
    client_id, client_secret, username, password = load_config_and_env()
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        password=password,
        user_agent=f"Reddit Stash by /u/{username}",
    )


def _write_success_heartbeat() -> None:
    """Atomically update the optional Docker health-check heartbeat."""
    heartbeat = os.getenv("REDDIT_STASH_HEARTBEAT_FILE")
    if not heartbeat:
        return
    destination = Path(heartbeat).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{time.time():.6f}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main() -> int:
    print("Validating configuration...")
    try:
        validation_result = validate_configuration()
        for warning in validation_result.get("warnings", []):
            print(f"WARNING: {warning}")
        print("Configuration validated successfully")
        print(get_feature_summary())

        config = _load_runtime_config()
        unsave_setting = config.getboolean(
            "Settings", "unsave_after_download", fallback=False
        )
        if unsave_setting:
            raise ValueError(
                "Settings.unsave_after_download=true is not safe with the current "
                "architecture: cloud upload happens after scraping and is not verified "
                "before Reddit unsaving. Set it to false."
            )

        configured_directory = Path(
            config.get("Settings", "save_directory", fallback="reddit_dropbox/")
        ).expanduser()
        if not configured_directory.is_absolute():
            configured_directory = Path.cwd() / configured_directory
        configured_directory = configured_directory.resolve()
        configured_directory.mkdir(parents=True, exist_ok=True)
        if not configured_directory.is_dir() or not os.access(
            configured_directory, os.R_OK | os.W_OK | os.X_OK
        ):
            raise PermissionError(
                f"Archive directory is not readable/writable: {configured_directory}. "
                "Refusing to fall back to an unmounted directory."
            )
        save_directory = str(configured_directory)
        process_api = config.getboolean("Settings", "process_api", fallback=True)
        process_gdpr = config.getboolean("Settings", "process_gdpr", fallback=False)
        check_type = config.get("Settings", "check_type", fallback="DIR").upper()

        if not process_api and not process_gdpr:
            print("Both process_api and process_gdpr are disabled; nothing to do.")
            return 0

        reddit = None
        if process_api:
            reddit = _build_reddit_client()
        elif process_gdpr:
            try:
                reddit = _build_reddit_client()
            except Exception as exc:
                print(
                    "Reddit API credentials unavailable; GDPR export will run in "
                    f"CSV-only mode. Details: {exc}"
                )

        # Corrupt logs always stop the run. A missing log with an existing archive
        # also stops here and tells the operator to rebuild it explicitly. This
        # avoids silently treating a populated archive as new/empty.
        file_log = load_file_log(save_directory)

        total_processed = 0
        total_skipped = 0
        total_failed = 0
        total_markdown_size = 0
        total_media_size = 0

        if process_api:
            if reddit is None:
                raise RuntimeError("Reddit API client was not initialized")
            print("Processing items from Reddit API...")
            api_stats = save_user_activity(
                reddit, save_directory, file_log, unsave=False
            )
            if len(api_stats) != 5:
                raise RuntimeError(
                    "save_user_activity returned an unexpected result. "
                    "Apply the updated utils/file_operations.py interface patch."
                )
            processed, skipped, failed, markdown_size, media_size = api_stats
            total_processed += processed
            total_skipped += skipped
            total_failed += failed
            total_markdown_size += markdown_size
            total_media_size += media_size

        if process_gdpr:
            print("\nProcessing GDPR export data...")
            gdpr_existing_files = set(file_log.keys()) | set(
                build_file_log(save_directory).keys()
            )
            gdpr_stats = process_gdpr_export(
                reddit,
                save_directory,
                gdpr_existing_files,
                set(),
                file_log,
            )
            if not isinstance(gdpr_stats, (tuple, list)) or len(gdpr_stats) != 4:
                raise RuntimeError(
                    "process_gdpr_export returned an unexpected result. Apply the "
                    "updated utils/gdpr_processor.py patch; expected four values "
                    "(processed, skipped, failed, markdown_size)."
                )
            processed, skipped, failed, markdown_size = gdpr_stats
            total_processed += int(processed or 0)
            total_skipped += int(skipped or 0)
            total_failed += int(failed or 0)
            total_markdown_size += int(markdown_size or 0)

        # Persist the latest in-memory index even when some individual items
        # failed. The process exits non-zero below so CI/schedulers report failure.
        save_file_log(file_log, save_directory)

        print(
            f"\nRun completed: {total_processed} saved/processed, "
            f"{total_skipped} skipped, {total_failed} failed."
        )
        print(f"Markdown storage: {total_markdown_size / (1024 * 1024):.2f} MiB")
        print(f"Media storage: {total_media_size / (1024 * 1024):.2f} MiB")
        print(
            "Total reported storage: "
            f"{(total_markdown_size + total_media_size) / (1024 * 1024):.2f} MiB"
        )

        if total_failed:
            print(
                "One or more Reddit items failed to archive. The run is marked "
                "failed so the scheduler/CI can retry or alert."
            )
            return 1

        _write_success_heartbeat()
        return 0

    except Exception as exc:
        print(f"FATAL: Reddit-Stash run failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
