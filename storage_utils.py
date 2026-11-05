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

--cycle (needs STORAGE_COORDINATION=true, AWS S3) runs reddit_stash under an S3
lease; see utils/storage/cycle.py.
Exit codes: 0 success, 2 failure, 3 skipped (lease held, lost race, or fresh).
"""

import argparse
import sys
from typing import NoReturn

from utils.log_utils import configure_logging
from utils.storage import cycle
from utils.storage.base import StorageProvider, StorageProviderProtocol, SyncResult
from utils.storage.factory import (
    StorageConfig,
    configured_local_dir,
    create_provider,
    get_remote_directory,
    get_storage_provider,
    load_local_dir,
    load_storage_config,
    load_sync_check_type,
)
from utils.storage.migration import StorageMigration

PROVIDER_NAMES = [p.value for p in StorageProvider if p != StorageProvider.NONE]


def _connected_provider(config: StorageConfig) -> StorageProviderProtocol:
    """The configured provider, connected; exits when none is configured."""
    provider = get_storage_provider(config)
    if provider is None:
        print("No storage provider configured. Set provider in [Storage] section of settings.ini.")
        sys.exit(1)
    provider.connect()
    return provider


def _exit_on_failure(result: SyncResult, max_shown: int = 5) -> None:
    """Print transfer errors and exit non-zero so callers never treat a failed sync as done."""
    if result.errors:
        print(f"\n{len(result.errors)} error(s) occurred:")
        for err in result.errors[:max_shown]:
            print(f"  - {err}")
    if result.failed or result.errors:
        sys.exit(1)


def cmd_download(args: argparse.Namespace) -> None:
    """Download files from the configured storage provider."""
    config = load_storage_config()
    provider = _connected_provider(config)

    local_dir = load_local_dir()
    remote_dir = get_remote_directory(config.provider, config)

    if load_sync_check_type() == "LOG":
        print(f"Downloading log file from {provider.get_provider_name()}...")
        result = provider.download_log(remote_dir, local_dir)
    else:
        print(f"Downloading directory from {provider.get_provider_name()}...")
        result = provider.download_directory(remote_dir, local_dir)
    _exit_on_failure(result)


def _refuse_when_coordinated(command: str) -> None:
    """Uncoordinated writes could overwrite a runner that holds the lease."""
    if cycle.coordination_enabled():
        print(f"Refusing {command}: STORAGE_COORDINATION is on, so only --cycle may write to storage.")
        sys.exit(cycle.EXIT_CYCLE_FAILED)


def _run_backup() -> bool:
    """Run reddit_stash in this process; True when it finished without error."""
    import reddit_stash  # only --cycle needs praw and the whole backup pipeline
    try:
        reddit_stash.main()
        return True
    except SystemExit as exc:
        return exc.code in (0, None)
    except Exception as exc:
        print(f"Backup run failed: {exc}")
        return False


def cmd_cycle(args: argparse.Namespace) -> NoReturn:
    """Coordinated run; see utils/storage/cycle.py."""
    cycle.cmd_cycle(args, _run_backup)


def cmd_upload(args: argparse.Namespace) -> None:
    """Upload files to the configured storage provider."""
    _refuse_when_coordinated("--upload")
    config = load_storage_config()
    provider = _connected_provider(config)

    # Upload only reads, so a read-only save directory is fine.
    local_dir = configured_local_dir()
    remote_dir = get_remote_directory(config.provider, config)

    print(f"Uploading to {provider.get_provider_name()}...")
    result = provider.upload_directory(local_dir, remote_dir)
    _exit_on_failure(result)


def cmd_migrate(args: argparse.Namespace) -> None:
    """Migrate files between storage providers."""
    if args.execute:
        _refuse_when_coordinated("--migrate --execute")

    if args.source == args.target:
        print("Error: Source and target providers must be different.")
        sys.exit(1)

    config = load_storage_config()
    source_kind = StorageProvider(args.source)
    target_kind = StorageProvider(args.target)
    try:
        source = create_provider(source_kind, config)
        target = create_provider(target_kind, config)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print(f"Connecting to {source.get_provider_name()}...")
    source.connect()
    print(f"Connecting to {target.get_provider_name()}...")
    target.connect()

    migration = StorageMigration(
        source=source,
        target=target,
        source_directory=get_remote_directory(source_kind, config),
        target_directory=get_remote_directory(target_kind, config),
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


def main() -> None:
    configure_logging()
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
    parser.add_argument("--source", choices=PROVIDER_NAMES, help="Migration source provider")
    parser.add_argument("--target", choices=PROVIDER_NAMES, help="Migration target provider")
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
