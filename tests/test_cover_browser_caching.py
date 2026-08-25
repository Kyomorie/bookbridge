"""Regression test: extracted covers carry a browser cache window.

Flask leaves `max_age` unset on `send_from_directory` by default, which emits
`Cache-Control: no-cache`. The dashboard renders one cover per book, so on a
397-book library that meant ~400 conditional revalidation round trips on EVERY
page load — each answered 304, all of them serialised behind the browser's
~6-connections-per-host cap. Covers are auth-gated, so the directive must be
`private` (never cacheable by a shared proxy).

This test fails if anyone reverts `serve_cover` to a bare
`send_from_directory(...)` without a max-age.
"""

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService

_TEMPLATES = str(Path(__file__).parent.parent / "templates")

# 1x1 JPEG, enough for send_from_directory to serve a real file.
_JPEG_BYTES = bytes.fromhex(
    "ffd8ffdb004300ff ffffffffffffffffff ffffffffffffffffff ffffffffffffffffff"
    "ffffffffffffffffff ffffffffffffffffff ffffffffffffffffff ffffffffffffff"
    .replace(" ", "")
) + b"\xff\xd9"


class TestCoverBrowserCaching(unittest.TestCase):
    """/covers/<hash>.jpg must be browser-cacheable and private."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "covers.db"))
        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        import src.web_server as ws
        self.ws = ws
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = True
        self.client = self.app.test_client()

        # Drop a real cover file where serve_cover looks for it.
        self.doc_hash = "a" * 32
        ws.COVERS_DIR.mkdir(parents=True, exist_ok=True)
        (ws.COVERS_DIR / f"{self.doc_hash}.jpg").write_bytes(_JPEG_BYTES)

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        try:
            (self.ws.COVERS_DIR / f"{self.doc_hash}.jpg").unlink()
        except OSError:
            pass
        self.ws._cover_fresh_until.pop(self.doc_hash, None)
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get_cover(self):
        resp = self.client.get(f'/covers/{self.doc_hash}.jpg')
        self.assertEqual(resp.status_code, 200, "cover should be served")
        return resp

    def test_cover_response_sets_positive_max_age(self):
        cc = self._get_cover().headers.get('Cache-Control', '')
        match = re.search(r'max-age=(\d+)', cc)
        self.assertIsNotNone(
            match,
            f"cover response must carry a max-age so browsers stop revalidating "
            f"every cover on every dashboard load; got Cache-Control={cc!r}",
        )
        self.assertGreater(int(match.group(1)), 0, f"max-age must be positive: {cc!r}")

    def test_cover_cache_is_private_not_public(self):
        """Covers are auth-gated — a shared proxy must never cache them."""
        cc = self._get_cover().headers.get('Cache-Control', '')
        self.assertIn('private', cc, f"cover cache must be private; got {cc!r}")
        self.assertNotIn('public', cc, f"cover cache must not be public; got {cc!r}")

    def test_cover_response_is_not_no_cache(self):
        """`no-cache` is the Flask default that caused the revalidation storm."""
        cc = self._get_cover().headers.get('Cache-Control', '')
        self.assertNotIn(
            'no-cache', cc,
            "cover regressed to Flask's default no-cache: the browser will "
            f"revalidate every cover on every page load. Cache-Control={cc!r}",
        )

    def test_cached_path_also_sets_headers(self):
        """The freshness-TTL fast path must set the same headers as a cold hit."""
        first = self._get_cover().headers.get('Cache-Control', '')
        second = self._get_cover().headers.get('Cache-Control', '')
        self.assertEqual(
            first, second,
            "the _cover_fresh_until fast path must not bypass the cache headers",
        )
        self.assertIn('max-age', second)


if __name__ == "__main__":
    unittest.main()
