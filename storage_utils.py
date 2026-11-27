from __future__ import annotations

import argparse
import configparser
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

from utils.config_paths import get_settings_file_path
from utils.file_path_validate import validate_and_set_directory
from utils.storage.factory import get_storage_provider, load_storage_config


LOG_NAME = "file_log.json"


def _load_parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    settings_path = get_settings_file_path()
    if not parser.read(settings_path):
        raise FileNotFoundError(f"Storage settings file not found: {settings_path}")
    return parser


def _load_local_dir(*, create: bool = False, writable: bool = False) -> str:
    """Resolve the configured archive path without falling back to another folder."""
    parser = _load_parser()
    raw_path = (
        parser.get("Settings", "save_directory", fallback=None)
        or parser.get("Storage", "save_directory", fallback=None)
        or "reddit_dropbox/"
    )
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()

    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    elif not candidate.is_dir():
        raise FileNotFoundError(
            f"Configured archive directory does not exist: {candidate}. "
            "Refusing to upload from an empty fallback directory."
        )
    if not candidate.is_dir():
        raise NotADirectoryError(f"Configured archive path is not a directory: {candidate}")
    required = os.R_OK | os.X_OK
    if create or writable:
        required |= os.W_OK
    if not os.access(candidate, required):
        mode = "readable and writable" if create or writable else "readable"
        raise PermissionError(f"Configured archive directory is not {mode}: {candidate}")
    return str(candidate)


def _load_check_type() -> str:
    parser = _load_parser()
    return (
        parser.get("Settings", "check_type", fallback=None)
        or parser.get("Storage", "check_type", fallback=None)
        or "DIR"
    ).upper()


def _remote_root_for(provider_name: str) -> str:
    config = load_storage_config()
    if provider_name.lower() in {"dropbox", "mega"}:
        return config.storage_root
    return config.storage_root.lstrip("/")


def _provider_from_name(name: str):
    name = name.lower().strip()
    config = load_storage_config()
    if name == "dropbox":
        from utils.storage.dropbox_provider import DropboxStorageProvider
        return DropboxStorageProvider(dropbox_directory=config.storage_root)
    if name == "s3":
        if not config.s3_bucket:
            raise ValueError("S3 bucket is not configured.")
        from utils.storage.s3_provider import S3StorageProvider
        return S3StorageProvider(
            bucket=config.s3_bucket,
            region=config.s3_region,
            storage_class=config.s3_storage_class,
            endpoint_url=config.s3_endpoint_url,
        )
    if name == "mega":
        from utils.storage.mega_provider import MegaStorageProvider
        return MegaStorageProvider()
    raise ValueError(f"Unknown provider '{name}'.")


def _connect(provider) -> None:
    if hasattr(provider, "connect") and callable(provider.connect):
        provider.connect()


def _summary(result) -> str:
    if hasattr(result, "summary") and callable(result.summary):
        return result.summary()
    if isinstance(result, dict):
        return str(result)
    return "done"


def _errors(result) -> list[str]:
    errs = getattr(result, "errors", None)
    if errs is None and isinstance(result, dict):
        errs = result.get("errors", [])
    return list(errs or [])


def _merge_results(a, b):
    from utils.storage.base import SyncResult
    return SyncResult(
        uploaded=getattr(a, "uploaded", 0) + getattr(b, "uploaded", 0),
        downloaded=getattr(a, "downloaded", 0) + getattr(b, "downloaded", 0),
        skipped=getattr(a, "skipped", 0) + getattr(b, "skipped", 0),
        failed=getattr(a, "failed", 0) + getattr(b, "failed", 0),
        bytes_transferred=getattr(a, "bytes_transferred", 0) + getattr(b, "bytes_transferred", 0),
        elapsed_seconds=max(getattr(a, "elapsed_seconds", 0), getattr(b, "elapsed_seconds", 0)),
        errors=[*_errors(a), *_errors(b)],
    )


def _move_log_out_of_tree(local_dir: str) -> str | None:
    """Temporarily remove file_log.json from provider directory scans."""
    log_path = os.path.join(local_dir, LOG_NAME)
    if not os.path.isfile(log_path):
        return None
    parent = os.path.dirname(os.path.abspath(local_dir))
    pending_path = os.path.join(parent, f".{LOG_NAME}.pending-{uuid.uuid4().hex}")
    shutil.move(log_path, pending_path)
    return pending_path


def _commit_log(provider, pending_path: str | None, remote_dir: str) -> object:
    if not pending_path:
        from utils.storage.base import SyncResult
        return SyncResult()
    remote_log = f"{remote_dir.rstrip('/')}/{LOG_NAME}" if remote_dir else LOG_NAME
    return provider.upload_file(pending_path, remote_log)


def _restore_pending_log(local_dir: str, pending_path: str | None) -> None:
    if not pending_path:
        return
    target = os.path.join(local_dir, LOG_NAME)
    if os.path.exists(pending_path):
        os.replace(pending_path, target)


def cmd_download(_: argparse.Namespace) -> int:
    config = load_storage_config()
    provider = get_storage_provider(config)
    if provider is None:
        print("No storage provider configured.")
        return 1
    _connect(provider)
    local_dir = _load_local_dir(create=True, writable=True)
    check_type = _load_check_type()
    remote_dir = _remote_root_for(config.provider.value)

    print(f"Downloading from {provider.get_provider_name()} using {check_type} mode...")
    result = provider.download_directory(remote_dir, local_dir, check_type=check_type)
    errs = _errors(result)
    if errs:
        print(f"\n{len(errs)} error(s) occurred:")
        for err in errs[:5]:
            print(f" - {err}")
        if len(errs) > 5:
            print(f" - ... and {len(errs) - 5} more")
        return 1
    print(f"Download complete: {_summary(result)}")
    return 0


def cmd_upload(_: argparse.Namespace) -> int:
    config = load_storage_config()
    provider = get_storage_provider(config)
    if provider is None:
        print("No storage provider configured.")
        return 1

    _connect(provider)
    local_dir = _load_local_dir()
    check_type = _load_check_type()
    remote_dir = _remote_root_for(config.provider.value)
    name = provider.get_provider_name().upper()
    print(f"Uploading to {provider.get_provider_name()} using {check_type} mode...")

    # S3 already implements the correct last-file-log transaction internally.
    if name == "S3":
        result = provider.upload_directory(local_dir, remote_dir, check_type=check_type)
    else:
        pending_log = None
        try:
            pending_log = _move_log_out_of_tree(local_dir)
            result = provider.upload_directory(local_dir, remote_dir, check_type=check_type)
            errs = _errors(result)
            if errs:
                return _print_errors_and_fail(errs)

            log_result = _commit_log(provider, pending_log, remote_dir)
            result = _merge_results(result, log_result)
            pending_log = None
        except Exception as exc:
            print(f"Storage upload failed before commit: {exc}")
            return 1
        finally:
            if pending_log:
                _restore_pending_log(local_dir, pending_log)

    errs = _errors(result)
    if errs:
        return _print_errors_and_fail(errs)
    print(f"Upload complete: {_summary(result)}")
    return 0


def _print_errors_and_fail(errs: list[str]) -> int:
    print(f"\n{len(errs)} error(s) occurred:")
    for err in errs[:10]:
        print(f" - {err}")
    if len(errs) > 10:
        print(f" - ... and {len(errs) - 10} more")
    return 1


def cmd_migrate(args: argparse.Namespace) -> int:
    source_name = args.source.lower().strip()
    target_name = args.target.lower().strip()
    if source_name == target_name:
        print("Error: source and target providers must be different.")
        return 1

    try:
        source = _provider_from_name(source_name)
        target = _provider_from_name(target_name)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    _connect(source)
    _connect(target)
    source_dir = _remote_root_for(source_name)
    target_dir = _remote_root_for(target_name)

    try:
        from utils.storage.migration import StorageMigration
    except Exception:
        StorageMigration = None  # type: ignore[assignment]

    if StorageMigration is not None:
        migration = StorageMigration(
            source=source,
            target=target,
            source_directory=source_dir,
            target_directory=target_dir,
        )
        if args.execute:
            print("Executing migration...")
            result = migration.execute()
            errs = _errors(result)
            if errs:
                return _print_errors_and_fail(errs)
            print(f"Migration complete: {_summary(result)}")
            return 0
        plan = migration.dry_run()
        count = getattr(plan, "file_count", None)
        if count is None and isinstance(plan, dict):
            count = plan.get("file_count", 0)
        print("Dry-run migration plan:")
        print(f" Source: {source_name} -> {source_dir}")
        print(f" Target: {target_name} -> {target_dir}")
        print(f" Files: {count or 0}")
        print(
            f"To execute this migration, rerun with: --migrate --source {source_name} "
            f"--target {target_name} --execute"
        )
        return 0

    if not args.execute:
        print("Dry-run migration plan:")
        print(f" Source: {source_name} -> {source_dir}")
        print(f" Target: {target_name} -> {target_dir}")
        print(
            f"To execute this migration, rerun with: --migrate --source {source_name} "
            f"--target {target_name} --execute"
        )
        return 0

    with tempfile.TemporaryDirectory(prefix="reddit-stash-migration-") as tmpdir:
        temp_dir = Path(tmpdir) / "payload"
        temp_dir.mkdir(parents=True, exist_ok=True)
        print(f"Downloading from {source.get_provider_name()}...")
        src_result = source.download_directory(source_dir, str(temp_dir), check_type="DIR")
        if _errors(src_result):
            return _print_errors_and_fail(_errors(src_result)[:10])
        print(f"Uploading to {target.get_provider_name()}...")
        tgt_result = target.upload_directory(str(temp_dir), target_dir, check_type="DIR")
        if _errors(tgt_result):
            return _print_errors_and_fail(_errors(tgt_result)[:10])
    print("Migration complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reddit Stash storage management")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download", action="store_true", help="Download from configured storage")
    group.add_argument("--upload", action="store_true", help="Upload to configured storage")
    group.add_argument("--migrate", action="store_true", help="Migrate between providers")
    parser.add_argument("--source", choices=["dropbox", "s3", "mega"])
    parser.add_argument("--target", choices=["dropbox", "s3", "mega"])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.download:
        return cmd_download(args)
    if args.upload:
        return cmd_upload(args)
    if args.migrate:
        if not args.source or not args.target:
            parser.error("--migrate requires --source and --target")
        return cmd_migrate(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
