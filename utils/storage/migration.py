"""
Bidirectional migration tool between storage providers.

Downloads everything from source to a temp directory, then uploads
to target.  Supports dry-run mode (default) and explicit execution.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import List
from utils.storage.base import StorageFileInfo, StorageProviderProtocol, SyncResult

@dataclass(frozen=True)
class MigrationPlan:
    source_provider: str
    target_provider: str
    file_count: int
    total_bytes: int
    files: List[StorageFileInfo]
    def summary(self) -> str:
        return (f"Migration plan: {self.source_provider} -> {self.target_provider}\n"
                f"  Files: {self.file_count}\n"
                f"  Total size: {self.total_bytes/(1024*1024):.2f} MB")

class StorageMigration:
    """Migrate files without committing file_log.json until target content is complete."""
    def __init__(self, source: StorageProviderProtocol, target: StorageProviderProtocol,
                 source_directory: str, target_directory: str):
        self._source=source; self._target=target; self._source_dir=source_directory; self._target_dir=target_directory

    def dry_run(self) -> MigrationPlan:
        files=self._source.list_files(self._source_dir); seen=set(); unique=[]
        for info in files:
            if info.remote_path not in seen: seen.add(info.remote_path); unique.append(info)
        plan=MigrationPlan(self._source.get_provider_name(), self._target.get_provider_name(), len(unique),
                           sum(f.size_bytes for f in unique), unique)
        print(plan.summary()); return plan

    def execute(self) -> SyncResult:
        start=time.time(); tmp_dir=tempfile.mkdtemp(prefix="reddit_stash_migrate_")
        downloaded=uploaded=failed_downloads=failed_uploads=bytes_transferred=0
        download_errors:List[str]=[]; upload_errors:List[str]=[]
        try:
            source_files=self._source.list_files(self._source_dir)
            by_path={}; duplicate_paths=set()
            for info in source_files:
                if info.remote_path in by_path: duplicate_paths.add(info.remote_path); continue
                by_path[info.remote_path]=info
            for path in sorted(duplicate_paths):
                download_errors.append(f"source contains duplicate remote path: {path}")
            failed_downloads += len(duplicate_paths)
            src_prefix=self._source_dir.strip("/"); src_prefix_len=len(src_prefix)+1 if src_prefix else 0
            for info in by_path.values():
                rel=info.remote_path[src_prefix_len:].lstrip("/") if src_prefix else info.remote_path.strip("/")
                local=os.path.join(tmp_dir, rel)
                try:
                    self._source.download_file(info.remote_path, local); downloaded+=1
                except Exception as exc:
                    failed_downloads+=1; download_errors.append(f"download {info.remote_path}: {exc}")
            print(f"Downloaded {downloaded} files ({failed_downloads} failed)")
            content_files=[]; pending_log=None
            for root, _dirs, fnames in os.walk(tmp_dir):
                for fname in fnames:
                    file_path=os.path.join(root,fname); rel=os.path.relpath(file_path,tmp_dir).replace(os.sep,"/")
                    if rel=="file_log.json": pending_log=(file_path,rel)
                    else: content_files.append((file_path,rel))
            content_files.sort(key=lambda pair:pair[1])
            tgt_prefix=self._target_dir.strip("/")
            print(f"Uploading {len(content_files)} content files to {self._target.get_provider_name()}...")
            for file_path,rel in content_files:
                remote_key=f"{tgt_prefix}/{rel}" if tgt_prefix else rel
                try:
                    info=self._target.upload_file(file_path,remote_key); uploaded+=1; bytes_transferred+=info.size_bytes
                except Exception as exc:
                    failed_uploads+=1; upload_errors.append(f"upload {rel}: {exc}")
            if pending_log and not download_errors and not upload_errors:
                remote_key=f"{tgt_prefix}/file_log.json" if tgt_prefix else "file_log.json"
                try:
                    info=self._target.upload_file(pending_log[0],remote_key); uploaded+=1; bytes_transferred+=info.size_bytes
                except Exception as exc:
                    failed_uploads+=1; upload_errors.append(f"upload file_log.json: {exc}")
            elif pending_log:
                print("Not uploading file_log.json because migration content is incomplete.")
        finally:
            shutil.rmtree(tmp_dir,ignore_errors=True)
        result=SyncResult(downloaded=downloaded,uploaded=uploaded,failed=failed_downloads+failed_uploads,
                          bytes_transferred=bytes_transferred,elapsed_seconds=time.time()-start,
                          errors=download_errors+upload_errors)
        print(f"Migration complete: {result.summary()}"); return result
