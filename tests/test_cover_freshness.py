"""Tests for cover staleness after an ebook is rewritten.

The extracted ebook cover is written once to /data/covers/<hash>.jpg and served
forever, and the dashboard prefers it over the live audio cover — so replacing a
cover inside an EPUB never showed up.
"""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.web_server import _extracted_cover_is_stale


class TestExtractedCoverFreshness(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cover = self.root / "cover.jpg"
        self.source = self.root / "book.epub"

    def _write(self, path: Path, mtime: float) -> None:
        path.write_bytes(b"x")
        os.utime(path, (mtime, mtime))

    def test_stale_when_ebook_is_newer_than_the_cover(self):
        now = time.time()
        self._write(self.cover, now - 3600)
        self._write(self.source, now)
        self.assertTrue(_extracted_cover_is_stale(self.cover, self.source))

    def test_fresh_when_cover_is_newer(self):
        now = time.time()
        self._write(self.source, now - 3600)
        self._write(self.cover, now)
        self.assertFalse(_extracted_cover_is_stale(self.cover, self.source))

    def test_missing_source_is_not_stale(self):
        self._write(self.cover, time.time())
        self.assertFalse(_extracted_cover_is_stale(self.cover, self.root / "gone.epub"))

    def test_none_source_is_not_stale(self):
        self._write(self.cover, time.time())
        self.assertFalse(_extracted_cover_is_stale(self.cover, None))

    def test_missing_cover_is_not_stale(self):
        self._write(self.source, time.time())
        self.assertFalse(_extracted_cover_is_stale(self.root / "gone.jpg", self.source))


if __name__ == "__main__":
    unittest.main()
