"""Tests for incremental-fetch early stop (_collect_until_known).

The risk this guards: a naive "stop at the first already-saved item" drops new
items, because a re-saved/re-voted OLD item jumps to the front of a newest-first
listing and can sit ahead of a genuinely new save. The streak-based stop must
not drop that new item, and must still stop once past the recently-touched region.
"""
import types
import unittest

from utils.file_operations import _collect_until_known
from utils.constants import EARLY_STOP_KNOWN_STREAK


# Class names matter: _collect_until_known keys off type(item).__name__.
class Submission:
    def __init__(self, id_, sub="test"):
        self.id = id_
        self.subreddit = types.SimpleNamespace(display_name=sub)


CATEGORY_MAP = {"Submission": "SAVED_POST"}


def _key(item):
    # Must match the unique_key format in save_to_file().
    return f"{item.id}-{item.subreddit.display_name}-Submission-SAVED_POST"


def _counting_iter(items, consumed):
    for it in items:
        consumed.append(it.id)
        yield it


class TestEarlyStop(unittest.TestCase):
    def test_stops_after_known_streak_and_skips_rest(self):
        new = [Submission("new")]
        known = [Submission(f"k{i}") for i in range(EARLY_STOP_KNOWN_STREAK)]
        tail = [Submission(f"t{i}") for i in range(10)]  # must NOT be fetched
        existing = {_key(k) for k in known + tail}

        consumed = []
        result = _collect_until_known(_counting_iter(new + known + tail, consumed),
                                      existing, CATEGORY_MAP)

        # Consumed exactly the new item + the streak, then broke.
        self.assertEqual(len(result), 1 + EARLY_STOP_KNOWN_STREAK)
        for t in tail:
            self.assertNotIn(t.id, consumed)  # later pages were never fetched

    def test_interleaved_new_item_is_not_dropped(self):
        # [re-saved old (known), NEW, then a long known streak].
        resaved = Submission("old_resaved")
        new = Submission("brand_new")
        known = [Submission(f"k{i}") for i in range(EARLY_STOP_KNOWN_STREAK)]
        existing = {_key(resaved)} | {_key(k) for k in known}

        result = _collect_until_known(iter([resaved, new] + known), existing, CATEGORY_MAP)
        ids = [it.id for it in result]

        self.assertIn("brand_new", ids)   # the regression this whole design exists to prevent

    def test_no_context_returns_full_listing(self):
        items = [Submission(f"s{i}") for i in range(5)]
        self.assertEqual(_collect_until_known(iter(items), set(), CATEGORY_MAP), items)
        self.assertEqual(_collect_until_known(iter(items), {"x"}, None), items)

    def test_all_new_consumes_everything(self):
        items = [Submission(f"s{i}") for i in range(150)]
        consumed = []
        result = _collect_until_known(_counting_iter(items, consumed), {"nothing"}, CATEGORY_MAP)
        self.assertEqual(len(result), 150)
        self.assertEqual(len(consumed), 150)


if __name__ == "__main__":
    unittest.main()
