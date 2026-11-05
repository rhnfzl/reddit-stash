"""Regression tests for the contract Dropbox and S3 share behind StorageProviderProtocol."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import storage_utils
from tests.helpers import HAS_DROPBOX, needs_dropbox
from utils.storage.base import StorageError, StorageFileInfo, StorageProvider, SyncTally
from utils.storage.factory import StorageConfig, get_remote_directory
from utils.storage.s3_provider import S3StorageProvider

if HAS_DROPBOX:
    import utils.storage.dropbox_provider as dp


class FakeApiError(Exception):
    def __init__(self, err):
        super().__init__("api error")
        self.error = err


def _api_error(not_found):
    err = Mock()
    err.is_path.return_value = not_found
    err.get_path.return_value.is_not_found.return_value = not_found
    return FakeApiError(err)


@needs_dropbox
class DropboxCase(unittest.TestCase):
    def setUp(self):
        for name, value in (("ApiError", FakeApiError), ("dropbox", MagicMock())):
            patcher = patch.object(dp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.provider = dp.DropboxStorageProvider()
        self.provider._dbx = MagicMock()
        self.provider._dbx.files_download.return_value = (Mock(size=3), Mock(content=b"abc"))


class TestDropboxGetFileInfoErrors(DropboxCase):
    def test_missing_path_is_none(self):
        self.provider._dbx.files_get_metadata.side_effect = _api_error(not_found=True)
        self.assertIsNone(self.provider.get_file_info("/reddit/a.md"))
        self.assertFalse(self.provider.file_exists("/reddit/a.md"))

    def test_other_api_errors_are_not_reported_as_absent(self):
        self.provider._dbx.files_get_metadata.side_effect = _api_error(not_found=False)
        with self.assertRaises(StorageError) as caught:
            self.provider.file_exists("/reddit/a.md")
        self.assertIsInstance(caught.exception.__cause__, FakeApiError)


class TestDropboxFileContract(DropboxCase):
    def test_upload_accepts_s3_style_key_and_returns_dropbox_hash(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.md")
            with open(path, "wb") as f:
                f.write(b"abc")

            info = self.provider.upload_file(path, "reddit/a.md")

            self.assertEqual(self.provider._dbx.files_upload.call_args.args[1], "/reddit/a.md")
            self.assertEqual(info.content_hash, dp._dropbox_content_hash(path))

    def test_download_to_bare_filename(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            info = self.provider.download_file("/reddit/a.md", "a.md")
            self.assertEqual(info.content_hash, dp._dropbox_content_hash("a.md"))
            os.chdir(cwd)


class TestDownloadContainment(DropboxCase):
    def _assert_contained(self, provider, keys, safe_key):
        with tempfile.TemporaryDirectory() as parent:
            local = os.path.join(parent, "save")
            os.mkdir(local)
            infos = [StorageFileInfo(remote_path=k) for k in keys]
            with patch.object(provider, "list_files", return_value=infos):
                result = provider.download_directory(safe_key, local)

            self.assertEqual(result.failed, len(keys) - 1)
            self.assertEqual(result.downloaded, 1)
            self.assertEqual(os.listdir(parent), ["save"])
            self.assertEqual(os.listdir(local), ["ok.md"])

    def _s3_provider(self):
        provider = S3StorageProvider(bucket="b")
        provider._s3 = MagicMock()
        provider._s3.download_file.side_effect = (
            lambda _bucket, _key, path, Config: open(path, "wb").close()
        )
        return provider

    def test_s3_refuses_keys_outside_the_save_directory(self):
        self._assert_contained(self._s3_provider(), ["reddit/../evil.md", "reddit/ok.md"], "reddit")

    def test_dropbox_refuses_keys_outside_the_save_directory(self):
        self._assert_contained(self.provider, ["/reddit/../evil.md", "/reddit/ok.md"], "/reddit")

    def test_s3_refuses_an_absolute_style_key(self):
        provider = self._s3_provider()
        with tempfile.TemporaryDirectory() as parent:
            local = os.path.join(parent, "save")
            os.mkdir(local)
            infos = [StorageFileInfo(remote_path=f"reddit/{parent}/evil.md")]
            with patch.object(provider, "list_files", return_value=infos):
                result = provider.download_directory("reddit", local)

            self.assertEqual(result.failed, 1)
            self.assertEqual(os.listdir(parent), ["save"])

    def _assert_symlink_refused(self, provider, key):
        with tempfile.TemporaryDirectory() as parent:
            local = os.path.join(parent, "save")
            outside = os.path.join(parent, "outside")
            os.mkdir(local)
            os.mkdir(outside)
            os.symlink(outside, os.path.join(local, "link"))
            infos = [StorageFileInfo(remote_path=key)]
            with patch.object(provider, "list_files", return_value=infos):
                result = provider.download_directory(key.split("/link")[0], local)

            self.assertEqual(result.failed, 1)
            self.assertEqual(os.listdir(outside), [])

    def test_s3_refuses_a_key_through_an_outward_symlink(self):
        self._assert_symlink_refused(self._s3_provider(), "reddit/link/evil.md")

    def test_dropbox_refuses_a_key_through_an_outward_symlink(self):
        self._assert_symlink_refused(self.provider, "/reddit/link/evil.md")

    def test_s3_nul_key_fails_alone(self):
        self._assert_contained(self._s3_provider(), ["reddit/x\0y.md", "reddit/ok.md"], "reddit")

    def test_dropbox_nul_key_fails_alone(self):
        self._assert_contained(self.provider, ["/reddit/x\0y.md", "/reddit/ok.md"], "/reddit")


def _write(root, rel, content="x"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestDropboxUnreadableLocalFile(DropboxCase):
    """One file that cannot be hashed fails alone, as it does on S3."""

    def test_upload_counts_it_and_holds_back_the_log(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "bad.md")
            _write(d, "good.md")
            _write(d, "file_log.json")
            remote = [StorageFileInfo(remote_path=f"/reddit/{n}", content_hash="h") for n in ("bad.md", "good.md")]
            real_hash = dp._dropbox_content_hash

            def hash_or_fail(path):
                if path.endswith("bad.md"):
                    raise PermissionError("denied")
                return real_hash(path)

            with patch.object(self.provider, "list_files", return_value=remote), \
                    patch.object(dp, "_dropbox_content_hash", hash_or_fail):
                result = self.provider.upload_directory(d, "/reddit")

        self.assertEqual((result.failed, result.uploaded), (1, 1))
        self.assertIn("denied", result.errors[0])
        uploaded = [call.args[1] for call in self.provider._dbx.files_upload.call_args_list]
        self.assertEqual(uploaded, ["/reddit/good.md"])

    def test_download_counts_it_and_continues(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "bad.md")
            remote = [StorageFileInfo(remote_path="/reddit/bad.md", content_hash="h"),
                      StorageFileInfo(remote_path="/reddit/new.md", content_hash="h")]
            with patch.object(self.provider, "list_files", return_value=remote), \
                    patch.object(dp, "_dropbox_content_hash", side_effect=PermissionError("denied")):
                result = self.provider.download_directory("/reddit", d)

        self.assertEqual((result.failed, result.downloaded), (1, 1))


class TestUploadCandidatesMatch(DropboxCase):
    def test_dropbox_prunes_hidden_directories_like_s3(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, ".reddit-stash/control/lock.json")
            _write(d, "sub/post.md")
            with patch.object(self.provider, "list_files", return_value=[]):
                self.provider.upload_directory(d, "/reddit")

        uploaded = [call.args[1] for call in self.provider._dbx.files_upload.call_args_list]
        self.assertEqual(uploaded, ["/reddit/sub/post.md"])


class TestSyncTallyStatuses(unittest.TestCase):
    def test_unknown_status_is_not_counted_as_a_transfer(self):
        tally = SyncTally(0.0)
        with self.assertRaises(ValueError):
            tally.record("faild")
        self.assertEqual(tally.moved, 0)

    def test_failure_without_a_message_keeps_errors_as_strings(self):
        tally = SyncTally(0.0)
        tally.record("failed")
        self.assertEqual(tally.errors, ["unknown error"])


class TestS3LogOnlyUploadCancelled(unittest.TestCase):
    def test_cancelled_run_with_no_content_files_skips_the_log(self):
        provider = S3StorageProvider(bucket="b")
        provider._s3 = MagicMock()
        provider.upload_file = MagicMock()
        with tempfile.TemporaryDirectory() as d:
            _write(d, "file_log.json")
            provider._do_upload_directory(d, "reddit", 0.0, lambda: True)
        provider.upload_file.assert_not_called()

    def test_log_only_upload_still_writes_to_the_root_key(self):
        provider = S3StorageProvider(bucket="b")
        provider._s3 = MagicMock()
        provider.upload_file = MagicMock(return_value=StorageFileInfo(remote_path="k", size_bytes=1))
        with tempfile.TemporaryDirectory() as d:
            _write(d, "sub/file_log.json")
            result = provider._do_upload_directory(d, "reddit", 0.0, lambda: False)
        provider.upload_file.assert_called_once()
        self.assertEqual(provider.upload_file.call_args.args[1], "reddit/file_log.json")
        self.assertEqual(result.uploaded, 1)


class _S3Error(Exception):
    """Shaped like botocore's ClientError, whose parsed response always has the HTTP status."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}}


class TestLogDownloadFailures(DropboxCase):
    def _s3(self, snapshot=False):
        provider = S3StorageProvider(bucket="b")
        provider._s3 = MagicMock()
        provider._transfer_config = None
        provider.require_log_snapshot = snapshot
        return provider

    def _download_log(self, provider, remote="reddit"):
        with tempfile.TemporaryDirectory() as d:
            return provider.download_log(remote, d)

    def test_s3_missing_log_starts_fresh_in_both_modes(self):
        plain, snapshot = self._s3(), self._s3(snapshot=True)
        plain._s3.download_file.side_effect = _S3Error(404)
        snapshot._s3.get_object.side_effect = _S3Error(404)
        for provider in (plain, snapshot):
            result = self._download_log(provider)
            self.assertEqual((result.failed, result.downloaded), (0, 0))
        self.assertTrue(snapshot._log_snapshot_taken)

    def test_s3_any_other_error_is_a_failed_result_in_both_modes(self):
        for error in (_S3Error(500), OSError("connection reset")):
            plain, snapshot = self._s3(), self._s3(snapshot=True)
            plain._s3.download_file.side_effect = error
            snapshot._s3.get_object.side_effect = error
            for provider in (plain, snapshot):
                with self.subTest(error=error, snapshot=provider.require_log_snapshot), self.assertLogs(level="ERROR"):
                    self.assertEqual(self._download_log(provider).failed, 1)
            self.assertFalse(snapshot._log_snapshot_taken)

    def test_dropbox_missing_log_starts_fresh_and_other_errors_fail(self):
        self.provider._dbx.files_download.side_effect = _api_error(not_found=True)
        self.assertEqual(self._download_log(self.provider, "/reddit").failed, 0)
        for error in (_api_error(not_found=False), OSError("reset")):
            self.provider._dbx.files_download.side_effect = error
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self._download_log(self.provider, "/reddit").failed, 1)

    def test_dropbox_log_is_written_where_the_log_belongs(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.provider.download_log("/reddit", d)
            self.assertEqual(self.provider._dbx.files_download.call_args.args[0], "/reddit/file_log.json")
            with open(os.path.join(d, "file_log.json"), "rb") as f:
                self.assertEqual(f.read(), b"abc")
        self.assertEqual((result.downloaded, result.bytes_transferred), (1, 3))


class TestListingFailureIsAFailedResult(DropboxCase):
    def test_both_providers_count_a_failed_listing(self):
        s3 = S3StorageProvider(bucket="b")
        s3._s3 = MagicMock()
        s3._s3.get_paginator.side_effect = _S3Error(500)
        self.provider._dbx.files_list_folder.side_effect = _api_error(not_found=False)
        for provider in (s3, self.provider):
            with self.subTest(provider=provider.get_provider_name()), tempfile.TemporaryDirectory() as d:
                result = provider.download_directory("/reddit", d)
                self.assertEqual(result.failed, 1)
                self.assertTrue(result.errors[0])

    def test_s3_failed_listing_does_not_claim_there_was_nothing_to_download(self):
        s3 = S3StorageProvider(bucket="b")
        s3._s3 = MagicMock()
        s3._s3.get_paginator.side_effect = _S3Error(500)
        with tempfile.TemporaryDirectory() as d, self.assertNoLogs("utils.storage.s3_provider", "INFO"):
            s3.download_directory("/reddit", d)


class TestProviderErrorContract(DropboxCase):
    def test_s3_failures_are_storage_errors_and_a_404_is_absent(self):
        s3 = S3StorageProvider(bucket="b")
        s3._s3 = MagicMock()
        s3._s3.head_object.side_effect = _S3Error(404)
        self.assertIsNone(s3.get_file_info("reddit/a.md"))
        s3._s3.head_object.side_effect = _S3Error(500)
        with self.assertRaises(StorageError):
            s3.get_file_info("reddit/a.md")
        s3._s3.get_paginator.side_effect = _S3Error(403)
        with self.assertRaises(StorageError):
            s3.list_files("reddit")

    def test_dropbox_upload_failure_is_a_storage_error(self):
        self.provider._dbx.files_upload.side_effect = _api_error(not_found=False)
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, "post.md")
            with self.assertRaises(StorageError) as caught:
                self.provider.upload_file(path, "reddit/post.md")
        self.assertIsInstance(caught.exception.__cause__, FakeApiError)

    def test_dropbox_listing_failure_still_uploads_without_change_detection(self):
        self.provider._dbx.files_list_folder.side_effect = OSError("reset")
        with tempfile.TemporaryDirectory() as d:
            _write(d, "post.md")
            result = self.provider.upload_directory(d, "/reddit")
        self.assertEqual(result.uploaded, 1)


class TestStorageCliConstruction(unittest.TestCase):
    def test_remote_directory_only_strips_for_s3(self):
        config = StorageConfig(remote_directory="/reddit/")
        self.assertEqual(get_remote_directory(StorageProvider.S3, config), "reddit")
        self.assertEqual(get_remote_directory(StorageProvider.DROPBOX, config), "/reddit/")

    def test_migrate_without_bucket_exits_cleanly(self):
        args = Mock(execute=False, source="dropbox", target="s3")
        config = StorageConfig(s3_bucket=None)
        with patch.object(storage_utils, "load_storage_config", return_value=config):
            with self.assertRaises(SystemExit) as caught:
                storage_utils.cmd_migrate(args)
        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
