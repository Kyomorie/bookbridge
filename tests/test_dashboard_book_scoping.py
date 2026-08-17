"""Regression tests for dashboard book-list scoping.

``index()`` (``/``) and ``api_status()`` (``/api/status``) used to fetch the
whole catalog unscoped (``database_service.get_all_books()``) and rely
entirely on the in-process ``_dashboard_visible_books_for_user`` filter to
narrow it down to the current user's claimed books. That unscoped fetch fired
a DEBUG breadcrumb every dashboard poll (~30s) because the caller had a
perfectly good ``user_id`` in hand and simply didn't pass it.

Both routes now do ``database_service.get_all_books(user_id=user_id)``. The
``_dashboard_visible_books_for_user`` call stays in place afterwards as a
deliberate, redundant second guard — these tests prove both halves:

  (a)/(c) each user sees exactly their own claimed books, on both routes;
  (b)     the DB-layer fetch is actually scoped (the assertion that fails if
          someone reverts the call site back to ``get_all_books()``);
  (d)     even if the DB layer regressed and handed back the *entire*
          catalog again, the visibility filter alone still narrows the
          response to the user's claimed books — proving the redundancy is
          load-bearing, not dead code.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService
from src.db.models import Book

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class TestDashboardBookScoping(unittest.TestCase):
    """Index + status endpoint only ever show a user their own claimed books."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        # index.html extends base.html, so the app's Jinja loader must point at
        # the real templates directory (create_app defaults to /app/templates).
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "scope.db"))

        # An admin exists so DatabaseService._default_user_id() (used by
        # save_book's creator-claim fallback) resolves to it, not to Alice —
        # keeping Alice's and Bob's claimed sets exact and independent of
        # insertion order.
        self.admin = self.svc.create_user("admin-scope", "adminpw", role="admin")
        self.alice = self.svc.create_user("alice-scope", "alicepw", role="user")
        self.bob = self.svc.create_user("bob-scope", "bobpw", role="user")

        self.book_alice = Book(
            abs_id="book-alice", abs_title="Alice Only Book",
            ebook_filename="book-alice.epub", status="active", duration=100,
            user_id=self.alice.id,
        )
        self.book_bob = Book(
            abs_id="book-bob", abs_title="Bob Only Book",
            ebook_filename="book-bob.epub", status="active", duration=100,
            user_id=self.bob.id,
        )
        self.book_unclaimed = Book(
            abs_id="book-unclaimed", abs_title="Nobody Claimed This",
            ebook_filename="book-unclaimed.epub", status="active", duration=100,
            user_id=self.admin.id,
        )
        self.svc.save_book(self.book_alice)
        self.svc.save_book(self.book_bob)
        self.svc.save_book(self.book_unclaimed)
        # save_book() already auto-claims each book for its user_id (creator
        # claim); link explicitly too so the fixture's intent reads clearly
        # and doesn't depend on that side effect.
        self.svc.link_user_book(self.alice.id, "book-alice")
        self.svc.link_user_book(self.bob.id, "book-bob")

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc  # inject real svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False  # enable real per-user auth
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, username, password):
        resp = self.client.post(
            '/login', data={'username': username, 'password': password},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, f"login failed for {username}")

    def _status_abs_ids(self):
        resp = self.client.get('/api/status')
        self.assertEqual(resp.status_code, 200)
        return resp, {m["abs_id"] for m in resp.get_json()["mappings"]}

    # ---- (a)/(c): each user sees exactly their own claimed books ----------

    def test_api_status_scopes_to_alices_claimed_books(self):
        self._login("alice-scope", "alicepw")
        _, abs_ids = self._status_abs_ids()
        self.assertEqual(abs_ids, {"book-alice"})

    def test_api_status_scopes_to_bobs_claimed_books(self):
        self._login("bob-scope", "bobpw")
        _, abs_ids = self._status_abs_ids()
        self.assertEqual(abs_ids, {"book-bob"})

    def test_index_html_shows_only_alices_claimed_book(self):
        self._login("alice-scope", "alicepw")
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Alice Only Book", html)
        self.assertNotIn("Bob Only Book", html)
        self.assertNotIn("Nobody Claimed This", html)

    def test_index_html_shows_only_bobs_claimed_book(self):
        self._login("bob-scope", "bobpw")
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bob Only Book", html)
        self.assertNotIn("Alice Only Book", html)
        self.assertNotIn("Nobody Claimed This", html)

    # ---- (b): the DB-layer fetch is actually scoped now -------------------
    # This is the assertion that fails if someone reverts the call sites back
    # to the unscoped `database_service.get_all_books()`.

    def test_index_fetches_books_scoped_to_the_logged_in_user(self):
        self._login("alice-scope", "alicepw")
        with patch.object(self.svc, 'get_all_books', wraps=self.svc.get_all_books) as spy:
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        spy.assert_called_once_with(user_id=self.alice.id)

    def test_api_status_fetches_books_scoped_to_the_logged_in_user(self):
        self._login("bob-scope", "bobpw")
        with patch.object(self.svc, 'get_all_books', wraps=self.svc.get_all_books) as spy:
            resp = self.client.get('/api/status')
        self.assertEqual(resp.status_code, 200)
        spy.assert_called_once_with(user_id=self.bob.id)

    # ---- (d): the visibility filter is still applied, even if the DB layer
    # were to regress and hand back the whole catalog regardless of user_id.
    # Protects the deliberate redundancy of keeping
    # _dashboard_visible_books_for_user after the DB fetch is scoped.

    def test_api_status_visibility_filter_survives_an_unscoped_db_layer(self):
        self._login("alice-scope", "alicepw")
        full_catalog = [self.book_alice, self.book_bob, self.book_unclaimed]
        with patch.object(self.svc, 'get_all_books', return_value=full_catalog):
            resp = self.client.get('/api/status')
        self.assertEqual(resp.status_code, 200)
        abs_ids = {m["abs_id"] for m in resp.get_json()["mappings"]}
        self.assertEqual(abs_ids, {"book-alice"})

    def test_index_html_visibility_filter_survives_an_unscoped_db_layer(self):
        self._login("bob-scope", "bobpw")
        full_catalog = [self.book_alice, self.book_bob, self.book_unclaimed]
        with patch.object(self.svc, 'get_all_books', return_value=full_catalog):
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bob Only Book", html)
        self.assertNotIn("Alice Only Book", html)
        self.assertNotIn("Nobody Claimed This", html)


if __name__ == '__main__':
    unittest.main()
