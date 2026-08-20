import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from src.utils import kosync_canonical as mod


class FakeParser:
    def __init__(self, path, mapping=None):
        self.path = Path(path)
        self.mapping = mapping or {}
        self.calls = []

    def resolve_book_path(self, filename):
        return self.path

    def resolve_xpath_to_index(self, filename, xpath):
        self.calls.append((filename, xpath))
        return self.mapping.get(xpath)


class FakeDB:
    def __init__(self):
        self.engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("""
                CREATE TABLE kosync_xpath_order_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash VARCHAR(64) NOT NULL UNIQUE,
                    document_hash VARCHAR(32) NOT NULL,
                    filename VARCHAR(500) NOT NULL,
                    device_xpath TEXT NOT NULL,
                    synced_xpath TEXT NOT NULL,
                    device_index INTEGER NOT NULL,
                    synced_index INTEGER NOT NULL,
                    file_key VARCHAR(64) NOT NULL,
                    updated_at FLOAT NOT NULL
                )
            """)
        self.document = None
        self.user_progress = None

    @contextmanager
    def get_session(self):
        with self.engine.begin() as conn:
            class Session:
                def execute(self, statement, params=None):
                    return conn.execute(statement, params or {})
            yield Session()

    def get_kosync_document(self, doc_id):
        return self.document

    def get_user_kosync_progress(self, doc_id, user_id=None):
        return self.user_progress


class CanonicalCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(b"epub-v1")
        tmp.close()
        self.path = Path(tmp.name)
        self.parser = FakeParser(self.path, {"/device": 100, "/synced": 200})
        self.db = FakeDB()
        self.memory = {}

        self._old_api_pkg = sys.modules.get("src.api")
        api_pkg = self._old_api_pkg or types.ModuleType("src.api")
        sys.modules["src.api"] = api_pkg
        server = types.ModuleType("src.api.kosync_server")
        server._database_service = self.db

        def cache_get(key):
            return self.memory.get(key)

        def cache_put(key, device_index, synced_index):
            self.memory[key] = (device_index, synced_index)

        server._xpath_index_cache_get = cache_get
        server._xpath_index_cache_put = cache_put
        sys.modules["src.api.kosync_server"] = server
        self._old_server_attr = getattr(api_pkg, "kosync_server", None)
        api_pkg.kosync_server = server

        # Reset module singleton state between tests.
        mod._INSTALLED = False

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        self.db.engine.dispose()
        sys.modules.pop("src.api.kosync_server", None)
        api_pkg = sys.modules.get("src.api")
        if api_pkg is not None:
            if self._old_server_attr is None:
                try:
                    delattr(api_pkg, "kosync_server")
                except AttributeError:
                    pass
            else:
                api_pkg.kosync_server = self._old_server_attr
        if self._old_api_pkg is None:
            sys.modules.pop("src.api", None)
        else:
            sys.modules["src.api"] = self._old_api_pkg

    def _wait_for_persisted(self, key_hash, timeout=1.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.db.engine.begin() as conn:
                count = conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM kosync_xpath_order_cache WHERE key_hash = ?",
                    (key_hash,),
                ).scalar_one()
            if count:
                return True
            time.sleep(0.01)
        return False

    def test_file_key_changes_when_epub_changes(self):
        first = mod.canonical_file_key(self.parser, "book.epub")
        time.sleep(0.002)
        self.path.write_bytes(b"epub-v2-longer")
        os.utime(self.path, None)
        second = mod.canonical_file_key(self.parser, "book.epub")
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, second)

    def test_persisted_pair_survives_memory_cache_clear(self):
        key = ("a" * 32, "book.epub", "/device", "/synced")
        mod.install_persistent_xpath_cache(self.parser)
        server = sys.modules["src.api.kosync_server"]
        file_key = mod.canonical_file_key(self.parser, "book.epub")
        mod._persist_pair(key, 100, 200, file_key)

        self.memory.clear()
        self.assertEqual(server._xpath_index_cache_get(key), (100, 200))
        self.assertEqual(self.memory[key], (100, 200))

    def test_get_path_put_is_not_persisted_without_file_stability_proof(self):
        key = ("a" * 32, "book.epub", "/device", "/synced")
        mod.install_persistent_xpath_cache(self.parser)
        server = sys.modules["src.api.kosync_server"]
        server._xpath_index_cache_put(key, 100, 200)
        time.sleep(0.05)

        with self.db.engine.begin() as conn:
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM kosync_xpath_order_cache WHERE key_hash = ?",
                (mod._pair_hash(key),),
            ).scalar_one()
        self.assertEqual(count, 0)
        self.assertEqual(self.memory[key], (100, 200))

    def test_failed_resolution_is_not_persisted(self):
        key = ("a" * 32, "book.epub", "/device", "/unresolved")
        file_key = mod.canonical_file_key(self.parser, "book.epub")
        mod._persist_pair(key, None, None, file_key)

        with self.db.engine.begin() as conn:
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM kosync_xpath_order_cache WHERE key_hash = ?",
                (mod._pair_hash(key),),
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_persistent_cache_is_bounded_per_document(self):
        file_key = mod.canonical_file_key(self.parser, "book.epub")
        doc_id = "a" * 32
        old_limit = mod._PERSISTED_MAX_PER_DOCUMENT
        mod._PERSISTED_MAX_PER_DOCUMENT = 3
        try:
            for idx in range(6):
                key = (doc_id, "book.epub", f"/device-{idx}", f"/synced-{idx}")
                mod._persist_pair(key, idx, idx + 100, file_key)
            with self.db.engine.begin() as conn:
                count = conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM kosync_xpath_order_cache WHERE document_hash = ?",
                    (doc_id,),
                ).scalar_one()
            self.assertEqual(count, 3)
        finally:
            mod._PERSISTED_MAX_PER_DOCUMENT = old_limit

    def test_persisted_pair_is_ignored_after_epub_replacement(self):
        key = ("a" * 32, "book.epub", "/device", "/synced")
        mod.install_persistent_xpath_cache(self.parser)
        server = sys.modules["src.api.kosync_server"]
        file_key = mod.canonical_file_key(self.parser, "book.epub")
        mod._persist_pair(key, 100, 200, file_key)
        self.memory.clear()

        time.sleep(0.002)
        self.path.write_bytes(b"changed-version-longer")
        os.utime(self.path, None)
        self.assertIsNone(server._xpath_index_cache_get(key))

    def test_bridge_write_prewarm_resolves_device_xpath_off_get_path(self):
        mod.install_persistent_xpath_cache(self.parser)
        self.db.document = SimpleNamespace(
            filename="book.epub",
            user_id=7,
            progress="/device",
        )
        self.db.user_progress = SimpleNamespace(progress="/device")
        book = SimpleNamespace(
            kosync_doc_id="a" * 32,
            ebook_filename="book.epub",
            original_ebook_filename=None,
        )
        file_key = mod.canonical_file_key(self.parser, "book.epub")
        old_user_id = mod._current_user_id
        mod._current_user_id = lambda: 7
        try:
            self.assertTrue(mod.prewarm_xpath_order_cache(book, self.parser, "/synced", 200, file_key))
            key = ("a" * 32, "book.epub", "/device", "/synced")
            deadline = time.time() + 1
            while key not in self.memory and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.memory.get(key), (100, 200))
            self.assertTrue(self._wait_for_persisted(mod._pair_hash(key)))
            self.assertIn(("book.epub", "/device"), self.parser.calls)
        finally:
            mod._current_user_id = old_user_id


if __name__ == "__main__":
    unittest.main()
