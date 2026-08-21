"""Tests for the dashboard's "Date Added" sort.

The dashboard could sort by title, progress, status, last sync and rating, but
not by when a book joined the library — so a freshly matched book landed
wherever its title happened to fall. Sorting is client-side over ``data-*``
attributes on the rendered cards, so the server has to supply the timestamp.

There is no ``Book.created_at``: the catalog row is shared between users and
carries no per-user history. ``user_books.created_at`` does, though — the
moment *this* user claimed the book — and ``UserBook.__init__`` stamps it on
every claim, so it is populated on every install without a migration.
``DatabaseService.get_book_claim_times()`` reads it back for the dashboard.

Covered here: the claim-time lookup (scoping, the unscoped mode, NULL rows),
the ``data-added`` attribute reaching the cards in the right order, the series
group taking its newest child, and the sort option existing at all.
"""

import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService
from src.db.models import Book

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class TestBookClaimTimes(unittest.TestCase):
    """DatabaseService.get_book_claim_times()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "claims.db")
        self.svc = DatabaseService(self.db_path)
        self.alice = self.svc.create_user("alice-claims", "pw", role="user")
        self.bob = self.svc.create_user("bob-claims", "pw", role="user")

        for abs_id in ("book-one", "book-two", "book-three"):
            self.svc.save_book(Book(
                abs_id=abs_id, abs_title=abs_id, ebook_filename=abs_id + ".epub",
                status="active", duration=100, user_id=self.alice.id,
            ))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_claim_times_follow_claim_order(self):
        """Later claims carry strictly later timestamps.

        save_book() auto-claims for its user_id (creator claim), so the three
        setUp saves are themselves the claims, in order.
        """
        times = self.svc.get_book_claim_times(user_id=self.alice.id)
        self.assertLess(times["book-one"], times["book-two"])
        self.assertLess(times["book-two"], times["book-three"])

    def test_claim_times_are_scoped_to_the_user(self):
        self.svc.save_book(Book(
            abs_id="bob-book", abs_title="Bob's Book", ebook_filename="bob.epub",
            status="active", duration=100, user_id=self.bob.id,
        ))

        alice_times = self.svc.get_book_claim_times(user_id=self.alice.id)
        self.assertIn("book-one", alice_times)
        self.assertNotIn("bob-book", alice_times)

        bob_times = self.svc.get_book_claim_times(user_id=self.bob.id)
        self.assertIn("bob-book", bob_times)
        self.assertNotIn("book-one", bob_times)

    def test_unscoped_lookup_takes_the_newest_claim_across_users(self):
        """user_id=None mirrors the dashboard's unscoped fetch under LOGIN_DISABLED."""
        self.svc.link_user_book(self.alice.id, "book-one")
        self.svc.link_user_book(self.bob.id, "book-one")

        scoped = self.svc.get_book_claim_times(user_id=self.alice.id)
        unscoped = self.svc.get_book_claim_times()
        self.assertGreater(unscoped["book-one"], scoped["book-one"])

    def test_null_created_at_is_skipped_not_crashed(self):
        """created_at is nullable, so a hand-edited or foreign row must not raise."""
        self.svc.link_user_book(self.alice.id, "book-one")
        self.svc.link_user_book(self.alice.id, "book-two")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE user_books SET created_at = NULL WHERE abs_id = 'book-one'")
        conn.commit()
        conn.close()

        times = self.svc.get_book_claim_times(user_id=self.alice.id)
        self.assertNotIn("book-one", times)
        self.assertIn("book-two", times)

    def test_claim_time_is_read_as_utc_not_local(self):
        """created_at is naive UTC; reading it as local time would shift the epoch."""
        self.svc.link_user_book(self.alice.id, "book-one")
        stamp = self.svc.get_book_claim_times(user_id=self.alice.id)["book-one"]
        # Within a minute of now if stamped as UTC; hours off if read as local.
        self.assertLess(abs(stamp - time.time()), 60)


class TestDashboardAddedSort(unittest.TestCase):
    """The dashboard hands the sort the data it needs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "sortadd.db"))
        self.user = self.svc.create_user("sort-user", "sortpw", role="admin")
        for abs_id, title in (("older-book", "Older Book"), ("newer-book", "Newer Book")):
            self.svc.save_book(Book(
                abs_id=abs_id, abs_title=title, ebook_filename=abs_id + ".epub",
                status="active", duration=100, user_id=self.user.id,
            ))
        # Claim order, not title order, is what the sort must reflect.
        self.svc.link_user_book(self.user.id, "older-book")
        self.svc.link_user_book(self.user.id, "newer-book")

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dashboard(self):
        resp = self.client.post(
            '/login', data={'username': "sort-user", 'password': "sortpw"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, "login failed")
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_sort_dropdown_offers_date_added(self):
        self.assertIn('<option value="added">Date Added</option>', self._dashboard())

    def test_cards_carry_a_data_added_timestamp(self):
        html = self._dashboard()
        stamps = [float(v) for v in re.findall(r'data-added="([0-9.]+)"', html)]
        self.assertEqual(len(stamps), 2, "both cards should carry data-added")
        self.assertTrue(all(s > 0 for s in stamps), "timestamps must be populated")

    def test_data_added_reflects_claim_order_not_title_order(self):
        """The later-claimed book carries the later stamp."""
        html = self._dashboard()
        by_book = {
            m.group(1): float(m.group(2))
            for m in re.finditer(
                r'data-abs-id="([^"]+)"\s+data-added="([0-9.]+)"', html
            )
        }
        self.assertEqual(set(by_book), {"older-book", "newer-book"})
        self.assertLess(by_book["older-book"], by_book["newer-book"])


class TestSeriesGroupAddedAggregate(unittest.TestCase):
    """A series group sorts by its most recently added child."""

    def test_group_takes_the_newest_child_timestamp(self):
        from src.web_server import _finalize_series_group
        group = {
            "series_name": "Test Series",
            "series_key": "test series",
            "children": [
                {"display_title": "One", "series_sequence": 1,
                 "unified_progress": 0, "added_at_unix": 100.0},
                {"display_title": "Two", "series_sequence": 2,
                 "unified_progress": 0, "added_at_unix": 300.0},
                {"display_title": "Three", "series_sequence": 3,
                 "unified_progress": 0, "added_at_unix": 200.0},
            ],
        }
        _finalize_series_group(group)
        self.assertEqual(group["added_at_unix"], 300.0)

    def test_group_survives_children_without_timestamps(self):
        from src.web_server import _finalize_series_group
        group = {
            "series_name": "Test Series",
            "series_key": "test series",
            "children": [
                {"display_title": "One", "series_sequence": 1, "unified_progress": 0},
                {"display_title": "Two", "series_sequence": 2, "unified_progress": 0},
            ],
        }
        _finalize_series_group(group)
        self.assertEqual(group["added_at_unix"], 0.0)


if __name__ == '__main__':
    unittest.main()
