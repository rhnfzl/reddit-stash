"""
Unified storage CLI for Reddit Stash.

Supports Dropbox and S3 backends through a single interface.
Replaces dropbox_utils.py as the primary storage entry point.

Usage:
    python storage_utils.py --download
    python storage_utils.py --upload
    python storage_utils.py --migrate --source dropbox --target s3          # dry-run
    python storage_utils.py --migrate --source dropbox --target s3 --execute  # actual
    python storage_utils.py --cycle [--runner NAME] [--require-stale HOURS]

--cycle (needs STORAGE_COORDINATION=true, AWS S3) takes an S3 lease, downloads
file_log.json, runs reddit_stash, uploads, and releases the lease, so several
runners (e.g. a homelab plus GitHub Actions) never sync at the same time.
Exit codes: 0 success, 2 failure, 3 skipped (lease held, lost race, or fresh).
"""

import argparse
import configparser
import os
import socket
import sys
from datetime import datetime, timedelta, timezone

from utils.file_path_validate import validate_and_set_directory
from utils.storage.base import StorageProvider
from utils.storage.factory import load_storage_config, get_storage_provider


def _load_local_dir() -> str:
    """Load and validate the local save directory from settings.ini."""
    parser = configparser.ConfigParser()
    parser.read("settings.ini")
    local_dir = parser.get("Settings", "save_directory", fallback="reddit/")
    return validate_and_set_directory(local_dir)


def _load_check_type() -> str:
    """Load the check_type setting from settings.ini."""
    parser = configparser.ConfigParser()
    parser.read("settings.ini")
    return parser.get("Settings", "check_type", fallback="DIR").upper()


def _get_provider_for_name(name: str):
    """Create a provider instance for the given provider name string."""
    config = load_storage_config()

    if name == "dropbox":
        from utils.storage.dropbox_provider import DropboxStorageProvider
        return DropboxStorageProvider(dropbox_directory=config.dropbox_directory)
    elif name == "s3":
        if not config.s3_bucket:
            print("Error: S3 bucket not configured. Set AWS_S3_BUCKET or s3_bucket in settings.ini.")
            sys.exit(1)
        from utils.storage.s3_provider import S3StorageProvider
        return S3StorageProvider(
            bucket=config.s3_bucket,
            region=config.s3_region,
            storage_class=config.s3_storage_class,
            endpoint_url=config.s3_endpoint_url,
        )
    else:
        print(f"Error: Unknown provider '{name}'. Must be 'dropbox' or 's3'.")
        sys.exit(1)


def _get_remote_directory(provider_name: str) -> str:
    """Return the remote directory path for the given provider."""
    config = load_storage_config()
    if provider_name == "dropbox":
        return config.dropbox_directory
    # S3 uses the bucket root or a prefix — use dropbox_directory as the key prefix
    # to maintain path consistency across providers
    return config.dropbox_directory.lstrip("/")


def _exit_on_failure(result, max_shown: int = 5) -> None:
    """Print transfer errors and exit non-zero so callers never treat a failed sync as done."""
    if result.errors:
        print(f"\n{len(result.errors)} error(s) occurred:")
        for err in result.errors[:max_shown]:
            print(f"  - {err}")
    if result.failed or result.errors:
        sys.exit(1)


def cmd_download(args):
    """Download files from the configured storage provider."""
    config = load_storage_config()
    provider = get_storage_provider(config)

    if provider is None:
        print("No storage provider configured. Set provider in [Storage] section of settings.ini.")
        sys.exit(1)

    provider.connect()

    local_dir = _load_local_dir()
    check_type = _load_check_type()

    remote_dir = config.dropbox_directory
    if config.provider == StorageProvider.S3:
        remote_dir = config.dropbox_directory.lstrip("/")

    if check_type == "LOG":
        print(f"Downloading log file from {provider.get_provider_name()}...")
    else:
        print(f"Downloading directory from {provider.get_provider_name()}...")

    result = provider.download_directory(remote_dir, local_dir, check_type=check_type)
    _exit_on_failure(result)


EXIT_CYCLE_FAILED = 2
EXIT_CYCLE_SKIPPED = 3


def _coordination_enabled() -> bool:
    return os.environ.get("STORAGE_COORDINATION", "").strip().lower() in ("1", "true", "yes")


def _refuse_when_coordinated(command: str) -> None:
    """Uncoordinated writes could overwrite a runner that holds the lease."""
    if _coordination_enabled():
        print(f"Refusing {command}: STORAGE_COORDINATION is on, so only --cycle may write to storage.")
        sys.exit(EXIT_CYCLE_FAILED)


def _coordination_problems(config) -> list:
    """Settings that coordinated runs do not support (yet), as readable messages."""
    problems = []
    if config.provider != StorageProvider.S3:
        problems.append("coordination needs provider = s3")
    if config.s3_endpoint_url and "amazonaws.com" not in config.s3_endpoint_url:
        problems.append("coordination is only verified on AWS S3, not custom endpoints")
    if not config.dropbox_directory.strip("/"):
        problems.append("coordination needs a non-empty dropbox_directory (the S3 key prefix)")
    parser = configparser.ConfigParser()
    parser.read("settings.ini")
    if parser.getboolean("Settings", "unsave_after_download", fallback=False):
        problems.append("unsave_after_download = true can lose items if an upload fails")
    if parser.getboolean("Settings", "process_gdpr", fallback=False):
        problems.append("process_gdpr = true needs the CSV export on every runner")
    if _load_check_type() != "LOG":
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
        import requests
        requests.get(url if success else f"{url.rstrip('/')}/fail", timeout=10)
    except Exception as exc:
        # Only the type: the message would contain the ping URL, which is a secret.
        print(f"Healthcheck ping failed: {type(exc).__name__}")


def _run_backup() -> bool:
    """Run reddit_stash in this process; True when it finished without error."""
    import reddit_stash
    try:
        reddit_stash.main()
        return True
    except SystemExit as exc:
        return exc.code in (0, None)
    except Exception as exc:
        print(f"Backup run failed: {exc}")
        return False


def _sync_cycle(provider, lease, prefix: str, require_stale) -> int:
    """Body of --cycle while holding the lease; returns the exit code."""
    if require_stale is not None:
        last = lease.last_success(lease.body)
        if last and datetime.now(timezone.utc) - last < timedelta(hours=require_stale):
            print(f"Last successful run was at {last:%Y-%m-%d %H:%M} UTC, within "
                  f"{require_stale}h; nothing to do.")
            return EXIT_CYCLE_SKIPPED

    local_dir = _load_local_dir()
    result = provider.download_directory(prefix, local_dir, check_type="LOG")
    if result.failed or result.errors:
        print(f"Download failed: {result.errors[:3]}")
        return EXIT_CYCLE_FAILED

    if not _run_backup():
        return EXIT_CYCLE_FAILED

    if not lease.still_held():
        print("Lease expired or was taken over during processing; not uploading.")
        return EXIT_CYCLE_FAILED

    result = provider.upload_directory(local_dir, prefix)
    if result.failed or result.errors:
        for err in result.errors[:5]:
            print(f"  - {err}")
        return EXIT_CYCLE_FAILED
    return 0


def cmd_cycle(args):
    """Lease -> download log -> back up -> upload -> release, in one process."""
    if not _coordination_enabled():
        print("--cycle needs STORAGE_COORDINATION=true.")
        sys.exit(EXIT_CYCLE_FAILED)
    config = load_storage_config()
    problems = _coordination_problems(config)
    if problems:
        print("Coordinated run refused:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(EXIT_CYCLE_FAILED)

    provider = get_storage_provider(config)
    provider.connect()
    provider.require_log_snapshot = True
    prefix = config.dropbox_directory.strip("/")
    lease = provider.lease(prefix, args.runner or _default_runner())

    if not lease.acquire():
        sys.exit(EXIT_CYCLE_SKIPPED)
    code = EXIT_CYCLE_FAILED
    try:
        code = _sync_cycle(provider, lease, prefix, args.require_stale)
    finally:
        # Release even on errors; only a successful run refreshes last_success.
        lease.release(success=code == 0)
        if code != EXIT_CYCLE_SKIPPED:
            _ping_healthcheck(code == 0)
    sys.exit(code)


def cmd_upload(args):
    """Upload files to the configured storage provider."""
    _refuse_when_coordinated("--upload")
    config = load_storage_config()
    provider = get_storage_provider(config)

    if provider is None:
        print("No storage provider configured. Set provider in [Storage] section of settings.ini.")
        sys.exit(1)

    provider.connect()

    local_dir = _load_local_dir()

    remote_dir = config.dropbox_directory
    if config.provider == StorageProvider.S3:
        remote_dir = config.dropbox_directory.lstrip("/")

    print(f"Uploading to {provider.get_provider_name()}...")
    result = provider.upload_directory(local_dir, remote_dir)
    _exit_on_failure(result)


def cmd_migrate(args):
    """Migrate files between storage providers."""
    if args.execute:
        _refuse_when_coordinated("--migrate --execute")
    from utils.storage.migration import StorageMigration

    source = _get_provider_for_name(args.source)
    target = _get_provider_for_name(args.target)

    if args.source == args.target:
        print("Error: Source and target providers must be different.")
        sys.exit(1)

    print(f"Connecting to {source.get_provider_name()}...")
    source.connect()
    print(f"Connecting to {target.get_provider_name()}...")
    target.connect()

    source_dir = _get_remote_directory(args.source)
    target_dir = _get_remote_directory(args.target)

    migration = StorageMigration(
        source=source,
        target=target,
        source_directory=source_dir,
        target_directory=target_dir,
    )

    if args.execute:
        print("\nExecuting migration...")
        result = migration.execute()
        _exit_on_failure(result, max_shown=10)
    else:
        plan = migration.dry_run()
        if plan.file_count > 0:
            print("\nTo execute this migration, add --execute:")
            print(f"  python storage_utils.py --migrate --source {args.source} --target {args.target} --execute")
        else:
            print("\nNo files to migrate.")


def main():
    parser = argparse.ArgumentParser(
        description="Reddit Stash storage management (Dropbox, S3)",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download", action="store_true", help="Download from storage provider")
    group.add_argument("--upload", action="store_true", help="Upload to storage provider")
    group.add_argument("--migrate", action="store_true", help="Migrate between providers")
    group.add_argument("--cycle", action="store_true",
                       help="Coordinated run: lease, download, back up, upload, release (S3)")

    # Migration-specific arguments
    parser.add_argument("--source", choices=["dropbox", "s3"], help="Migration source provider")
    parser.add_argument("--target", choices=["dropbox", "s3"], help="Migration target provider")
    parser.add_argument("--execute", action="store_true", help="Execute migration (default is dry-run)")

    # Cycle-specific arguments
    parser.add_argument("--runner", help="Name recorded in the lease (default: GitHub run or hostname)")
    parser.add_argument("--require-stale", type=float, metavar="HOURS",
                        help="Skip unless the last successful run is older than HOURS")

    args = parser.parse_args()

    if args.migrate:
        if not args.source or not args.target:
            parser.error("--migrate requires --source and --target")
        cmd_migrate(args)
    elif args.download:
        cmd_download(args)
    elif args.upload:
        cmd_upload(args)
    elif args.cycle:
        cmd_cycle(args)


if __name__ == "__main__":
    main()
