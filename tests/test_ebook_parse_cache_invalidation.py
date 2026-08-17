"""Tests that EbookParser's parse cache notices a file replaced in place.

The cache was keyed by path alone inside a process-lifetime Singleton, so editing
an EPUB's metadata (which rewrites the file at the same path) kept serving the old
text and spine map until eviction or a container restart.
"""

import os
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from src.utils.ebook_utils import EbookParser


def _write_epub(path: Path, body_text: str) -> None:
    """Write a minimal but real EPUB containing body_text."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">test-book</dc:identifier>'
            "<dc:title>Test</dc:title></metadata>"
            '<manifest><item id="c1" href="c1.xhtml" '
            'media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        zf.writestr(
            "c1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<body><p>{body_text}</p></body></html>",
        )


class TestParseCacheInvalidation(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.books_dir = Path(self.tmp.name)
        self.parser = EbookParser(self.books_dir, epub_cache_dir=self.books_dir / "cache")
        self.epub = self.books_dir / "book.epub"

    def test_cache_key_changes_with_content(self):
        _write_epub(self.epub, "ORIGINAL")
        first = self.parser._file_cache_key(self.epub)

        time.sleep(0.01)
        _write_epub(self.epub, "REPLACED WITH LONGER TEXT")
        second = self.parser._file_cache_key(self.epub)

        self.assertNotEqual(first, second)

    def test_cache_key_stable_for_untouched_file(self):
        _write_epub(self.epub, "ORIGINAL")
        self.assertEqual(
            self.parser._file_cache_key(self.epub),
            self.parser._file_cache_key(self.epub),
        )

    def test_missing_file_falls_back_to_path(self):
        missing = self.books_dir / "gone.epub"
        self.assertEqual(self.parser._file_cache_key(missing), str(missing))

    def test_replaced_file_is_reparsed_without_restart(self):
        _write_epub(self.epub, "ORIGINAL CONTENT HERE")
        text_before, _ = self.parser.extract_text_and_map(self.epub)
        self.assertIn("ORIGINAL", text_before)

        # Same path, new bytes - exactly what a metadata edit produces.
        time.sleep(0.01)
        _write_epub(self.epub, "COMPLETELY DIFFERENT CONTENT")
        text_after, _ = self.parser.extract_text_and_map(self.epub)

        self.assertIn("DIFFERENT", text_after)
        self.assertNotIn("ORIGINAL", text_after)

    def test_unchanged_file_still_hits_the_cache(self):
        _write_epub(self.epub, "STABLE CONTENT")
        first_text, _ = self.parser.extract_text_and_map(self.epub)

        # Removing the file entirely: a cache hit must still answer, proving the
        # second call never re-read from disk.
        cached = self.parser.cache.get(self.parser._file_cache_key(self.epub))
        self.assertIsNotNone(cached)
        second_text, _ = self.parser.extract_text_and_map(self.epub)
        self.assertEqual(first_text, second_text)


if __name__ == "__main__":
    unittest.main()
