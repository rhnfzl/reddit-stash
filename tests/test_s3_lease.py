"""S3 lease lock and coordinated --cycle, against an in-memory S3 with conditional writes."""

import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from utils.storage import s3_lease
from utils.storage.s3_lease import S3Lease, is_control_key, lease_key


class ClientError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeS3:
    """Enough of S3 for the lease: get/put with If-Match / If-None-Match semantics."""

    class exceptions:
        ClientError = ClientError

    def __init__(self):
        self.objects = {}  # key -> (bytes, etag)
        self._n = 0
        self.fail_next_put_response = False  # write lands, response is lost (retry case)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(404)
        data, etag = self.objects[Key]
        return {"Body": io.BytesIO(data), "ETag": etag}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **_):
        current = self.objects.get(Key)
        if IfNoneMatch == "*" and current is not None:
            raise ClientError(412)
        if IfMatch is not None:
            if current is None:
                raise ClientError(404)
            if current[1] != IfMatch:
                raise ClientError(412)
        self._n += 1
        etag = f'"etag-{self._n}"'
        self.objects[Key] = (Body if isinstance(Body, bytes) else Body.encode(), etag)
        if self.fail_next_put_response:
            self.fail_next_put_response = False
            raise ClientError(412)  # what a botocore retry of the landed write sees
        return {"ETag": etag}

    def body(self, key):
        return json.loads(self.objects[key][0])


KEY = lease_key("reddit")


def _lease(s3, runner="homelab"):
    return S3Lease(s3, "bucket", "reddit", runner)


class TestLeaseAcquire(unittest.TestCase):
    def test_first_runner_creates_the_lease(self):
        s3 = FakeS3()
        lease = _lease(s3)

        self.assertTrue(lease.acquire())
        body = s3.body(KEY)
        self.assertEqual((body["state"], body["lease_id"], body["runner"]), ("running", lease.lease_id, "homelab"))
        self.assertIsNone(body["last_success"])

    def test_second_runner_skips_while_lease_is_active(self):
        s3 = FakeS3()
        self.assertTrue(_lease(s3, "homelab").acquire())

        self.assertFalse(_lease(s3, "github").acquire())
        self.assertEqual(s3.body(KEY)["runner"], "homelab")

    def test_expired_lease_is_taken_over(self):
        s3 = FakeS3()
        first = _lease(s3, "homelab")
        first.acquire()
        later = s3_lease._now() + s3_lease.LEASE_TTL + s3_lease.SKEW_MARGIN + timedelta(minutes=1)

        with patch.object(s3_lease, "_now", return_value=later):
            second = _lease(s3, "github")
            self.assertTrue(second.acquire())

        self.assertEqual(s3.body(KEY)["lease_id"], second.lease_id)

    def test_corrupt_or_overlong_lease_does_not_block_forever(self):
        s3 = FakeS3()
        for body in (b"not json", json.dumps({"schema": 1, "state": "running",
                                               "expires_at": "2999-01-01T00:00:00Z"}).encode()):
            s3.objects[KEY] = (body, '"bad"')
            self.assertTrue(_lease(s3).acquire(), body)

    def test_losing_a_race_is_a_skip(self):
        s3 = FakeS3()
        lease = _lease(s3, "github")
        winner_body = json.dumps({"schema": 1, "state": "running", "lease_id": "other",
                                  "runner": "homelab", "expires_at": "2999-01-01T00:00:00Z"}).encode()
        real_put = s3.put_object

        def racing_put(**kwargs):
            s3.objects[KEY] = (winner_body, '"winner"')  # homelab wrote between our read and put
            return real_put(**kwargs)

        with patch.object(s3, "put_object", side_effect=racing_put):
            self.assertFalse(lease.acquire())

    def test_own_write_followed_by_timeout_counts_as_acquired(self):
        s3 = FakeS3()
        real_put = s3.put_object

        def put_then_timeout(**kwargs):
            real_put(**kwargs)
            raise TimeoutError("read timeout")  # no HTTP status at all

        lease = _lease(s3)
        with patch.object(s3, "put_object", side_effect=put_then_timeout):
            self.assertTrue(lease.acquire())
            self.assertTrue(lease.release(success=True))
        self.assertEqual(s3.body(KEY)["state"], "idle")

    def test_timeout_without_landing_is_raised(self):
        s3 = FakeS3()
        with patch.object(s3, "put_object", side_effect=TimeoutError("down")):
            with self.assertRaises(TimeoutError):
                _lease(s3).acquire()

    def test_own_write_with_lost_response_counts_as_acquired(self):
        s3 = FakeS3()
        s3.fail_next_put_response = True
        lease = _lease(s3)

        self.assertTrue(lease.acquire())
        self.assertTrue(lease.still_held())


class TestLeaseRelease(unittest.TestCase):
    def test_success_records_last_success_and_idles(self):
        s3 = FakeS3()
        lease = _lease(s3)
        lease.acquire()

        self.assertTrue(lease.release(success=True))
        body = s3.body(KEY)
        self.assertEqual((body["state"], body["last_success_runner"]), ("idle", "homelab"))
        self.assertIsNotNone(S3Lease.last_success(body))

    def test_failure_keeps_previous_last_success(self):
        s3 = FakeS3()
        first = _lease(s3, "homelab")
        first.acquire()
        first.release(success=True)
        previous = s3.body(KEY)["last_success"]

        second = _lease(s3, "github")
        second.acquire()
        second.release(success=False)

        self.assertEqual(s3.body(KEY)["last_success"], previous)
        self.assertEqual(s3.body(KEY)["last_success_runner"], "homelab")

    def test_release_after_takeover_leaves_new_holder_alone(self):
        s3 = FakeS3()
        stale = _lease(s3, "homelab")
        stale.acquire()
        later = s3_lease._now() + s3_lease.LEASE_TTL + s3_lease.SKEW_MARGIN + timedelta(minutes=1)
        with patch.object(s3_lease, "_now", return_value=later):
            fresh = _lease(s3, "github")
            fresh.acquire()

        self.assertFalse(stale.still_held())
        self.assertFalse(stale.release(success=True))
        body = s3.body(KEY)
        self.assertEqual((body["state"], body["lease_id"]), ("running", fresh.lease_id))

    def test_invalid_or_future_last_success_reads_as_never(self):
        self.assertIsNone(S3Lease.last_success({"last_success": "yesterday"}))
        self.assertIsNone(S3Lease.last_success({"last_success": "2999-01-01T00:00:00Z"}))
        self.assertIsNone(S3Lease.last_success(None))


class TestControlKeys(unittest.TestCase):
    def test_control_keys_are_recognised(self):
        self.assertTrue(is_control_key("reddit/.reddit-stash/control/lock.json"))
        self.assertTrue(is_control_key(".reddit-stash/control/lock.json"))
        self.assertFalse(is_control_key("reddit/r_python/POST_abc.md"))

    def test_list_files_never_returns_the_lease(self):
        from utils.storage.s3_provider import S3StorageProvider

        provider = S3StorageProvider(bucket="bucket")
        page = {"Contents": [
            {"Key": "reddit/file_log.json", "Size": 1, "LastModified": s3_lease._now()},
            {"Key": KEY, "Size": 1, "LastModified": s3_lease._now()},
        ]}
        provider._s3 = type("C", (), {"get_paginator": lambda self, _: type(
            "P", (), {"paginate": lambda self, **kw: [page]})()})()

        self.assertEqual([f.remote_path for f in provider.list_files("reddit")], ["reddit/file_log.json"])


class TestConditionalLogCommit(unittest.TestCase):
    def _provider(self, s3):
        from utils.storage.s3_provider import S3StorageProvider

        provider = S3StorageProvider(bucket="bucket")
        provider._s3 = s3
        provider.require_log_snapshot = True
        return provider

    def test_log_is_committed_when_unchanged_and_refused_when_changed(self):
        s3 = FakeS3()
        s3.objects["reddit/file_log.json"] = (b"{}", '"v1"')
        provider = self._provider(s3)
        with tempfile.TemporaryDirectory() as d:
            provider._download_log_snapshot("reddit/file_log.json", os.path.join(d, "file_log.json"), 0)
            local = os.path.join(d, "file_log.json")
            with open(local, "w") as f:
                f.write('{"a": 1}')

            provider._upload_log(local, "reddit/file_log.json")
            self.assertEqual(s3.objects["reddit/file_log.json"][0], b'{"a": 1}')

            s3.objects["reddit/file_log.json"] = (b'{"other": 1}', '"someone-else"')
            with self.assertRaises(RuntimeError):
                provider._upload_log(local, "reddit/file_log.json")
            self.assertEqual(s3.objects["reddit/file_log.json"][0], b'{"other": 1}')

    def test_missing_remote_log_is_created_only_if_still_missing(self):
        s3 = FakeS3()
        provider = self._provider(s3)
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, "file_log.json")
            provider._download_log_snapshot("reddit/file_log.json", local, 0)
            with open(local, "w") as f:
                f.write("{}")
            s3.objects["reddit/file_log.json"] = (b'{"raced": 1}', '"x"')

            with self.assertRaises(RuntimeError):
                provider._upload_log(local, "reddit/file_log.json")

    def test_landed_commit_with_lost_response_is_success(self):
        s3 = FakeS3()
        s3.objects["reddit/file_log.json"] = (b"{}", '"v1"')
        provider = self._provider(s3)
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, "file_log.json")
            provider._download_log_snapshot("reddit/file_log.json", local, 0)
            with open(local, "w") as f:
                f.write('{"a": 1}')
            s3.fail_next_put_response = True

            provider._upload_log(local, "reddit/file_log.json")

        self.assertEqual(s3.objects["reddit/file_log.json"][0], b'{"a": 1}')

    def test_upload_without_snapshot_is_refused(self):
        provider = self._provider(FakeS3())
        with tempfile.NamedTemporaryFile() as f, self.assertRaises(RuntimeError):
            provider._upload_log(f.name, "reddit/file_log.json")


class TestCycle(unittest.TestCase):
    """storage_utils --cycle orchestration with the provider and backup mocked."""

    def setUp(self):
        import storage_utils
        self.su = storage_utils
        self.s3 = FakeS3()
        self.provider = MagicMock()
        self.provider.lease.side_effect = lambda prefix, runner: S3Lease(self.s3, "b", prefix, runner)
        ok = MagicMock(failed=0, errors=[])
        self.provider.download_directory.return_value = ok
        self.provider.upload_directory.return_value = ok
        config = MagicMock(dropbox_directory="/reddit")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local_dir = tmp.name  # never the real reddit/ folder: the cycle empties it
        patches = [
            patch.dict(os.environ, {"STORAGE_COORDINATION": "true"}),
            patch.object(storage_utils, "load_storage_config", return_value=config),
            patch.object(storage_utils, "_coordination_problems", return_value=[]),
            patch.object(storage_utils, "get_storage_provider", return_value=self.provider),
            patch.object(storage_utils, "_load_local_dir", return_value=self.local_dir),
            patch.object(storage_utils, "_ping_healthcheck"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, require_stale=None, backup_ok=True):
        args = MagicMock(runner="homelab", require_stale=require_stale)
        with patch.object(self.su, "_run_backup", return_value=backup_ok), \
                self.assertRaises(SystemExit) as ctx:
            self.su.cmd_cycle(args)
        return ctx.exception.code

    def test_successful_cycle_uploads_and_records_success(self):
        self.assertEqual(self._run(), 0)
        self.provider.upload_directory.assert_called_once()
        self.assertEqual(self.s3.body(lease_key("reddit"))["state"], "idle")
        self.assertIsNotNone(self.s3.body(lease_key("reddit"))["last_success"])

    def test_failed_backup_does_not_upload_or_refresh_success(self):
        self.assertEqual(self._run(backup_ok=False), self.su.EXIT_CYCLE_FAILED)
        self.provider.upload_directory.assert_not_called()
        self.assertIsNone(self.s3.body(lease_key("reddit"))["last_success"])

    def test_held_lease_skips_without_touching_storage(self):
        S3Lease(self.s3, "b", "reddit", "github").acquire()

        self.assertEqual(self._run(), self.su.EXIT_CYCLE_SKIPPED)
        self.provider.download_directory.assert_not_called()

    def test_require_stale_skips_after_a_recent_success(self):
        self.assertEqual(self._run(), 0)
        self.provider.reset_mock()

        self.assertEqual(self._run(require_stale=6), self.su.EXIT_CYCLE_SKIPPED)
        self.provider.download_directory.assert_not_called()
        self.assertEqual(self.s3.body(lease_key("reddit"))["state"], "idle")

    def test_lost_lease_before_upload_aborts(self):
        def steal():
            self.s3.objects[lease_key("reddit")] = (b'{"schema": 1, "state": "running"}', '"stolen"')
            return True

        args = MagicMock(runner="homelab", require_stale=None)
        with patch.object(self.su, "_run_backup", side_effect=steal), self.assertRaises(SystemExit) as ctx:
            self.su.cmd_cycle(args)

        self.assertEqual(ctx.exception.code, self.su.EXIT_CYCLE_FAILED)
        self.provider.upload_directory.assert_not_called()

    def test_non_empty_save_directory_is_refused_and_kept(self):
        leftover = os.path.join(self.local_dir, "old.md")
        with open(leftover, "w") as f:
            f.write("older than S3")

        self.assertEqual(self._run(), self.su.EXIT_CYCLE_FAILED)
        self.assertTrue(os.path.exists(leftover))
        self.assertNotIn(lease_key("reddit"), self.s3.objects)

    def test_save_directory_is_emptied_after_the_cycle(self):
        def backup():
            with open(os.path.join(self.local_dir, "new.md"), "w") as f:
                f.write("x")
            return True

        args = MagicMock(runner="homelab", require_stale=None)
        with patch.object(self.su, "_run_backup", side_effect=backup), self.assertRaises(SystemExit) as ctx:
            self.su.cmd_cycle(args)

        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(os.listdir(self.local_dir), [])

    def test_unconfirmed_release_is_reported_as_failure(self):
        with patch.object(S3Lease, "release", return_value=False):
            self.assertEqual(self._run(), self.su.EXIT_CYCLE_FAILED)

    def test_errors_and_sigterm_exit_2_and_release_the_lease(self):
        for error in (OSError("disk"), self.su._Terminated()):
            with self.subTest(error=type(error).__name__):
                self.provider.download_directory.side_effect = error
                self.assertEqual(self._run(), self.su.EXIT_CYCLE_FAILED)
                self.assertEqual(self.s3.body(lease_key("reddit"))["state"], "idle")

    def test_connect_failure_exits_2(self):
        self.provider.connect.side_effect = RuntimeError("Cannot access S3 bucket")
        self.assertEqual(self._run(), self.su.EXIT_CYCLE_FAILED)

    def test_upload_and_migrate_are_refused_when_coordinated(self):
        for cmd, args in ((self.su.cmd_upload, None), (self.su.cmd_migrate, MagicMock(execute=True))):
            with self.assertRaises(SystemExit) as ctx:
                cmd(args)
            self.assertEqual(ctx.exception.code, self.su.EXIT_CYCLE_FAILED)


if __name__ == "__main__":
    unittest.main()
