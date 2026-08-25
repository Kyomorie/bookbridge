"""Regression tests: the dashboard resolves BookFusion links in ONE bulk query.

``_build_dashboard_mapping`` used to call
``database_service.get_user_bookfusion_link(user_id, abs_id)`` once per book.
On a 397-book library that is 397 queries per dashboard render, repeated every
30 seconds by the ``/api/status`` poll, and every one of them was discarded when
BookFusion was unconfigured (``templates/index.html`` gates the whole BookFusion
tile on ``{% if integrations.bookfusion %}``).

Links are now prefetched once per render by ``_prefetch_bookfusion_links`` via
the bulk ``get_user_bookfusion_links_for_books``, and skipped entirely when
BookFusion is not configured. These tests fail if either property regresses:

  (a) no per-book ``get_user_bookfusion_link`` call is ever made;
  (b) the bulk call count stays CONSTANT as the number of books grows;
  (c) an unconfigured BookFusion issues no BookFusion query at all;
  (d) the prefetched links still populate the same mapping fields as before.
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


class TestDashboardBookFusionBulkLookup(unittest.TestCase):
    """Dashboard BookFusion links cost one query per render, not one per book."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "bfbulk.db"))
        self.user = self.svc.create_user("bf-user", "bfpw", role="admin")

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

    # ---- helpers ---------------------------------------------------------

    def _seed_books(self, count):
        for i in range(count):
            abs_id = f"bf-book-{i}"
            self.svc.save_book(Book(
                abs_id=abs_id, abs_title=f"BF Book {i}",
                ebook_filename=f"{abs_id}.epub", status="active", duration=100,
                user_id=self.user.id,
            ))
            self.svc.link_user_book(self.user.id, abs_id)

    def _login(self):
        resp = self.client.post(
            '/login', data={'username': "bf-user", 'password': "bfpw"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, "login failed")

    def _render_counting_queries(self, bookfusion_configured=True):
        """Hit /api/status and count both BookFusion lookup styles."""
        import src.web_server as ws

        counts = {"per_book": 0, "bulk": 0}
        real_bulk = self.svc.get_user_bookfusion_links_for_books

        def counting_per_book(*args, **kwargs):
            counts["per_book"] += 1
            return None

        def counting_bulk(*args, **kwargs):
            counts["bulk"] += 1
            return real_bulk(*args, **kwargs)

        integrations = {'bookfusion': bookfusion_configured}

        with patch.object(self.svc, 'get_user_bookfusion_link', counting_per_book), \
             patch.object(self.svc, 'get_user_bookfusion_links_for_books', counting_bulk), \
             patch.object(ws, '_build_dashboard_integrations', lambda: integrations):
            resp = self.client.get('/api/status')
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["mappings"], counts

    # ---- (a) no per-book query -------------------------------------------

    def test_no_per_book_bookfusion_query(self):
        self._seed_books(12)
        self._login()
        mappings, counts = self._render_counting_queries()
        self.assertEqual(len(mappings), 12)
        self.assertEqual(
            counts["per_book"], 0,
            "dashboard regressed to a per-book get_user_bookfusion_link() call",
        )
        self.assertEqual(counts["bulk"], 1, "expected exactly one bulk prefetch")

    # ---- (b) constant query count as the library grows -------------------

    def test_bookfusion_query_count_constant_as_books_grow(self):
        self._seed_books(3)
        self._login()
        _, small = self._render_counting_queries()

        self._seed_books(40)  # now 40 books total
        _, large = self._render_counting_queries()

        self.assertEqual(small["per_book"], 0)
        self.assertEqual(large["per_book"], 0)
        self.assertEqual(
            small["bulk"], large["bulk"],
            "BookFusion query count must not scale with the number of books "
            f"(3 books -> {small['bulk']}, 40 books -> {large['bulk']})",
        )

    # ---- (c) unconfigured BookFusion costs nothing -----------------------

    def test_unconfigured_bookfusion_issues_no_query(self):
        self._seed_books(10)
        self._login()
        mappings, counts = self._render_counting_queries(bookfusion_configured=False)
        self.assertEqual(len(mappings), 10)
        self.assertEqual(counts["per_book"], 0)
        self.assertEqual(
            counts["bulk"], 0,
            "unconfigured BookFusion must not hit the database at all",
        )

    # ---- (d) the mapping fields still populate ---------------------------

    def test_bulk_prefetch_populates_mapping_fields(self):
        self._seed_books(4)
        self.svc.set_user_bookfusion_link(
            self.user.id, "bf-book-2", "bf-remote-99", title="Linked Title",
        )
        self._login()
        mappings, counts = self._render_counting_queries()
        by_id = {m["abs_id"]: m for m in mappings}

        linked = by_id["bf-book-2"]
        self.assertTrue(linked["bookfusion_linked"])
        self.assertEqual(linked["bookfusion_id"], "bf-remote-99")
        self.assertEqual(linked["bookfusion_title"], "Linked Title")

        unlinked = by_id["bf-book-0"]
        self.assertFalse(unlinked["bookfusion_linked"])
        self.assertIsNone(unlinked["bookfusion_id"])
        self.assertEqual(counts["per_book"], 0)


if __name__ == "__main__":
    unittest.main()
