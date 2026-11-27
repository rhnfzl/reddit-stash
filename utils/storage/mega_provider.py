from __future__ import annotations

"""Reliable MEGA storage backend for Reddit Stash.

MEGA (through rclone) does not expose hashes or modification times and can
contain duplicate files with the same path.  Therefore this provider does
not rely on rclone's normal destination comparison for archive correctness.
Instead it keeps a small remote content manifest and replaces files through a
staging path so a failed upload cannot create a new committed file_log.json.
"""

import configparser
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from utils.config_paths import get_settings_file_path
from utils.storage.content_hash import compute_file_hash
from .base import StorageFileInfo, StorageProviderProtocol, SyncResult


class MegaStorageProvider(StorageProviderProtocol):
    """MEGA implementation backed by rclone with archive-safe replacement."""

    LEGACY_LOG_NAME = "log.json"
    CANONICAL_LOG_NAME = "file_log.json"
    MANIFEST_NAME = "media_manifest.json"
    STAGING_DIR = ".reddit-stash-staging"

    def __init__(self, mega_remote: Optional[str] = None):
        configured_remote = mega_remote or os.getenv("MEGA_REMOTE") or "mega"
        configured_remote = configured_remote.strip()
        if not configured_remote or any(char in configured_remote for char in ":/\\"):
            raise ValueError("MEGA_REMOTE must be a non-empty rclone remote name without path separators")
        self._mega_remote = configured_remote
        self._connected = False

    # ------------------------------------------------------------------
    # rclone / path helpers
    # ------------------------------------------------------------------
    def _require_rclone(self) -> None:
        if shutil.which("rclone") is None:
            raise RuntimeError(
                "rclone is not installed. Install rclone before using the MEGA provider."
            )

    def _remote_prefix(self, remote_directory: str = "") -> str:
        directory = (remote_directory or "").strip().strip("/")
        return f"{self._mega_remote}:{directory}" if directory else f"{self._mega_remote}:"

    def _remote_join(self, remote_directory: str, filename: str) -> str:
        prefix = self._remote_prefix(remote_directory)
        return f"{prefix}{filename}" if prefix.endswith(":") else f"{prefix}/{filename}"

    @staticmethod
    def _norm_rel(path: str) -> str:
        return str(path).replace(os.sep, "/").strip("/")

    def _run(self, args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        self._require_rclone()
        proc = subprocess.run(
            ["rclone", *args],
            text=True,
            capture_output=True,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "rclone command failed")
        return proc

    def _ensure_remote_configured(self) -> None:
        result = self._run(["listremotes"])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"Could not inspect configured rclone remotes: {detail or 'unknown error'}"
            )
        if f"{self._mega_remote}:" in result.stdout:
            return

        email = os.getenv("MEGA_EMAIL")
        password = os.getenv("MEGA_PASSWORD")
        twofa = os.getenv("MEGA_2FA")
        if not email or not password:
            raise RuntimeError(
                "MEGA remote is not configured. Set MEGA_EMAIL and MEGA_PASSWORD, "
                "or create an rclone remote named 'mega'."
            )

        args = [
            "config", "create", self._mega_remote, "mega",
            "user", email, "pass", password,
            "--non-interactive",
        ]
        if twofa:
            args.extend(["2fa", twofa])
        self._run(args, check=True)

    def connect(self) -> None:
        self._ensure_remote_configured()
        self._connected = True

    def get_provider_name(self) -> str:
        return "MEGA"

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------
    def _load_remote_manifest(self, remote_directory: str) -> Dict[str, dict]:
        remote_path = self._remote_join(remote_directory, self.MANIFEST_NAME)
        with tempfile.TemporaryDirectory(prefix="reddit-stash-mega-manifest-") as tmp:
            local = os.path.join(tmp, self.MANIFEST_NAME)
            proc = self._run(["copyto", remote_path, local])
            if proc.returncode != 0:
                text = (proc.stderr or proc.stdout or "").lower()
                if "not found" in text or "does not exist" in text:
                    return {}
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "Failed to read MEGA manifest")
            try:
                with open(local, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Invalid MEGA {self.MANIFEST_NAME}: {exc}") from exc
        files = payload.get("files", {}) if isinstance(payload, dict) else {}
        return files if isinstance(files, dict) else {}

    def _write_manifest(self, local_directory: str) -> str:
        payload = {
            "version": 1,
            "algorithm": "blake3-or-sha256",
            "files": {},
        }
        root = Path(local_directory)
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                # Dropbox and S3 collectors also ignore dotfiles. Keep the MEGA
                # manifest aligned with what is actually transferred (including
                # the local media_usage.sqlite3 quota database).
                continue
            rel = self._norm_rel(str(path.relative_to(root)))
            if rel in {self.CANONICAL_LOG_NAME, self.LEGACY_LOG_NAME, self.MANIFEST_NAME}:
                continue
            if rel.startswith(self.STAGING_DIR + "/"):
                continue
            payload["files"][rel] = {
                "hash": compute_file_hash(str(path)),
                "size": path.stat().st_size,
            }

        fd, temp_path = tempfile.mkstemp(prefix="reddit-stash-manifest-", suffix=".json")
        os.close(fd)
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        return temp_path

    # ------------------------------------------------------------------
    # Remote listing / duplicate-safe replacement
    # ------------------------------------------------------------------
    def list_files(self, remote_directory: str) -> List[StorageFileInfo]:
        """List files; return [] only for a confirmed missing remote directory."""
        remote_spec = self._remote_prefix(remote_directory)
        proc = self._run(["lsjson", remote_spec, "--recursive"])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            lowered = detail.lower()
            missing_markers = (
                "directory not found", "path not found", "object not found",
                "directory does not exist", "path does not exist", "doesn't exist",
            )
            if any(marker in lowered for marker in missing_markers):
                return []
            raise RuntimeError(
                f"rclone lsjson failed for {remote_spec!r} (exit {proc.returncode}): "
                f"{detail or 'no error text returned'}"
            )
        try:
            entries = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"rclone returned invalid JSON for {remote_spec!r}: {exc}") from exc
        if not isinstance(entries, list):
            raise RuntimeError(
                f"rclone lsjson returned {type(entries).__name__}, expected a JSON list"
            )

        prefix = self._norm_rel(remote_directory)
        files: List[StorageFileInfo] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise RuntimeError(f"Invalid rclone listing entry {index}: expected an object")
            if entry.get("IsDir"):
                continue
            rel_path = self._norm_rel(entry.get("Path") or entry.get("Name") or "")
            if not rel_path:
                continue
            try:
                size = int(entry.get("Size") or 0)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid size in rclone listing entry {rel_path!r}") from exc
            remote_path = f"{prefix}/{rel_path}" if prefix else rel_path
            files.append(StorageFileInfo(
                remote_path=remote_path,
                content_hash=None,
                size_bytes=size,
                last_modified=entry.get("ModTime"),
            ))
        return files

    def _remote_matches(self, remote_directory: str, relative_path: str) -> List[StorageFileInfo]:
        """Return all remote nodes matching one exact relative path under a directory."""
        prefix = self._norm_rel(remote_directory)
        target = self._norm_rel(relative_path)
        expected = f"{prefix}/{target}" if prefix else target
        return [
            item for item in self.list_files(remote_directory)
            if self._norm_rel(item.remote_path) == expected
        ]

    def _delete_known_matches(self, remote_directory: str, relative_path: str, count: int) -> None:
        """Delete a known number of duplicate MEGA nodes without rescanning the remote."""
        if count <= 0:
            return
        target = self._remote_join(remote_directory, self._norm_rel(relative_path))
        for _ in range(count):
            proc = self._run(["deletefile", target])
            if proc.returncode != 0:
                raise RuntimeError(
                    proc.stderr.strip() or proc.stdout.strip() or
                    f"Failed deleting MEGA {target}"
                )

    def _cleanup_staging(self, remote_directory: str) -> None:
        """Ensure staging exists without deleting files left by interrupted uploads.

        A staged object may be the only verified remote copy if a previous run
        deleted the old final-path object and then failed during ``moveto``.
        Deleting staging contents at the beginning of the next run would turn
        that recoverable interruption into permanent data loss.  Therefore
        this method intentionally preserves all staged files and only reports
        pending leftovers for operator review.
        """
        staging_directory = f"{self._norm_rel(remote_directory)}/{self.STAGING_DIR}".strip("/")
        self._run(["mkdir", self._remote_prefix(staging_directory)], check=True)
        pending = self.list_files(staging_directory)
        if pending:
            print(
                f"WARNING: MEGA staging contains {len(pending)} pending file(s). "
                "They are being preserved because they may be the only verified "
                "copy from an interrupted replacement. Inspect and verify final "
                "remote files before manually removing anything from "
                f"{self._remote_prefix(staging_directory)}."
            )

    def _stage_and_replace(
        self,
        local_path: str,
        remote_directory: str,
        relative_path: str,
        existing_count: int | None = None,
    ) -> int:
        """Upload to a unique staging path, then replace all final-path nodes."""
        rel = self._norm_rel(relative_path)
        root = self._norm_rel(remote_directory)
        token = uuid.uuid4().hex
        stage_rel = f"{self.STAGING_DIR}/{token}/{rel}"
        stage_target = self._remote_join(root, stage_rel)
        final_target = self._remote_join(root, rel)
        size = os.path.getsize(local_path)

        proc = self._run(["copyto", local_path, stage_target, "--progress"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"MEGA staging upload failed: {rel}")

        # rclone verifies the completed transfer before returning success.  The staged
        # copy is therefore safe to use as the replacement before old nodes are removed.
        if existing_count is None:
            existing_count = len(self._remote_matches(root, rel))

        # Delete all old copies only after the new copy exists remotely.
        self._delete_known_matches(root, rel, existing_count)

        proc = self._run(["moveto", stage_target, final_target, "--progress"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"MEGA final move failed: {rel}")

        # Do not rescan the whole remote after every transfer.  moveto success means the
        # staged object was moved to the requested destination; the next directory listing
        # will verify uniqueness and size when the provider runs again.
        return size

    def upload_file(self, local_path: str, remote_path: str) -> StorageFileInfo:
        self.connect()
        normalized = self._norm_rel(remote_path)
        remote_root = self._norm_rel(os.path.dirname(normalized))
        relative = os.path.basename(normalized)
        existing_count = len(self._remote_matches(remote_root, relative))
        size = self._stage_and_replace(local_path, remote_root, relative, existing_count=existing_count)
        return StorageFileInfo(remote_path=normalized, size_bytes=size)

    def download_file(self, remote_path: str, local_path: str) -> StorageFileInfo:
        self.connect()
        source = self._remote_join("", self._norm_rel(remote_path))
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        proc = self._run(["copyto", source, local_path, "--progress"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "MEGA download failed")
        size = os.path.getsize(local_path)
        return StorageFileInfo(remote_path=self._norm_rel(remote_path), size_bytes=size)

    def get_file_info(self, remote_path: str) -> Optional[StorageFileInfo]:
        normalized = self._norm_rel(remote_path)
        parent = os.path.dirname(normalized)
        basename = os.path.basename(normalized)
        for item in self.list_files(parent):
            if self._norm_rel(item.remote_path) == normalized:
                return item
        return None

    def file_exists(self, remote_path: str) -> bool:
        return self.get_file_info(remote_path) is not None

    # ------------------------------------------------------------------
    # LOG-mode helpers
    # ------------------------------------------------------------------
    def _read_bool_from_settings(self, section: str, key: str, fallback: bool = False) -> bool:
        parser = configparser.ConfigParser()
        parser.read(get_settings_file_path())
        if parser.has_option(section, key):
            try:
                return parser.getboolean(section, key)
            except Exception:
                pass
        return fallback

    def _process_gdpr_enabled(self) -> bool:
        return self._read_bool_from_settings("Settings", "process_gdpr", fallback=False)

    def _download_log_only(self, remote_directory: str, local_directory: str, start: float) -> SyncResult:
        os.makedirs(local_directory, exist_ok=True)
        local_file = os.path.join(local_directory, self.CANONICAL_LOG_NAME)
        for candidate in (self.CANONICAL_LOG_NAME, self.LEGACY_LOG_NAME):
            matches = self._remote_matches(remote_directory, candidate)
            if len(matches) > 1:
                return SyncResult(
                    failed=1,
                    elapsed_seconds=time.time() - start,
                    errors=[
                        f"MEGA contains {len(matches)} copies of {candidate} at "
                        f"{self._remote_join(remote_directory, candidate)}; clean duplicates before continuing."
                    ],
                )
            source = self._remote_join(remote_directory, candidate)
            proc = self._run(["copyto", source, local_file, "--progress"])
            if proc.returncode == 0:
                size = os.path.getsize(local_file)
                return SyncResult(downloaded=1, bytes_transferred=size, elapsed_seconds=time.time() - start)
            text = (proc.stderr or proc.stdout or "").lower()
            if "not found" in text or "does not exist" in text:
                continue
            return SyncResult(
                failed=1,
                elapsed_seconds=time.time() - start,
                errors=[proc.stderr.strip() or proc.stdout.strip() or "MEGA log download failed"],
            )
        print("No existing log file in MEGA — starting fresh.")
        return SyncResult(elapsed_seconds=time.time() - start)

    def _download_gdpr_data(self, remote_directory: str, local_directory: str) -> SyncResult:
        start = time.time()
        remote = f"{self._norm_rel(remote_directory)}/gdpr_data".strip("/")
        parent_entries = self.list_files(remote_directory)
        remote_prefix = self._norm_rel(remote)
        if not any(
            self._norm_rel(item.remote_path).startswith(remote_prefix + "/")
            for item in parent_entries
        ):
            return SyncResult(elapsed_seconds=time.time() - start)
        local = os.path.join(local_directory, "gdpr_data")
        os.makedirs(local, exist_ok=True)
        proc = self._run([
            "copy", self._remote_prefix(remote), local,
            "--transfers", "2", "--checkers", "4",
            "--low-level-retries", "20", "--retries", "10", "--progress",
        ])
        if proc.returncode != 0:
            return SyncResult(
                failed=1,
                elapsed_seconds=time.time() - start,
                errors=[proc.stderr.strip() or proc.stdout.strip() or "MEGA GDPR download failed"],
            )
        return SyncResult(downloaded=1, elapsed_seconds=time.time() - start)

    # ------------------------------------------------------------------
    # Directory upload / download
    # ------------------------------------------------------------------
    def _collect_local_files(self, local_directory: str) -> List[Path]:
        root = Path(local_directory)
        files = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = self._norm_rel(str(path.relative_to(root)))
            if rel in {self.CANONICAL_LOG_NAME, self.LEGACY_LOG_NAME, self.MANIFEST_NAME}:
                continue
            if rel.startswith(self.STAGING_DIR + "/") or path.name.startswith("."):
                continue
            files.append(path)
        return sorted(files, key=lambda p: self._norm_rel(str(p.relative_to(root))))

    def _result(self, start: float, **kwargs) -> SyncResult:
        return SyncResult(elapsed_seconds=time.time() - start, **kwargs)

    def upload_directory(self, local_directory: str, remote_directory: str, check_type: str = "DIR") -> SyncResult:
        self.connect()
        start = time.time()
        errors: List[str] = []
        uploaded = skipped = failed = 0
        bytes_transferred = 0

        try:
            remote_prefix = self._norm_rel(remote_directory)
            self._cleanup_staging(remote_directory)
            manifest = self._load_remote_manifest(remote_directory)
            remote_entries = self.list_files(remote_directory)
            remote_by_path: Dict[str, List[StorageFileInfo]] = {}
            for info in remote_entries:
                remote_by_path.setdefault(self._norm_rel(info.remote_path), []).append(info)

            files = self._collect_local_files(local_directory)
            for path in files:
                rel = self._norm_rel(str(path.relative_to(local_directory)))
                remote_rel = f"{remote_prefix}/{rel}" if remote_prefix else rel
                digest = compute_file_hash(str(path))
                size = path.stat().st_size
                recorded = manifest.get(rel)
                existing = remote_by_path.get(remote_rel, [])

                if (
                    isinstance(recorded, dict)
                    and recorded.get("hash") == digest
                    and int(recorded.get("size") or -1) == size
                    and len(existing) == 1
                    and existing[0].size_bytes == size
                ):
                    skipped += 1
                    continue

                try:
                    transferred = self._stage_and_replace(
                        str(path), remote_directory, rel, existing_count=len(existing)
                    )
                    uploaded += 1
                    bytes_transferred += transferred
                    remote_by_path[remote_rel] = [StorageFileInfo(remote_path=remote_rel, size_bytes=size)]
                except Exception as exc:
                    failed += 1
                    errors.append(f"{rel}: {exc}")
                    break

            if not errors:
                manifest_path = self._write_manifest(local_directory)
                try:
                    manifest_existing = len(remote_by_path.get(
                        f"{remote_prefix}/{self.MANIFEST_NAME}" if remote_prefix else self.MANIFEST_NAME, []
                    ))
                    transferred = self._stage_and_replace(
                        manifest_path, remote_directory, self.MANIFEST_NAME, existing_count=manifest_existing
                    )
                    uploaded += 1
                    bytes_transferred += transferred
                finally:
                    try:
                        os.unlink(manifest_path)
                    except OSError:
                        pass

                local_log = os.path.join(local_directory, self.CANONICAL_LOG_NAME)
                if os.path.exists(local_log):
                    log_existing = len(remote_by_path.get(
                        f"{remote_prefix}/{self.CANONICAL_LOG_NAME}" if remote_prefix else self.CANONICAL_LOG_NAME, []
                    ))
                    transferred = self._stage_and_replace(
                        local_log, remote_directory, self.CANONICAL_LOG_NAME, existing_count=log_existing
                    )
                    uploaded += 1
                    bytes_transferred += transferred

                legacy_existing = len(
                    remote_by_path.get(
                        f"{remote_prefix}/{self.LEGACY_LOG_NAME}"
                        if remote_prefix
                        else self.LEGACY_LOG_NAME,
                        [],
                    )
                )

                self._delete_known_matches(remote_directory, self.LEGACY_LOG_NAME, legacy_existing)
                # Do not recursively remove the staging directory here. It may
                # contain files left by a previously interrupted replacement;
                # those can be the only verified remote copy of an archive item.
                # Successfully moved current uploads no longer have staged files.

        except Exception as exc:
            failed += 1
            errors.append(str(exc))

        result = self._result(
            start,
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
            bytes_transferred=bytes_transferred,
            errors=errors,
        )
        print(f"MEGA upload: {result.summary()}")
        return result

    def download_directory(self, remote_directory: str, local_directory: str, check_type: str = "DIR") -> SyncResult:
        self.connect()
        start = time.time()
        os.makedirs(local_directory, exist_ok=True)
        if check_type.upper() == "LOG":
            log_result = self._download_log_only(remote_directory, local_directory, start)
            if self._process_gdpr_enabled():
                return self._merge_results(log_result, self._download_gdpr_data(remote_directory, local_directory))
            return log_result

        errors: List[str] = []
        downloaded = skipped = failed = 0
        bytes_transferred = 0
        prefix = self._norm_rel(remote_directory)

        try:
            manifest = self._load_remote_manifest(remote_directory)
            remote_entries = self.list_files(remote_directory)
            duplicate_paths = {}
            for info in remote_entries:
                key = self._norm_rel(info.remote_path)
                duplicate_paths[key] = duplicate_paths.get(key, 0) + 1
            duplicate_errors = [
                f"MEGA contains {count} copies of {path}; refusing ambiguous restore."
                for path, count in duplicate_paths.items() if count > 1
            ]
            if duplicate_errors:
                errors.extend(duplicate_errors[:20])
                failed += len(duplicate_errors)
            for info in sorted(remote_entries, key=lambda i: self._norm_rel(i.remote_path)):
                if duplicate_paths.get(self._norm_rel(info.remote_path), 0) > 1:
                    continue
                rel = self._norm_rel(info.remote_path)
                if prefix and rel.startswith(prefix + "/"):
                    rel = rel[len(prefix) + 1:]
                if rel in {self.CANONICAL_LOG_NAME, self.LEGACY_LOG_NAME, self.MANIFEST_NAME} or rel.startswith(self.STAGING_DIR + "/"):
                    continue
                local_path = os.path.join(local_directory, rel)
                recorded = manifest.get(rel)
                if os.path.exists(local_path) and isinstance(recorded, dict):
                    try:
                        if (
                            recorded.get("hash") == compute_file_hash(local_path)
                            and int(recorded.get("size") or -1) == os.path.getsize(local_path)
                        ):
                            skipped += 1
                            continue
                    except OSError:
                        pass
                try:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    self.download_file(info.remote_path, local_path)
                    downloaded += 1
                    bytes_transferred += os.path.getsize(local_path)
                except Exception as exc:
                    failed += 1
                    errors.append(f"{info.remote_path}: {exc}")

            # Commit local log last, so a failed restore never makes the local archive look complete.
            if not errors:
                log_result = self._download_log_only(remote_directory, local_directory, time.time())
                downloaded += log_result.downloaded
                failed += log_result.failed
                bytes_transferred += log_result.bytes_transferred
                errors.extend(log_result.errors)

        except Exception as exc:
            failed += 1
            errors.append(str(exc))

        result = self._result(
            start,
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            bytes_transferred=bytes_transferred,
            errors=errors,
        )
        print(f"MEGA download: {result.summary()}")
        return result

    def _merge_results(self, a: SyncResult, b: SyncResult) -> SyncResult:
        return SyncResult(
            uploaded=a.uploaded + b.uploaded,
            downloaded=a.downloaded + b.downloaded,
            skipped=a.skipped + b.skipped,
            failed=a.failed + b.failed,
            bytes_transferred=a.bytes_transferred + b.bytes_transferred,
            elapsed_seconds=max(a.elapsed_seconds, b.elapsed_seconds),
            errors=[*a.errors, *b.errors],
        )

    def __repr__(self) -> str:
        return f"MegaStorageProvider(remote={self._mega_remote!r})"


__all__ = ["MegaStorageProvider"]
                 
