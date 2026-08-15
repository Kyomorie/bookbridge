"""Issue #360 — re-matching a book must not throw away its alignment.

The `Book` catalog row and its `BookAlignment` are shared across users; only the
`user_books` claim is per-user. But every match handler reset the mapping to
'pending' unconditionally, so a second user adopting an already-matched book sent
it back through transcription and alignment — the "requires re-running the whole
process" in the report.

Covers the guard itself and the setting from #361 that fans claims out.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server


class _Book:
    """Minimal stand-in with the identity attributes the guard inspects."""

    def __init__(self, abs_id="ab-1", status="active", kosync_doc_id=None,
                 ebook_filename=None, audio_source_id=None):
        self.abs_id = abs_id
        self.status = status
        self.kosync_doc_id = kosync_doc_id
        self.ebook_filename = ebook_filename
        self.audio_source_id = audio_source_id


class MappingStatusReuseTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_db = web_server.database_service
        self.db = Mock()
        self.db.has_alignment.return_value = True
        web_server.database_service = self.db

        self._saved_setting = os.environ.get("SHARE_ALL_BOOKS_WITH_ALL_USERS")
        os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)

    def tearDown(self):
        web_server.database_service = self._saved_db
        if self._saved_setting is None:
            os.environ.pop("SHARE_ALL_BOOKS_WITH_ALL_USERS", None)
        else:
            os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = self._saved_setting


class TestPreserveOrResetMappingStatus(MappingStatusReuseTestCase):
    def test_unchanged_identity_with_alignment_stays_active(self):
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "active")

    def test_pending_book_with_reusable_alignment_is_restored_to_active(self):
        """The orphan-repair case: the row says pending but the map is right there."""
        book = _Book(status="pending", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "active")

    def test_changed_ebook_still_requeues(self):
        """A genuine re-map invalidates the old alignment and must re-run."""
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="old.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="new.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_changed_kosync_hash_still_requeues(self):
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-2", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_changed_audio_source_still_requeues(self):
        book = _Book(status="active", audio_source_id="ab-1")

        web_server._preserve_or_reset_mapping_status(book, audio_source_id="ab-2")

        self.assertEqual(book.status, "pending")

    def test_no_alignment_requeues_as_before(self):
        self.db.has_alignment.return_value = False
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_new_book_with_empty_identity_requeues(self):
        """A freshly constructed Book has no prior values to compare, and no map."""
        self.db.has_alignment.return_value = False
        book = _Book(status=None)

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_alignment_lookup_failure_falls_back_to_requeue(self):
        self.db.has_alignment.side_effect = RuntimeError("db down")
        book = _Book(status="active", kosync_doc_id="hash-1", ebook_filename="book.epub")

        web_server._preserve_or_reset_mapping_status(
            book, kosync_doc_id="hash-1", ebook_filename="book.epub",
        )

        self.assertEqual(book.status, "pending")

    def test_alignment_is_not_consulted_when_identity_changed(self):
        """Cheap path: no point asking the DB when we already know it must re-run."""
        book = _Book(status="active", ebook_filename="old.epub")

        web_server._preserve_or_reset_mapping_status(book, ebook_filename="new.epub")

        self.db.has_alignment.assert_not_called()

    def test_none_book_is_a_noop(self):
        web_server._preserve_or_reset_mapping_status(None, kosync_doc_id="hash-1")


class TestShareAllBooksClaims(MappingStatusReuseTestCase):
    """Issue #361 — opt-in visibility fan-out."""

    def test_off_by_default_claims_only_the_matching_user(self):
        web_server._claim_book_for_user_id(7, "ab-1")

        self.db.link_user_book.assert_called_once_with(7, "ab-1")
        self.db.link_book_to_all_active_users.assert_not_called()

    def test_enabled_fans_out_to_all_active_users(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"
        self.db.link_book_to_all_active_users.return_value = 2

        web_server._claim_book_for_user_id(7, "ab-1")

        self.db.link_book_to_all_active_users.assert_called_once_with("ab-1")
        self.db.link_user_book.assert_not_called()

    def test_checkbox_on_spelling_is_honoured(self):
        """Settings checkboxes POST 'on', not 'true' — the recurring bug in this repo."""
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "on"
        self.db.link_book_to_all_active_users.return_value = 1

        web_server._claim_book_for_user_id(7, "ab-1")

        self.db.link_book_to_all_active_users.assert_called_once_with("ab-1")

    def test_explicit_false_claims_only_the_matching_user(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "false"

        web_server._claim_book_for_user_id(7, "ab-1")

        self.db.link_user_book.assert_called_once_with(7, "ab-1")

    def test_missing_ids_are_a_noop(self):
        os.environ["SHARE_ALL_BOOKS_WITH_ALL_USERS"] = "true"

        web_server._claim_book_for_user_id(None, "ab-1")
        web_server._claim_book_for_user_id(7, None)

        self.db.link_book_to_all_active_users.assert_not_called()
        self.db.link_user_book.assert_not_called()


class TestShareAllBooksSettingRegistration(unittest.TestCase):
    def test_registered_for_persistence_and_as_a_boolean(self):
        """Failure mode #1: a checkbox missing from bool_keys silently never saves."""
        from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

        self.assertIn('SHARE_ALL_BOOKS_WITH_ALL_USERS', ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG['SHARE_ALL_BOOKS_WITH_ALL_USERS'], 'false')
        self.assertIn('TRANSCRIPT_MIN_COVERAGE', ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG['TRANSCRIPT_MIN_COVERAGE'], '0.85')

    def test_boolean_setting_is_in_bool_keys(self):
        source = Path(web_server.__file__).read_text(encoding='utf-8')
        bool_block = source.split('bool_keys = [', 1)[1].split(']', 1)[0]
        self.assertIn('SHARE_ALL_BOOKS_WITH_ALL_USERS', bool_block)

    def test_both_settings_are_exposed_in_the_settings_ui(self):
        template = (
            Path(web_server.__file__).parent.parent / 'templates' / 'settings.html'
        ).read_text(encoding='utf-8')
        self.assertIn('SHARE_ALL_BOOKS_WITH_ALL_USERS', template)
        self.assertIn('TRANSCRIPT_MIN_COVERAGE', template)


if __name__ == "__main__":
    unittest.main()


class TestSharedCatalogDatabaseHelpers(unittest.TestCase):
    """Real-SQLite coverage for the new DatabaseService methods."""

    def setUp(self):
        import tempfile, shutil
        from src.db.database_service import DatabaseService
        from src.db.models import Book, BookAlignment

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = DatabaseService(str(Path(self.tmp) / "test.db"))

        for abs_id in ("ab-1", "ab-2"):
            self.db.save_book(Book(abs_id=abs_id, abs_title=abs_id, status="active"))
        self.alice = self.db.create_user("alice", "pw", role="admin")
        self.bob = self.db.create_user("bob", "pw")
        self.carol = self.db.create_user("carol", "pw", active=0)

        with self.db.get_session() as session:
            session.add(BookAlignment(
                abs_id="ab-1", alignment_map_json="[]", align_method="lexical",
                total_chars=98765,
            ))

    def test_has_alignment(self):
        self.assertTrue(self.db.has_alignment("ab-1"))
        self.assertFalse(self.db.has_alignment("ab-2"))
        self.assertFalse(self.db.has_alignment(""))
        self.assertFalse(self.db.has_alignment(None))

    def test_get_alignment_total_chars(self):
        self.assertEqual(self.db.get_alignment_total_chars("ab-1"), 98765)
        self.assertIsNone(self.db.get_alignment_total_chars("ab-2"))
        self.assertIsNone(self.db.get_alignment_total_chars(None))

    def test_link_book_to_all_active_users_skips_inactive(self):
        created = self.db.link_book_to_all_active_users("ab-1")

        self.assertEqual(created, 2)
        self.assertTrue(self.db.is_user_linked(self.alice.id, "ab-1"))
        self.assertTrue(self.db.is_user_linked(self.bob.id, "ab-1"))
        self.assertFalse(self.db.is_user_linked(self.carol.id, "ab-1"))

    def test_link_book_to_all_active_users_is_idempotent(self):
        self.db.link_book_to_all_active_users("ab-1")
        self.assertEqual(self.db.link_book_to_all_active_users("ab-1"), 0)

    def test_backfill_user_books_for_user(self):
        self.db.link_user_book(self.bob.id, "ab-1")

        created = self.db.backfill_user_books_for_user(self.bob.id)

        self.assertEqual(created, 1)  # only ab-2 was missing
        self.assertTrue(self.db.is_user_linked(self.bob.id, "ab-2"))
        self.assertEqual(self.db.backfill_user_books_for_user(self.bob.id), 0)
