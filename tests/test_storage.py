"""
Unit tests for the storage provider abstraction layer.

All cloud provider tests use mocks — no real AWS or Dropbox access required.
"""

import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.storage.base import StorageProvider, StorageFileInfo, SyncResult
from utils.storage.content_hash import compute_file_hash, compute_bytes_hash, hashes_match


# ---------------------------------------------------------------
# Base types
# ---------------------------------------------------------------

class TestStorageProvider(unittest.TestCase):
    """Test StorageProvider enum values."""

    def test_enum_values(self):
        self.assertEqual(StorageProvider.NONE.value, "none")
        self.assertEqual(StorageProvider.DROPBOX.value, "dropbox")
        self.assertEqual(StorageProvider.S3.value, "s3")

    def test_from_string(self):
        self.assertEqual(StorageProvider("none"), StorageProvider.NONE)
        self.assertEqual(StorageProvider("dropbox"), StorageProvider.DROPBOX)
        self.assertEqual(StorageProvider("s3"), StorageProvider.S3)

    def test_invalid_value(self):
        with self.assertRaises(ValueError):
            StorageProvider("gcs")


class TestStorageFileInfo(unittest.TestCase):
    """Test StorageFileInfo frozen dataclass."""

    def test_creation(self):
        info = StorageFileInfo(
            remote_path="/reddit/test.md",
            content_hash="abc123",
            size_bytes=1024,
            last_modified="2026-01-01T00:00:00Z",
        )
        self.assertEqual(info.remote_path, "/reddit/test.md")
        self.assertEqual(info.content_hash, "abc123")
        self.assertEqual(info.size_bytes, 1024)

    def test_frozen(self):
        info = StorageFileInfo(remote_path="/test.md")
        with self.assertRaises(FrozenInstanceError):
            info.remote_path = "/other.md"

    def test_defaults(self):
        info = StorageFileInfo(remote_path="/test.md")
        self.assertIsNone(info.content_hash)
        self.assertEqual(info.size_bytes, 0)
        self.assertIsNone(info.last_modified)


class TestSyncResult(unittest.TestCase):
    """Test SyncResult frozen dataclass."""

    def test_creation(self):
        result = SyncResult(uploaded=5, skipped=10, bytes_transferred=2048)
        self.assertEqual(result.uploaded, 5)
        self.assertEqual(result.skipped, 10)
        self.assertEqual(result.bytes_transferred, 2048)

    def test_total_processed(self):
        result = SyncResult(uploaded=3, downloaded=2, skipped=5, failed=1)
        self.assertEqual(result.total_processed, 11)

    def test_success_rate(self):
        result = SyncResult(uploaded=9, failed=1)
        self.assertAlmostEqual(result.success_rate, 0.9)

    def test_success_rate_zero(self):
        result = SyncResult()
        self.assertEqual(result.success_rate, 1.0)

    def test_summary(self):
        result = SyncResult(uploaded=2, skipped=3, bytes_transferred=1048576, elapsed_seconds=1.5)
        summary = result.summary()
        self.assertIn("2 uploaded", summary)
        self.assertIn("3 skipped", summary)
        self.assertIn("1.00 MB", summary)

    def test_frozen(self):
        result = SyncResult(uploaded=1)
        with self.assertRaises(FrozenInstanceError):
            result.uploaded = 2


# ---------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------

class TestContentHash(unittest.TestCase):
    """Test content hashing utilities."""

    def test_compute_file_hash(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"hello world")
            f.flush()
            path = f.name

        try:
            h = compute_file_hash(path)
            self.assertIsInstance(h, str)
            self.assertTrue(len(h) > 0)

            # Same content should produce the same hash
            h2 = compute_file_hash(path)
            self.assertEqual(h, h2)
        finally:
            os.unlink(path)

    def test_compute_bytes_hash(self):
        h1 = compute_bytes_hash(b"test data")
        h2 = compute_bytes_hash(b"test data")
        self.assertEqual(h1, h2)

        h3 = compute_bytes_hash(b"different data")
        self.assertNotEqual(h1, h3)

    def test_hashes_match_true(self):
        h = compute_bytes_hash(b"same")
        self.assertTrue(hashes_match(h, h))

    def test_hashes_match_false(self):
        h1 = compute_bytes_hash(b"a")
        h2 = compute_bytes_hash(b"b")
        self.assertFalse(hashes_match(h1, h2))

    def test_hashes_match_none(self):
        self.assertFalse(hashes_match(None, "abc"))
        self.assertFalse(hashes_match("abc", None))
        self.assertFalse(hashes_match("", "abc"))

    def test_file_hash_consistency_with_bytes_hash(self):
        """File hash of content should match bytes hash of the same content."""
        data = b"consistency check"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            path = f.name

        try:
            file_h = compute_file_hash(path)
            bytes_h = compute_bytes_hash(data)
            self.assertEqual(file_h, bytes_h)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------
# Storage factory
# ---------------------------------------------------------------

class TestStorageFactory(unittest.TestCase):
    """Test storage config loading and factory function."""

    @patch.dict(os.environ, {"STORAGE_PROVIDER": "none"}, clear=False)
    def test_provider_none(self):
        from utils.storage.factory import load_storage_config, get_storage_provider
        config = load_storage_config()
        self.assertEqual(config.provider, StorageProvider.NONE)
        self.assertIsNone(get_storage_provider(config))

    @patch.dict(os.environ, {"STORAGE_PROVIDER": "dropbox"}, clear=False)
    def test_provider_dropbox(self):
        from utils.storage.factory import load_storage_config, get_storage_provider
        config = load_storage_config()
        self.assertEqual(config.provider, StorageProvider.DROPBOX)
        provider = get_storage_provider(config)
        self.assertIsNotNone(provider)
        self.assertEqual(provider.get_provider_name(), "Dropbox")

    @patch.dict(os.environ, {
        "STORAGE_PROVIDER": "s3",
        "AWS_S3_BUCKET": "test-bucket",
        "AWS_DEFAULT_REGION": "us-east-1",
        "S3_STORAGE_CLASS": "STANDARD_IA",
    }, clear=False)
    def test_provider_s3(self):
        from utils.storage.factory import load_storage_config, get_storage_provider
        config = load_storage_config()
        self.assertEqual(config.provider, StorageProvider.S3)
        provider = get_storage_provider(config)
        self.assertIsNotNone(provider)
        self.assertEqual(provider.get_provider_name(), "AWS S3")

    @patch.dict(os.environ, {"STORAGE_PROVIDER": "invalid"}, clear=False)
    def test_invalid_provider(self):
        from utils.storage.factory import load_storage_config
        with self.assertRaises(ValueError):
            load_storage_config()

    @patch.dict(os.environ, {"STORAGE_PROVIDER": "s3"}, clear=False)
    def test_s3_missing_bucket(self):
        from utils.storage.factory import load_storage_config, get_storage_provider
        # Remove bucket env var if present
        env = os.environ.copy()
        env.pop("AWS_S3_BUCKET", None)
        with patch.dict(os.environ, env, clear=True):
            config = load_storage_config()
            with self.assertRaises(ValueError):
                get_storage_provider(config)

    @patch.dict(os.environ, {
        "STORAGE_PROVIDER": "s3",
        "AWS_S3_BUCKET": "my-bucket",
        "S3_STORAGE_CLASS": "GLACIER_IR",
    }, clear=False)
    def test_s3_storage_class_override(self):
        from utils.storage.factory import load_storage_config
        config = load_storage_config()
        self.assertEqual(config.s3_storage_class, "GLACIER_IR")

    @patch.dict(os.environ, {
        "STORAGE_PROVIDER": "s3",
        "AWS_S3_BUCKET": "my-bucket",
        "S3_ENDPOINT_URL": "http://localhost:4566",
    }, clear=False)
    def test_s3_endpoint_url(self):
        from utils.storage.factory import load_storage_config
        config = load_storage_config()
        self.assertEqual(config.s3_endpoint_url, "http://localhost:4566")


# ---------------------------------------------------------------
# S3 provider (mocked)
# ---------------------------------------------------------------

class TestS3StorageProvider(unittest.TestCase):
    """Test S3 provider with mocked boto3."""

    def _make_provider(self, **kwargs):
        from utils.storage.s3_provider import S3StorageProvider
        defaults = {"bucket": "test-bucket", "region": "us-east-1", "storage_class": "STANDARD_IA"}
        defaults.update(kwargs)
        return S3StorageProvider(**defaults)

    def test_init_valid_storage_class(self):
        provider = self._make_provider(storage_class="GLACIER_IR")
        self.assertEqual(provider._storage_class, "GLACIER_IR")

    def test_init_invalid_storage_class(self):
        with self.assertRaises(ValueError):
            self._make_provider(storage_class="INVALID")

    def test_provider_name(self):
        provider = self._make_provider()
        self.assertEqual(provider.get_provider_name(), "AWS S3")

    def test_require_client_before_operations(self):
        provider = self._make_provider()
        with self.assertRaises(RuntimeError):
            provider.list_files("/test")

    @patch("utils.storage.s3_provider._ensure_boto3")
    def test_connect(self, mock_ensure):
        provider = self._make_provider()
        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client

        with patch("utils.storage.s3_provider._boto3") as mock_boto3, \
             patch("utils.storage.s3_provider._botocore") as mock_botocore, \
             patch("utils.storage.s3_provider._TransferConfig") as mock_tc:
            mock_boto3.Session.return_value = mock_session
            mock_botocore.config.Config.return_value = MagicMock()
            mock_tc.return_value = MagicMock()

            provider.connect()
            self.assertIsNotNone(provider._s3)
            mock_client.head_bucket.assert_called_once_with(Bucket="test-bucket")

    @patch("utils.storage.s3_provider._ensure_boto3")
    def test_upload_file(self, mock_ensure):
        provider = self._make_provider()
        mock_client = MagicMock()
        provider._s3 = mock_client
        provider._transfer_config = MagicMock()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".md") as f:
            f.write(b"test content")
            path = f.name

        try:
            info = provider.upload_file(path, "reddit/test.md")
            self.assertEqual(info.remote_path, "reddit/test.md")
            self.assertGreater(info.size_bytes, 0)
            self.assertIsNotNone(info.content_hash)
            mock_client.upload_file.assert_called_once()
        finally:
            os.unlink(path)

    @patch("utils.storage.s3_provider._ensure_boto3")
    def test_file_log_always_standard(self, mock_ensure):
        """file_log.json should always use STANDARD class regardless of config."""
        provider = self._make_provider(storage_class="DEEP_ARCHIVE")
        mock_client = MagicMock()
        provider._s3 = mock_client
        provider._transfer_config = MagicMock()

        # Simulate file not existing on S3 (404) so Glacier check passes
        error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
        client_error_type = type("ClientError", (Exception,), {})
        mock_client.exceptions.ClientError = client_error_type
        client_error = client_error_type()
        client_error.response = error_response
        mock_client.head_object.side_effect = client_error

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "file_log.json")
            with open(path, "wb") as f:
                f.write(b'{"test": true}')

            provider.upload_file(path, "reddit/file_log.json")
            call_args = mock_client.upload_file.call_args
            extra_args = call_args[1]["ExtraArgs"]
            self.assertEqual(extra_args["StorageClass"], "STANDARD")


# ---------------------------------------------------------------
# Dropbox provider (mocked)
# ---------------------------------------------------------------

class TestDropboxStorageProvider(unittest.TestCase):
    """Test Dropbox provider with mocked dropbox SDK."""

    def _make_provider(self):
        from utils.storage.dropbox_provider import DropboxStorageProvider
        return DropboxStorageProvider(dropbox_directory="/reddit")

    def test_provider_name(self):
        provider = self._make_provider()
        self.assertEqual(provider.get_provider_name(), "Dropbox")

    def test_require_client_before_operations(self):
        provider = self._make_provider()
        with self.assertRaises(RuntimeError):
            provider.list_files("/reddit")

    @patch.dict(os.environ, {
        "DROPBOX_REFRESH_TOKEN": "test_token",
        "DROPBOX_APP_KEY": "test_key",
        "DROPBOX_APP_SECRET": "test_secret",
    })
    @patch("utils.storage.dropbox_provider._ensure_dropbox")
    @patch("utils.storage.dropbox_provider._dropbox")
    def test_connect(self, mock_dbx_module, mock_ensure):
        provider = self._make_provider()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "new_token"}

        with patch("requests.post", return_value=mock_response):
            mock_dbx_module.Dropbox.return_value = MagicMock()
            provider.connect()
            self.assertIsNotNone(provider._dbx)

    @patch.dict(os.environ, {}, clear=False)
    def test_connect_missing_creds(self):
        provider = self._make_provider()

        # Clear Dropbox env vars
        for key in ["DROPBOX_REFRESH_TOKEN", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"]:
            os.environ.pop(key, None)

        with patch("utils.storage.dropbox_provider._ensure_dropbox"):
            with self.assertRaises(RuntimeError):
                provider.connect()

    def test_upload_skips_log_when_a_content_file_fails(self):
        provider = self._make_provider()
        provider._dbx = MagicMock()
        provider._max_workers = 1

        with tempfile.TemporaryDirectory() as temp_dir:
            post_path = os.path.join(temp_dir, "post.md")
            log_path = os.path.join(temp_dir, "file_log.json")
            for path in (post_path, log_path):
                with open(path, "w", encoding="utf-8") as output_file:
                    output_file.write("content")

            uploaded_paths = []

            def upload(local_path, remote_path):
                uploaded_paths.append((local_path, remote_path))
                if local_path == post_path:
                    raise RuntimeError("network failure")
                return os.path.getsize(local_path)

            with patch.object(provider, "list_files", return_value=[]):
                with patch.object(provider, "_raw_upload", side_effect=upload):
                    result = provider.upload_directory(temp_dir, "/reddit")

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.uploaded, 0)
        self.assertEqual([path for path, _ in uploaded_paths], [post_path])

    def test_uploads_log_after_content_files_succeed(self):
        provider = self._make_provider()
        provider._dbx = MagicMock()
        provider._max_workers = 1

        with tempfile.TemporaryDirectory() as temp_dir:
            post_path = os.path.join(temp_dir, "post.md")
            log_path = os.path.join(temp_dir, "file_log.json")
            for path in (post_path, log_path):
                with open(path, "w", encoding="utf-8") as output_file:
                    output_file.write("content")

            uploaded_paths = []

            def upload(local_path, remote_path):
                uploaded_paths.append((local_path, remote_path))
                return os.path.getsize(local_path)

            with patch.object(provider, "list_files", return_value=[]):
                with patch.object(provider, "_raw_upload", side_effect=upload):
                    result = provider.upload_directory(temp_dir, "/reddit")

        self.assertEqual(result.failed, 0)
        self.assertEqual(result.uploaded, 2)
        self.assertEqual([path for path, _ in uploaded_paths], [post_path, log_path])


# ---------------------------------------------------------------
# Migration
# ---------------------------------------------------------------

class TestStorageMigration(unittest.TestCase):
    """Test migration between providers."""

    def _make_mock_provider(self, name="MockProvider"):
        mock = MagicMock()
        mock.get_provider_name.return_value = name
        return mock

    def test_dry_run(self):
        from utils.storage.migration import StorageMigration

        source = self._make_mock_provider("Dropbox")
        target = self._make_mock_provider("AWS S3")

        source.list_files.return_value = [
            StorageFileInfo(remote_path="/reddit/post1.md", size_bytes=1024),
            StorageFileInfo(remote_path="/reddit/post2.md", size_bytes=2048),
        ]

        migration = StorageMigration(source, target, "/reddit", "reddit")
        plan = migration.dry_run()

        self.assertEqual(plan.file_count, 2)
        self.assertEqual(plan.total_bytes, 3072)
        self.assertEqual(plan.source_provider, "Dropbox")
        self.assertEqual(plan.target_provider, "AWS S3")
        target.upload_file.assert_not_called()

    def test_execute(self):
        from utils.storage.migration import StorageMigration

        source = self._make_mock_provider("Dropbox")
        target = self._make_mock_provider("AWS S3")

        source.list_files.return_value = [
            StorageFileInfo(remote_path="/reddit/post1.md", size_bytes=100),
        ]

        # download_file should write a real file
        def fake_download(remote_path, local_path):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w") as f:
                f.write("content")
            return StorageFileInfo(remote_path=remote_path, size_bytes=100)

        source.download_file.side_effect = fake_download
        target.upload_file.return_value = StorageFileInfo(
            remote_path="reddit/post1.md", size_bytes=100,
        )

        migration = StorageMigration(source, target, "/reddit", "reddit")
        result = migration.execute()

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(result.failed, 0)
        source.download_file.assert_called_once()
        target.upload_file.assert_called_once()


# ---------------------------------------------------------------
# Config validator: storage section
# ---------------------------------------------------------------

class TestConfigValidatorStorage(unittest.TestCase):
    """Test config_validator.py storage validation."""

    def _get_validator(self):
        from utils.config_validator import ConfigValidator
        return ConfigValidator()

    def test_no_storage_section_is_valid(self):
        """Missing [Storage] section should not produce errors."""
        validator = self._get_validator()
        validator.config_parser.remove_section('Storage')
        validator.validate_storage_section()
        self.assertEqual(len(validator.errors), 0)

    def test_valid_provider_none(self):
        validator = self._get_validator()
        validator.config_parser.set('Storage', 'provider', 'none')
        validator.validate_storage_section()
        self.assertEqual(len(validator.errors), 0)

    def test_invalid_provider(self):
        validator = self._get_validator()
        if not validator.config_parser.has_section('Storage'):
            validator.config_parser.add_section('Storage')
        validator.config_parser.set('Storage', 'provider', 'gcs')
        validator.validate_storage_section()
        self.assertTrue(any("Invalid storage provider" in e for e in validator.errors))

    def test_s3_missing_bucket(self):
        validator = self._get_validator()
        if not validator.config_parser.has_section('Storage'):
            validator.config_parser.add_section('Storage')
        validator.config_parser.set('Storage', 'provider', 's3')
        validator.config_parser.set('Storage', 's3_bucket', 'None')
        # Clear env var if set
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWS_S3_BUCKET", None)
            validator.validate_storage_section()
        self.assertTrue(any("s3_bucket" in e for e in validator.errors))

    def test_s3_invalid_storage_class(self):
        validator = self._get_validator()
        if not validator.config_parser.has_section('Storage'):
            validator.config_parser.add_section('Storage')
        validator.config_parser.set('Storage', 'provider', 's3')
        validator.config_parser.set('Storage', 's3_bucket', 'test-bucket')
        validator.config_parser.set('Storage', 's3_storage_class', 'INVALID_CLASS')
        validator.validate_storage_section()
        self.assertTrue(any("s3_storage_class" in e for e in validator.errors))


# ---------------------------------------------------------------
# Feature flags: storage summary
# ---------------------------------------------------------------

class TestStorageSummary(unittest.TestCase):
    """Test get_storage_summary()."""

    @patch.dict(os.environ, {"STORAGE_PROVIDER": "none"}, clear=False)
    def test_summary_none(self):
        from utils.feature_flags import get_storage_summary
        summary = get_storage_summary()
        self.assertIn("DISABLED", summary)

    @patch.dict(os.environ, {"STORAGE_PROVIDER": "dropbox"}, clear=False)
    def test_summary_dropbox(self):
        from utils.feature_flags import get_storage_summary
        summary = get_storage_summary()
        self.assertIn("Dropbox", summary)

    @patch.dict(os.environ, {
        "STORAGE_PROVIDER": "s3",
        "AWS_S3_BUCKET": "my-bucket",
        "S3_STORAGE_CLASS": "GLACIER_IR",
    }, clear=False)
    def test_summary_s3(self):
        from utils.feature_flags import get_storage_summary
        summary = get_storage_summary()
        self.assertIn("S3", summary)
        self.assertIn("my-bucket", summary)
        self.assertIn("GLACIER_IR", summary)


if __name__ == "__main__":
    unittest.main()
