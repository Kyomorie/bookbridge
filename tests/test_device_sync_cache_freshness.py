"""Tests for KOReaderDeviceSyncService cache freshness and fallback behavior.

Covers _hosted_cache_expired and _resolve_source_path cache TTL logic:
- Fresh cached copy served without download
- Expired cached copy triggers revalidation
- Zero/negative TTL means never expire
- Malformed TTL falls back to default (360 minutes)
- Failed revalidation reuses stale cached copy
- No cache and failed download returns None
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.services.koreader_device_sync_service import KOReaderDeviceSyncService
from src.utils.cache_paths import safe_cache_path


class TestDeviceSyncCacheFreshness(unittest.TestCase):
    """Test cache TTL and fallback behavior in KOReaderDeviceSyncService."""

    def setUp(self):
        # Create temp directory for epub cache
        self.cache_temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.cache_temp_dir.name)

        # Save original TTL env value
        self.original_ttl = os.environ.get("DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES")

        # Ebook parser stub that always raises FileNotFoundError
        self.ebook_parser = MagicMock()
        self.ebook_parser.resolve_book_path.side_effect = FileNotFoundError("not found")

        # BookOrbit client mock - configured and returns content by default
        self.bookorbit_client = MagicMock()
        self.bookorbit_client.is_configured.return_value = True
        self.bookorbit_client.download_book.return_value = b"EPUBBYTES"

        # Other clients - not configured so their download helpers fail fast
        self.abs_client = MagicMock()
        self.abs_client.is_configured.return_value = False

        self.booklore_client = MagicMock()
        self.booklore_client.is_configured.return_value = False

        self.cwa_client = MagicMock()
        self.cwa_client.is_configured.return_value = False

        self.kavita_client = MagicMock()
        self.kavita_client.is_configured.return_value = False

        # Database service mock
        self.database_service = MagicMock()

        # Construct the service
        self.service = KOReaderDeviceSyncService(
            database_service=self.database_service,
            ebook_parser=self.ebook_parser,
            abs_client=self.abs_client,
            booklore_client=self.booklore_client,
            cwa_client=self.cwa_client,
            kavita_client=self.kavita_client,
            epub_cache_dir=self.cache_dir,
            bookorbit_client=self.bookorbit_client,
        )

        # Book stub with bookorbit source
        self.book = SimpleNamespace(
            ebook_source="bookorbit",
            ebook_source_id="42",
            abs_id="book-1",
            abs_title="Test Book",
            sync_mode="ebook_only",
        )

    def tearDown(self):
        # Restore original TTL env value exactly
        if self.original_ttl is None:
            os.environ.pop("DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES", None)
        else:
            os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = self.original_ttl

        self.cache_temp_dir.cleanup()

    def _write_cache_file(self, source_filename: str, content: bytes = b"CACHED_EPUB") -> Path:
        """Write a cache file using the same path logic as the service."""
        cache_path = safe_cache_path(self.cache_dir, source_filename)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(content)
        return cache_path

    def _backdate_mtime(self, path: Path, seconds_ago: float) -> None:
        """Set the file's mtime to `seconds_ago` seconds in the past."""
        past = time.time() - seconds_ago
        os.utime(path, (past, past))

    def test_fresh_cached_copy_is_served_without_download(self):
        """TTL 360, fresh cache file -> served without download."""
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "360"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename)

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, cache_path)
        self.bookorbit_client.download_book.assert_not_called()

    def test_expired_cached_copy_triggers_revalidation(self):
        """TTL 60, cache file backdated 7200s (2h) -> revalidation triggers download."""
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "60"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename)
        self._backdate_mtime(cache_path, 7200)  # 2 hours ago, > 60 min TTL

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, cache_path)
        self.bookorbit_client.download_book.assert_called_once_with("42")

    def test_zero_ttl_never_expires(self):
        """TTL 0, cache file backdated 30 days -> still served, no download."""
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "0"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename)
        self._backdate_mtime(cache_path, 30 * 24 * 3600)  # 30 days ago

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, cache_path)
        self.bookorbit_client.download_book.assert_not_called()

    def test_malformed_ttl_falls_back_to_default(self):
        """TTL 'abc', fresh cache file -> served (default 360 min applies)."""
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "abc"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename)

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, cache_path)
        self.bookorbit_client.download_book.assert_not_called()

    def test_failed_revalidation_reuses_stale_copy(self):
        """TTL 60, expired cache, download fails -> stale copy returned (not None)."""
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "60"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename, content=b"OLD")
        self._backdate_mtime(cache_path, 7200)  # Expired
        self.bookorbit_client.download_book.return_value = None  # Download fails

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, cache_path)
        self.assertEqual(cache_path.read_bytes(), b"OLD")
        self.bookorbit_client.download_book.assert_called_once()

    def test_failed_revalidation_backs_off_a_full_ttl(self):
        """A failing source must not be retried by every manifest rebuild.

        The prebuilder rebuilds every 60s, so without a backoff an unreachable or
        401ing source is re-downloaded every minute for as long as the copy stays
        expired (observed live as a CWA 401 storm with ERROR tracebacks).
        """
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "60"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename)
        self._backdate_mtime(cache_path, 7200)
        self.bookorbit_client.download_book.return_value = None

        self.service._resolve_source_path(self.book, source_filename)

        self.assertFalse(
            self.service._hosted_cache_expired(cache_path),
            "a failed refresh must push the next attempt out by a full TTL",
        )

        # A second immediate pass therefore attempts no further download.
        self.bookorbit_client.download_book.reset_mock()
        self.service._resolve_source_path(self.book, source_filename)
        self.bookorbit_client.download_book.assert_not_called()

    def test_destructive_download_cannot_clobber_the_cached_copy(self):
        """A download helper that deletes the path it is given must not lose the cache.

        The helpers write straight to the path handed to them, and a failure can leave
        it truncated or removed (observed live: CWA streamed a 401 over the cache file
        and deleted it). Revalidation must therefore download into a temp sibling and
        keep the previous copy intact when the refresh fails.
        """
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "60"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename, content=b"GOOD")
        self._backdate_mtime(cache_path, 7200)

        def destructive_download(book_id):
            # Simulate a helper that truncates/removes its target then fails.
            for candidate in self.cache_dir.iterdir():
                if candidate.name.endswith(".refresh"):
                    candidate.unlink()
            return None

        self.bookorbit_client.download_book.side_effect = destructive_download

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, cache_path)
        self.assertTrue(cache_path.exists(), "cached copy must survive a failed refresh")
        self.assertEqual(cache_path.read_bytes(), b"GOOD")

    def test_successful_revalidation_swaps_in_new_bytes(self):
        """A successful refresh replaces the cached copy atomically and leaves no temp."""
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "60"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename, content=b"OLD")
        self._backdate_mtime(cache_path, 7200)
        self.bookorbit_client.download_book.return_value = b"NEWBYTES"

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, cache_path)
        self.assertEqual(cache_path.read_bytes(), b"NEWBYTES")
        leftovers = [p.name for p in self.cache_dir.iterdir() if p.name.endswith(".refresh")]
        self.assertEqual(leftovers, [], "revalidation temp file must not be left behind")

    def test_no_cache_and_failed_download_returns_none(self):
        """No cache file, download fails -> returns None."""
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "60"
        source_filename = "test-book.epub"
        self.bookorbit_client.download_book.return_value = None

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertIsNone(result)
        self.bookorbit_client.download_book.assert_called_once_with("42")

    def test_resolver_returning_cached_copy_does_not_bypass_ttl(self):
        """A resolver that hands back the cache copy must not skip revalidation.

        EbookParser.resolve_book_path falls back to the epub cache directory for
        ordinary filenames, so it "resolves" hosted books to their cached copy. If
        that counted as a real library file, the TTL branch would be unreachable for
        exactly the hosted books it exists to refresh (caught in live verification).
        """
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "60"
        source_filename = "test-book.epub"

        cache_path = self._write_cache_file(source_filename)
        self._backdate_mtime(cache_path, 7200)

        # Resolver succeeds, returning the cached copy itself.
        self.ebook_parser.resolve_book_path.side_effect = None
        self.ebook_parser.resolve_book_path.return_value = str(cache_path)

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, cache_path)
        self.bookorbit_client.download_book.assert_called_once_with("42")

    def test_real_library_file_wins_and_skips_cache_logic(self):
        """A genuine library file outside the cache dir short-circuits, as before."""
        os.environ["DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES"] = "60"
        source_filename = "test-book.epub"

        # An expired cached copy exists, but the real library file must win.
        cache_path = self._write_cache_file(source_filename)
        self._backdate_mtime(cache_path, 7200)

        library_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, library_dir, True)
        library_file = library_dir / source_filename
        library_file.write_bytes(b"LIBRARY_COPY")

        self.ebook_parser.resolve_book_path.side_effect = None
        self.ebook_parser.resolve_book_path.return_value = str(library_file)

        result = self.service._resolve_source_path(self.book, source_filename)

        self.assertEqual(result, library_file)
        self.bookorbit_client.download_book.assert_not_called()


if __name__ == "__main__":
    unittest.main()