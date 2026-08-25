"""Regression tests: a client-supplied local ebook path must stay inside the library.

Forge accepts a "Local File" text source whose path travels in the request
payload. Before the containment fix an authenticated user could name any file in
the container and have its bytes staged as an EPUB and uploaded to Storyteller —
an arbitrary local-file read/exfiltration path. These tests fail with the fix
reverted.
"""

import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.forge_service import ForgeService
from src.utils.cache_paths import safe_library_path, is_plain_basename


class TestSafeLibraryPath(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.books = self.tmp / "books"
        self.books.mkdir()
        self.extra = self.tmp / "extra"
        self.extra.mkdir()
        self.secret = self.tmp / "database.db"
        self.secret.write_bytes(b"SECRET")
        self.legit = self.books / "real.epub"
        self.legit.write_bytes(b"EPUB")
        self._env = {k: os.environ.get(k) for k in ("BOOKS_DIR", "DATA_DIR", "EXTRA_EBOOK_DIRS")}
        os.environ["BOOKS_DIR"] = str(self.books)
        os.environ["DATA_DIR"] = str(self.tmp / "data")
        os.environ.pop("EXTRA_EBOOK_DIRS", None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_accepts_path_inside_books_dir(self):
        self.assertEqual(safe_library_path(str(self.legit)), self.legit.resolve())

    def test_rejects_path_outside_library_roots(self):
        self.assertIsNone(safe_library_path(str(self.secret)))

    def test_rejects_traversal_out_of_books_dir(self):
        self.assertIsNone(safe_library_path(str(self.books / ".." / "database.db")))

    def test_rejects_absolute_system_path(self):
        self.assertIsNone(safe_library_path("/etc/passwd"))

    def test_rejects_empty(self):
        self.assertIsNone(safe_library_path(""))
        self.assertIsNone(safe_library_path(None))

    def test_accepts_path_inside_extra_ebook_dirs(self):
        other = self.extra / "other.epub"
        other.write_bytes(b"EPUB")
        os.environ["EXTRA_EBOOK_DIRS"] = str(self.extra)
        self.assertEqual(safe_library_path(str(other)), other.resolve())

    def test_accepts_path_inside_epub_cache(self):
        cache = Path(os.environ["DATA_DIR"]) / "epub_cache"
        cache.mkdir(parents=True)
        cached = cache / "storyteller_x.epub"
        cached.write_bytes(b"EPUB")
        self.assertEqual(safe_library_path(str(cached)), cached.resolve())

    def test_extra_dirs_read_per_call(self):
        """A settings change applies without a restart (no import-time freeze)."""
        other = self.extra / "other.epub"
        other.write_bytes(b"EPUB")
        self.assertIsNone(safe_library_path(str(other)))
        os.environ["EXTRA_EBOOK_DIRS"] = str(self.extra)
        self.assertIsNotNone(safe_library_path(str(other)))


class TestPlainBasename(unittest.TestCase):
    def test_accepts_bare_filename(self):
        self.assertTrue(is_plain_basename("book.epub"))

    def test_rejects_directory_components(self):
        for bad in ("../database.db", "../../etc/passwd", "sub/book.epub",
                    r"..\database.db", r"sub\book.epub", "/etc/passwd", "", ".", ".."):
            with self.subTest(bad=bad):
                self.assertFalse(is_plain_basename(bad))


class TestForgeLocalSourceContainment(unittest.TestCase):
    """The forge worker must not stage a file from outside the library roots."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.books = self.tmp / "books"
        self.books.mkdir()
        self.secret = self.tmp / "database.db"
        self.secret.write_bytes(b"SUPER-SECRET-SQLITE-BYTES")
        self.legit = self.books / "real.epub"
        self.legit.write_bytes(b"EPUB-BYTES")
        self._env = {k: os.environ.get(k) for k in ("BOOKS_DIR", "DATA_DIR", "EXTRA_EBOOK_DIRS")}
        os.environ["BOOKS_DIR"] = str(self.books)
        os.environ["DATA_DIR"] = str(self.tmp / "data")
        os.environ.pop("EXTRA_EBOOK_DIRS", None)

        self.service = ForgeService(
            database_service=MagicMock(), abs_client=MagicMock(),
            booklore_client=MagicMock(), storyteller_client=MagicMock(),
            library_service=MagicMock(), ebook_parser=MagicMock(),
            transcriber=MagicMock(), alignment_service=MagicMock(),
            bookorbit_client=MagicMock(),
        )
        self.service.storyteller_client.is_configured.return_value = True

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _staged_source_for(self, path):
        """Run the forge worker and report which file it handed to staging."""
        staged = {}
        real_stage = ForgeService._stage_local_file

        def spy(inner_self, src, dest, mode, context):
            staged['src'] = str(src)
            real_stage(inner_self, src, dest, mode, context)
            raise RuntimeError("stop after staging")

        with patch.object(ForgeService, "_copy_audio_files", return_value=True), \
             patch.object(ForgeService, "_stage_local_file", spy):
            try:
                self.service._forge_background_task(
                    "abs1", {"source": "Local File", "path": path}, "Title", "Author"
                )
            except Exception:
                pass
        return staged.get('src')

    def test_forge_refuses_file_outside_books_dir(self):
        self.assertIsNone(self._staged_source_for(str(self.secret)))

    def test_forge_refuses_traversal_out_of_books_dir(self):
        self.assertIsNone(self._staged_source_for(str(self.books / ".." / "database.db")))

    def test_forge_still_stages_a_library_file(self):
        self.assertEqual(self._staged_source_for(str(self.legit)), str(self.legit.resolve()))

    def test_local_source_path_helper_rejects_out_of_tree(self):
        self.assertIsNone(
            self.service._local_source_path({"path": str(self.secret)}, "Forge")
        )

    def test_local_source_path_helper_accepts_library_file(self):
        self.assertEqual(
            self.service._local_source_path({"path": str(self.legit)}, "Forge"),
            self.legit.resolve(),
        )

    def test_local_source_path_helper_handles_missing_and_non_dict(self):
        self.assertIsNone(self.service._local_source_path({}, "Forge"))
        self.assertIsNone(self.service._local_source_path(None, "Forge"))


if __name__ == "__main__":
    unittest.main()


class TestNonLocalSourcesUnaffected(unittest.TestCase):
    """A provider download_url in `path` is not a local source and is not staged."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.books = self.tmp / "books"
        self.books.mkdir()
        self._env = {k: os.environ.get(k) for k in ("BOOKS_DIR", "DATA_DIR")}
        os.environ["BOOKS_DIR"] = str(self.books)
        os.environ["DATA_DIR"] = str(self.tmp / "data")
        self.service = ForgeService(
            database_service=MagicMock(), abs_client=MagicMock(),
            booklore_client=MagicMock(), storyteller_client=MagicMock(),
            library_service=MagicMock(), ebook_parser=MagicMock(),
            transcriber=MagicMock(), alignment_service=MagicMock(),
            bookorbit_client=MagicMock(),
        )

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_download_url_is_not_treated_as_a_local_source(self):
        self.assertIsNone(
            self.service._local_source_path(
                {"source": "CWA", "path": "https://cwa.example/download/12"}, "Forge"
            )
        )


class TestReportedEdgeCases(unittest.TestCase):
    """The reporter's remaining cases: symlink escape and non-regular files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.books = self.tmp / "books"
        self.books.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "database.db"
        self.secret.write_bytes(b"SECRET")
        self._env = {k: os.environ.get(k) for k in ("BOOKS_DIR", "DATA_DIR", "EXTRA_EBOOK_DIRS")}
        os.environ["BOOKS_DIR"] = str(self.books)
        os.environ["DATA_DIR"] = str(self.tmp / "data")
        os.environ.pop("EXTRA_EBOOK_DIRS", None)
        self.service = ForgeService(
            database_service=MagicMock(), abs_client=MagicMock(),
            booklore_client=MagicMock(), storyteller_client=MagicMock(),
            library_service=MagicMock(), ebook_parser=MagicMock(),
            transcriber=MagicMock(), alignment_service=MagicMock(),
            bookorbit_client=MagicMock(),
        )

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_symlink(self, link, target):
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable in this environment: {exc}")

    def test_symlink_inside_root_pointing_outside_is_rejected(self):
        link = self.books / "innocent.epub"
        self._make_symlink(link, self.secret)
        self.assertIsNone(safe_library_path(str(link)))
        self.assertIsNone(self.service._local_source_path({"path": str(link)}, "Forge"))

    def test_symlink_inside_root_pointing_inside_is_allowed(self):
        real = self.books / "real.epub"
        real.write_bytes(b"EPUB")
        link = self.books / "alias.epub"
        self._make_symlink(link, real)
        self.assertEqual(safe_library_path(str(link)), real.resolve())

    def test_directory_inside_root_is_rejected_as_a_source(self):
        subdir = self.books / "a_folder"
        subdir.mkdir()
        # Contained, but not a stageable file.
        self.assertIsNotNone(safe_library_path(str(subdir)))
        self.assertIsNone(self.service._local_source_path({"path": str(subdir)}, "Forge"))

    def test_missing_path_still_reaches_the_callers_not_found_diagnostic(self):
        """A contained-but-absent path is returned so the existing log line stands."""
        missing = self.books / "gone.epub"
        self.assertEqual(
            self.service._local_source_path({"path": str(missing)}, "Forge"),
            missing.resolve(),
        )
