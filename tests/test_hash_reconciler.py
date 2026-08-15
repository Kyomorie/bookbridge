"""Tests for the standalone KoSync hash reconciler.

Covers KOReaderDeviceSyncService.reconcile_hashes plus the daemon's setting reads:
- drifted hashes are counted as linked, unchanged ones are not
- a failing book does not abort the pass
- an unresolvable book counts as skipped
- the enable toggle honors both 'true' and 'on'
- the interval parses, falls back on garbage, and is floored at 5 minutes
- the daemon does no work while disabled
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services import hash_reconciler
from src.services.koreader_device_sync_service import KOReaderDeviceSyncService


class _Sentinel(Exception):
    """Breaks the daemon loop after one iteration."""


def _book(abs_id, title):
    return SimpleNamespace(
        abs_id=abs_id,
        abs_title=title,
        ebook_source="bookorbit",
        ebook_source_id="1",
        sync_mode="ebook_only",
        original_ebook_filename=f"{abs_id}.epub",
        ebook_filename=f"{abs_id}.epub",
        kosync_doc_id=None,
        status="active",
    )


class TestReconcileHashes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = KOReaderDeviceSyncService(
            database_service=MagicMock(),
            ebook_parser=MagicMock(),
            abs_client=MagicMock(),
            booklore_client=MagicMock(),
            cwa_client=MagicMock(),
            kavita_client=MagicMock(),
            epub_cache_dir=Path(self.tmp.name),
            bookorbit_client=MagicMock(),
        )
        self.books = [_book("a", "Book A"), _book("b", "Book B")]
        self.service._get_active_books = lambda: list(self.books)

    def test_counts_only_newly_linked_hashes(self):
        results = {
            "a": {"path": Path("a"), "content_hash": "hash-a"},
            "b": {"path": Path("b"), "content_hash": "hash-b"},
        }
        self.service._resolve_download_artifact = lambda book, link_hashes=True: results[book.abs_id]
        self.service.database_service.ensure_linked_kosync_document.side_effect = [True, False]

        summary = self.service.reconcile_hashes()

        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["linked"], 1)
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["errors"], 0)

    def test_failing_book_does_not_abort_the_pass(self):
        seen = []

        def resolve(book, link_hashes=True):
            seen.append(book.abs_id)
            if book.abs_id == "a":
                raise RuntimeError("boom")
            return {"path": Path("b"), "content_hash": "hash-b"}

        self.service._resolve_download_artifact = resolve
        self.service.database_service.ensure_linked_kosync_document.return_value = True

        summary = self.service.reconcile_hashes()

        self.assertEqual(seen, ["a", "b"], "the second book must still be processed")
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["linked"], 1)
        self.assertEqual(summary["checked"], 2)

    def test_unresolvable_book_counts_as_skipped(self):
        self.service._resolve_download_artifact = lambda book, link_hashes=True: None

        summary = self.service.reconcile_hashes()

        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(summary["linked"], 0)
        self.assertEqual(summary["errors"], 0)

    def test_two_books_sharing_one_file_do_not_steal_the_hash(self):
        """A catalogue mis-mapping must not make passes flip the link forever.

        Observed live: 'The Breach' and 'The Dorians' both pointed at the same EPUB,
        so each pass rebound the same hash to whichever book came last.
        """
        same = {"path": Path("shared"), "content_hash": "shared-hash"}
        self.service._resolve_download_artifact = lambda book, link_hashes=True: dict(same)
        self.service.database_service.ensure_linked_kosync_document.return_value = True

        summary = self.service.reconcile_hashes()

        self.assertEqual(summary["conflicts"], 1)
        self.assertEqual(summary["linked"], 1, "only the first claimant links")
        self.assertEqual(
            self.service.database_service.ensure_linked_kosync_document.call_count, 1,
            "the second book must not rebind the shared hash",
        )

    def test_resolution_during_reconcile_does_not_link_internally(self):
        """reconcile must resolve with link_hashes=False so it can veto a conflict."""
        captured = {}

        def resolve(book, link_hashes=True):
            captured[book.abs_id] = link_hashes
            return {"path": Path("p"), "content_hash": f"hash-{book.abs_id}"}

        self.service._resolve_download_artifact = resolve
        self.service.database_service.ensure_linked_kosync_document.return_value = False

        self.service.reconcile_hashes()

        self.assertEqual(captured, {"a": False, "b": False})

    def test_link_sibling_hash_reports_whether_anything_changed(self):
        self.service.database_service.ensure_linked_kosync_document.return_value = True
        self.assertTrue(self.service._link_sibling_hash("abs-1", "hash-1"))

        self.service.database_service.ensure_linked_kosync_document.return_value = False
        self.assertFalse(self.service._link_sibling_hash("abs-1", "hash-1"))

        self.service.database_service.ensure_linked_kosync_document.side_effect = RuntimeError("db down")
        self.assertFalse(self.service._link_sibling_hash("abs-1", "hash-1"))


class TestReconcilerSettings(unittest.TestCase):
    KEYS = ("KOSYNC_HASH_RECONCILE_ENABLED", "KOSYNC_HASH_RECONCILE_MINUTES")

    def setUp(self):
        self.original = {k: os.environ.get(k) for k in self.KEYS}

    def tearDown(self):
        for key, value in self.original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_enabled_accepts_true_and_on(self):
        for spelling in ("true", "on", "True", "1"):
            os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = spelling
            self.assertTrue(hash_reconciler._reconcile_enabled(), spelling)

    def test_disabled_when_false(self):
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "false"
        self.assertFalse(hash_reconciler._reconcile_enabled())

    def test_enabled_by_default(self):
        os.environ.pop("KOSYNC_HASH_RECONCILE_ENABLED", None)
        self.assertTrue(hash_reconciler._reconcile_enabled())

    def test_interval_default_and_parsing(self):
        os.environ.pop("KOSYNC_HASH_RECONCILE_MINUTES", None)
        self.assertEqual(hash_reconciler._reconcile_interval_seconds(), 360 * 60)

        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "30"
        self.assertEqual(hash_reconciler._reconcile_interval_seconds(), 30 * 60)

    def test_interval_falls_back_on_garbage(self):
        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "not-a-number"
        self.assertEqual(hash_reconciler._reconcile_interval_seconds(), 360 * 60)

    def test_interval_floored_at_five_minutes(self):
        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "1"
        self.assertEqual(hash_reconciler._reconcile_interval_seconds(), 5 * 60)

    def test_daemon_skips_work_while_disabled(self):
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "false"
        service = MagicMock()

        with patch.object(hash_reconciler._wake_event, "wait", side_effect=_Sentinel()):
            with self.assertRaises(_Sentinel):
                hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        service.reconcile_hashes.assert_not_called()

    def test_daemon_reconciles_while_enabled(self):
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "true"
        service = MagicMock()

        with patch.object(hash_reconciler._wake_event, "wait", side_effect=_Sentinel()):
            with self.assertRaises(_Sentinel):
                hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        service.reconcile_hashes.assert_called_once()

    def test_signal_wakes_the_daemon_early(self):
        """An unresolved hash must not wait a whole interval for the next pass."""
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "true"
        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "360"
        service = MagicMock()
        hash_reconciler._wake_event.clear()

        hash_reconciler.signal_reconcile_soon()
        self.assertTrue(hash_reconciler._wake_event.is_set())

        # Second pass starts as soon as the wait observes the signal.
        passes = []

        def fake_wait(timeout=None):
            passes.append(timeout)
            if len(passes) >= 2:
                raise _Sentinel()
            return True

        with patch.object(hash_reconciler._wake_event, "wait", side_effect=fake_wait):
            with self.assertRaises(_Sentinel):
                hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        self.assertEqual(service.reconcile_hashes.call_count, 2)
        self.assertEqual(passes[0], 360 * 60, "the wait must use the configured interval")
        hash_reconciler._wake_event.clear()


if __name__ == "__main__":
    unittest.main()
