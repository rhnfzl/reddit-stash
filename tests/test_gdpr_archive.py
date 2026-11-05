"""Tests for archive-enriched GDPR CSV-only exports."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.content_recovery.recovery_config import RecoveryConfig
from utils.file_operations import SaveContext
from utils.gdpr_processor import (
    _csv_only_comment_markdown,
    _csv_only_post_markdown,
    process_gdpr_export,
)


class _ArchiveClient:
    def __init__(self, posts=None, comments=None):
        self.posts = posts or {}
        self.comments = comments or {}
        self.post_ids = []
        self.comment_ids = []
        self.post_calls = []
        self.comment_calls = []

    def fetch_posts(self, ids):
        self.post_calls.append(list(ids))
        self.post_ids.extend(ids)
        return self.posts

    def fetch_comments(self, ids):
        self.comment_calls.append(list(ids))
        self.comment_ids.extend(ids)
        return self.comments


class _PullPushFallback:
    def __init__(self, posts=None, comments=None):
        self.posts = posts or {}
        self.comments = comments or {}
        self.calls = []

    def _fetch(self, content_type, records, ids):
        requested_ids = list(ids)
        self.calls.append((content_type, requested_ids))
        return {item_id: records[item_id] for item_id in requested_ids if item_id in records}

    def fetch_posts(self, ids):
        return self._fetch('posts', self.posts, ids)

    def fetch_comments(self, ids):
        return self._fetch('comments', self.comments, ids)


class TestGdprArchiveEnrichment(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.save_directory = self.temp_dir.name
        self.gdpr_directory = Path(self.save_directory) / 'gdpr_data'
        self.gdpr_directory.mkdir()
        # An unset archive argument is built from config: keep these tests off the live archives.
        no_providers = RecoveryConfig(use_wayback_machine=False, use_arctic_shift=False, use_pullpush=False)
        config = patch('utils.gdpr_processor.load_recovery_config', return_value=no_providers)
        self.load_config = config.start()
        self.addCleanup(config.stop)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_export(self, filename, rows):
        with (self.gdpr_directory / filename).open('w', newline='', encoding='utf-8') as export_file:
            writer = csv.DictWriter(export_file, fieldnames=['id', 'permalink'])
            writer.writeheader()
            writer.writerows(rows)

    def test_csv_only_export_uses_batched_archive_content(self):
        self._write_export('saved_posts.csv', [
            {'id': 'post123', 'permalink': '/r/python/comments/post123/example/'},
            {'id': 'post789', 'permalink': '/r/python/comments/post789/example/'},
        ])
        self._write_export('saved_comments.csv', [
            {'id': 'comment456', 'permalink': '/r/python/comments/post123/example/comment456/'},
            {'id': 'comment789', 'permalink': '/r/python/comments/post789/example/comment789/'},
        ])
        archive_client = _ArchiveClient(
            posts={
                'post123': {'title': 'Archived title', 'selftext': 'Archived post body'},
                'post789': {'title': 'Second archived title', 'selftext': 'Second archived post body'},
            },
            comments={
                'comment456': {'body': 'Archived comment body'},
                'comment789': {'body': 'Second archived comment body'},
            },
        )

        stats = process_gdpr_export(
            None,
            SaveContext(self.save_directory, set(), {}, set()),
            archive_client=archive_client,
        )
        processed, skipped = stats.processed, stats.skipped

        self.assertEqual((processed, skipped), (4, 0))
        self.assertEqual(archive_client.post_calls, [['post123', 'post789']])
        self.assertEqual(archive_client.comment_calls, [['comment456', 'comment789']])
        post_file = next(Path(self.save_directory).glob('r_python/GDPR_POST_post123.md'))
        comment_file = next(Path(self.save_directory).glob('r_python/GDPR_COMMENT_comment456.md'))
        self.assertIn('# Archived title', post_file.read_text(encoding='utf-8'))
        self.assertIn('Archived post body', post_file.read_text(encoding='utf-8'))
        self.assertIn('Archived comment body', comment_file.read_text(encoding='utf-8'))

    def test_pullpush_fallback_enriches_records_missing_from_arctic_shift(self):
        self._write_export('saved_posts.csv', [
            {'id': 'post123', 'permalink': '/r/python/comments/post123/example/'},
        ])
        fallback = _PullPushFallback(
            posts={'post123': {'title': 'Fallback title', 'selftext': 'Fallback body'}},
        )

        stats = process_gdpr_export(
            None,
            SaveContext(self.save_directory, set(), {}, set()),
            archive_client=_ArchiveClient(),
            archive_fallback=fallback,
        )
        processed, skipped = stats.processed, stats.skipped

        self.assertEqual((processed, skipped), (1, 0))
        self.assertEqual(fallback.calls, [('posts', ['post123'])])
        post_file = next(Path(self.save_directory).glob('r_python/GDPR_POST_post123.md'))
        self.assertIn('# Fallback title', post_file.read_text(encoding='utf-8'))
        self.assertIn('Fallback body', post_file.read_text(encoding='utf-8'))

    def test_existing_link_only_post_is_upgraded_when_archive_data_arrives(self):
        self._write_export('saved_posts.csv', [
            {'id': 'post123', 'permalink': '/r/python/comments/post123/example/'},
        ])
        existing_keys = set()
        file_log = {}

        first_processed = process_gdpr_export(
            None,
            SaveContext(self.save_directory, existing_keys, file_log, set()),
            archive_client=_ArchiveClient(),
        ).processed
        second = process_gdpr_export(
            None,
            SaveContext(self.save_directory, existing_keys, file_log, set()),
            archive_client=_ArchiveClient(
                posts={'post123': {'title': 'Recovered title', 'selftext': 'Recovered body'}},
            ),
        )
        second_processed, second_skipped = second.processed, second.skipped

        self.assertEqual(first_processed, 1)
        self.assertEqual((second_processed, second_skipped), (1, 0))
        post_file = next(Path(self.save_directory).glob('r_python/GDPR_POST_post123.md'))
        content = post_file.read_text(encoding='utf-8')
        self.assertIn('# Recovered title', content)
        self.assertNotIn('Content was not fetched from Reddit.', content)
        self.assertTrue(file_log['GDPR_POST_post123']['archive_enriched'])

    def test_existing_link_only_comment_is_upgraded_when_archive_data_arrives(self):
        self._write_export('saved_comments.csv', [
            {'id': 'comment456', 'permalink': '/r/python/comments/post123/example/comment456/'},
        ])
        existing_keys = set()
        file_log = {}

        process_gdpr_export(
            None,
            SaveContext(self.save_directory, existing_keys, file_log, set()),
            archive_client=_ArchiveClient(),
        )
        stats = process_gdpr_export(
            None,
            SaveContext(self.save_directory, existing_keys, file_log, set()),
            archive_client=_ArchiveClient(
                comments={'comment456': {'body': 'Recovered comment body'}},
            ),
        )
        processed, skipped = stats.processed, stats.skipped

        self.assertEqual((processed, skipped), (1, 0))
        comment_file = next(Path(self.save_directory).glob('r_python/GDPR_COMMENT_comment456.md'))
        content = comment_file.read_text(encoding='utf-8')
        self.assertIn('Recovered comment body', content)
        self.assertNotIn('Content was not fetched from Reddit.', content)
        self.assertTrue(file_log['GDPR_COMMENT_comment456']['archive_enriched'])

    def test_given_up_post_is_not_retried_even_when_archive_text_arrives(self):
        self._write_export('saved_posts.csv', [
            {'id': 'post123', 'permalink': '/r/python/comments/post123/example/'},
        ])
        file_log = {'GDPR_POST_post123': {'status': 'failed', 'attempts': 5, 'last_error': 'x'}}

        stats = process_gdpr_export(
            None,
            SaveContext(self.save_directory, set(), file_log, set()),
            archive_client=_ArchiveClient(posts={'post123': {'selftext': 'Recovered body'}}),
        )
        processed, skipped, failed = stats.processed, stats.skipped, stats.failed

        self.assertEqual((processed, skipped, failed), (0, 1, 0))
        self.assertEqual(file_log['GDPR_POST_post123']['attempts'], 5)
        self.assertEqual(list(Path(self.save_directory).glob('r_python/GDPR_POST_post123.md')), [])

    def test_metadata_only_archive_record_keeps_a_post_upgradeable(self):
        self._write_export('saved_posts.csv', [
            {'id': 'post123', 'permalink': '/r/python/comments/post123/example/'},
        ])
        existing_keys = set()
        file_log = {}

        process_gdpr_export(
            None,
            SaveContext(self.save_directory, existing_keys, file_log, set()),
            archive_client=_ArchiveClient(posts={'post123': {'id': 'post123', 'author': 'archivist'}}),
        )
        first_export = next(Path(self.save_directory).glob('r_python/GDPR_POST_post123.md'))
        self.assertIn('Content was not fetched from Reddit.', first_export.read_text(encoding='utf-8'))
        self.assertFalse(file_log['GDPR_POST_post123']['archive_enriched'])

        stats = process_gdpr_export(
            None,
            SaveContext(self.save_directory, existing_keys, file_log, set()),
            archive_client=_ArchiveClient(posts={'post123': {'selftext': 'Recovered body'}}),
        )
        processed, skipped = stats.processed, stats.skipped

        self.assertEqual((processed, skipped), (1, 0))
        self.assertIn('Recovered body', first_export.read_text(encoding='utf-8'))
        self.assertTrue(file_log['GDPR_POST_post123']['archive_enriched'])

    def test_missing_link_only_export_is_recreated_when_archive_text_arrives(self):
        self._write_export('saved_comments.csv', [
            {'id': 'comment456', 'permalink': '/r/python/comments/post123/example/comment456/'},
        ])
        existing_keys = set()
        file_log = {}

        process_gdpr_export(
            None,
            SaveContext(self.save_directory, existing_keys, file_log, set()),
            archive_client=_ArchiveClient(),
        )
        comment_file = next(Path(self.save_directory).glob('r_python/GDPR_COMMENT_comment456.md'))
        comment_file.unlink()

        stats = process_gdpr_export(
            None,
            SaveContext(self.save_directory, existing_keys, file_log, set()),
            archive_client=_ArchiveClient(comments={'comment456': {'body': 'Recovered body'}}),
        )
        processed, skipped = stats.processed, stats.skipped

        self.assertEqual((processed, skipped), (1, 0))
        self.assertTrue(comment_file.exists())
        self.assertIn('Recovered body', comment_file.read_text(encoding='utf-8'))

    def test_missing_archive_record_keeps_link_only_export(self):
        self._write_export('saved_posts.csv', [
            {'id': 'post123', 'permalink': '/r/python/comments/post123/example/'},
        ])

        process_gdpr_export(
            None,
            SaveContext(self.save_directory, set(), {}, set()),
            archive_client=_ArchiveClient(),
        )

        post_file = next(Path(self.save_directory).glob('r_python/GDPR_POST_post123.md'))
        self.assertIn('Content was not fetched from Reddit.', post_file.read_text(encoding='utf-8'))

    def test_metadata_only_comment_renders_as_a_link_only_export(self):
        content = _csv_only_comment_markdown(
            'comment456',
            'https://www.reddit.com/r/python/comments/post123/example/comment456/',
            {'id': 'comment456', 'author': 'archivist'},
        )

        _, frontmatter, _ = content.split('---\n', 2)
        fields = {
            key: json.loads(value)
            for key, value in (line.split(': ', 1) for line in frontmatter.strip().splitlines())
        }
        self.assertFalse(fields['archive_enriched'])
        self.assertIn('Content was not fetched from Reddit.', content)

    def test_csv_only_posts_start_with_quoted_frontmatter(self):
        title = 'Archived: "quoted" title'
        content = _csv_only_post_markdown(
            'post123',
            'https://www.reddit.com/r/python/comments/post123/example/',
            {'title': title, 'selftext': 'Archived body'},
        )

        _, frontmatter, _ = content.split('---\n', 2)
        fields = {
            key: json.loads(value)
            for key, value in (line.split(': ', 1) for line in frontmatter.strip().splitlines())
        }
        self.assertEqual(fields['title'], title)
        self.assertEqual(fields['id'], 'post123')
        self.assertIs(fields['archive_enriched'], True)


class TestArchiveArgumentsResolveIndependently(unittest.TestCase):
    def test_passing_only_a_client_keeps_the_configured_fallback(self):
        with tempfile.TemporaryDirectory() as save_directory:
            gdpr_directory = Path(save_directory) / 'gdpr_data'
            gdpr_directory.mkdir()
            (gdpr_directory / 'saved_posts.csv').write_text('id,permalink\npost123,/r/test/comments/post123/\n')
            with patch('utils.gdpr_processor.load_recovery_config', return_value=RecoveryConfig()), \
                    patch('utils.gdpr_processor.PullPushProvider') as pullpush:
                pullpush.return_value.fetch_posts.return_value = {}
                process_gdpr_export(None, SaveContext(save_directory, set(), {}, set()), archive_client=_ArchiveClient())

        pullpush.return_value.fetch_posts.assert_called_once_with(['post123'])


if __name__ == '__main__':
    unittest.main()
