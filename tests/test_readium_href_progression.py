#!/usr/bin/env python3
"""Regression tests: Readium locators that carry no fragment.

Live-measured on 2026-08-16 against a real Audiobookshelf ebook item. The reader
stored:

    {"href":"OEBPS/cPB.xhtml","locations":{"position":53,"progression":0.2},
     "title":"Chapter 8","type":"application/xhtml+xml"}

with ebookProgress 0.14685714. There is no ``fragments`` key — real ABS-ecosystem
locators never carry one — so ``resolve_locator_id`` (which requires a fragment id)
could not fire and text resolution fell through to the whole-book percentage. In that
book the two land 6725 characters apart, 0.99% of the book, in different scenes.

``char_len`` is the chapter's extracted-text length; ``content`` is raw HTML and runs
~1.17x longer on real books. Mixing the two spaces is what makes the naive version of
this calculation wrong, so the tests below pin that too.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.models import Book
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.utils.ebook_utils import EbookParser


def _parser_with(full_text, spine_map):
    parser = EbookParser(books_dir=".")
    parser.resolve_book_path = lambda filename: "/fake/book.epub"
    parser.extract_text_and_map = lambda path: (full_text, spine_map)
    return parser


class TestResolveHrefProgression(unittest.TestCase):
    """Arithmetic of href + in-chapter progression."""

    def setUp(self):
        self.full_text = ("A" * 100) + ("B" * 100) + ("C" * 100)
        # `content` is deliberately far longer than `char_len`: using it for the
        # offset maths would push the answer out of the chapter entirely.
        self.spine = [
            {"start": 0, "end": 100, "char_len": 100, "spine_index": 1,
             "href": "OEBPS/c1.xhtml", "content": b"<html>" + b"x" * 1000},
            {"start": 100, "end": 200, "char_len": 100, "spine_index": 2,
             "href": "OEBPS/c2.xhtml", "content": b"<html>" + b"x" * 1000},
            {"start": 200, "end": 300, "char_len": 100, "spine_index": 3,
             "href": "OEBPS/c3.xhtml", "content": b"<html>" + b"x" * 1000},
        ]
        self.parser = _parser_with(self.full_text, self.spine)

    def test_midway_through_a_chapter(self):
        txt = self.parser.resolve_href_progression("book.epub", "OEBPS/c2.xhtml", 0.5)
        # char 150: 50 B's remain, then the C chapter.
        self.assertEqual(txt, ("B" * 50) + ("C" * 100))

    def test_chapter_start_when_progression_is_zero(self):
        txt = self.parser.resolve_href_progression("book.epub", "OEBPS/c2.xhtml", 0.0)
        self.assertEqual(txt, ("B" * 100) + ("C" * 100))

    def test_missing_progression_falls_back_to_chapter_start(self):
        """A locator may omit progression; the chapter start still beats a book %."""
        txt = self.parser.resolve_href_progression("book.epub", "OEBPS/c2.xhtml", None)
        self.assertEqual(txt, ("B" * 100) + ("C" * 100))

    def test_uses_char_len_not_content_length(self):
        """content is raw HTML (~10x here); using it would leave the chapter."""
        txt = self.parser.resolve_href_progression("book.epub", "OEBPS/c1.xhtml", 0.5)
        self.assertTrue(txt.startswith("A" * 50), f"landed outside chapter 1: {txt[:40]!r}")

    def test_progression_is_clamped(self):
        self.assertTrue(
            self.parser.resolve_href_progression("book.epub", "OEBPS/c1.xhtml", 5.0)
            .startswith("B")
        )
        self.assertTrue(
            self.parser.resolve_href_progression("book.epub", "OEBPS/c2.xhtml", -3.0)
            .startswith("B")
        )

    def test_unknown_href_returns_none(self):
        self.assertIsNone(
            self.parser.resolve_href_progression("book.epub", "OEBPS/nope.xhtml", 0.5)
        )

    def test_missing_href_returns_none(self):
        self.assertIsNone(self.parser.resolve_href_progression("book.epub", "", 0.5))
        self.assertIsNone(self.parser.resolve_href_progression("book.epub", None, 0.5))

    def test_non_numeric_progression_degrades_to_chapter_start(self):
        txt = self.parser.resolve_href_progression("book.epub", "OEBPS/c2.xhtml", "junk")
        self.assertEqual(txt, ("B" * 100) + ("C" * 100))


class TestMeasuredRealWorldPosition(unittest.TestCase):
    """The exact live case, with the real numbers from the ABS item."""

    TOTAL = 680211
    CH_START = 90479
    CH_LEN = 13446
    PROGRESSION = 0.2
    BOOK_PCT = 0.14685714285714285
    HREF_CHAR = 93168          # start + 0.2 * char_len
    PCT_CHAR = 99893           # BOOK_PCT * TOTAL

    def setUp(self):
        chars = ["."] * self.TOTAL
        chars[self.HREF_CHAR:self.HREF_CHAR + 11] = list("HREF_TARGET")
        chars[self.PCT_CHAR:self.PCT_CHAR + 10] = list("PCT_TARGET")
        self.full_text = "".join(chars)
        self.spine = [{
            "start": self.CH_START,
            "end": self.CH_START + self.CH_LEN,
            "char_len": self.CH_LEN,
            "spine_index": 13,
            "href": "OEBPS/cPB.xhtml",
            "content": b"<html>" + b"x" * 15754,   # the real HTML length: 1.17x
        }]
        self.parser = _parser_with(self.full_text, self.spine)

    def test_href_progression_lands_on_the_reader_position(self):
        txt = self.parser.resolve_href_progression("book.epub", "OEBPS/cPB.xhtml", self.PROGRESSION)
        self.assertTrue(
            txt.startswith("HREF_TARGET"),
            "href + progression must resolve to where the reader actually is",
        )

    def test_the_percentage_fallback_lands_somewhere_else(self):
        """Documents the bug this replaces: 6725 chars away, a different scene."""
        txt = self.parser.get_text_at_percentage("book.epub", self.BOOK_PCT)
        self.assertIn("PCT_TARGET", txt)
        self.assertNotIn("HREF_TARGET", txt)
        self.assertEqual(self.PCT_CHAR - self.HREF_CHAR, 6725)


class TestABSEbookPrefersHrefOverPercentage(unittest.TestCase):
    """The client wires the new resolver in ahead of the percentage fallback."""

    def setUp(self):
        self.parser = MagicMock()
        self.client = ABSEbookSyncClient(MagicMock(), self.parser)
        self.book = Book(abs_id="abs-1", abs_title="T", ebook_filename="book.epub")

    def _state(self, **current):
        state = MagicMock()
        state.current = current
        return state

    def test_fragmentless_locator_uses_href_progression(self):
        self.parser.resolve_locator_id.return_value = None
        self.parser.resolve_href_progression.return_value = "text at the real position"

        txt = self.client.get_text_from_current_state(
            self.book,
            self._state(cfi="", href="OEBPS/cPB.xhtml", chapter_progress=0.2, pct=0.1468),
        )

        self.assertEqual(txt, "text at the real position")
        self.parser.resolve_href_progression.assert_called_once_with(
            "book.epub", "OEBPS/cPB.xhtml", 0.2
        )
        self.parser.get_text_at_percentage.assert_not_called()

    def test_fragment_still_wins_when_present(self):
        """Storyteller-style locators with a fragment must be unaffected."""
        self.parser.resolve_locator_id.return_value = "fragment-anchored text"

        txt = self.client.get_text_from_current_state(
            self.book,
            self._state(cfi="", href="OEBPS/cPB.xhtml", frag="f1",
                        chapter_progress=0.2, pct=0.1468),
        )

        self.assertEqual(txt, "fragment-anchored text")
        self.parser.resolve_href_progression.assert_not_called()

    def test_percentage_remains_the_last_resort(self):
        self.parser.resolve_locator_id.return_value = None
        self.parser.resolve_href_progression.return_value = None
        self.parser.get_text_at_percentage.return_value = "percentage text"

        txt = self.client.get_text_from_current_state(
            self.book,
            self._state(cfi="", href="OEBPS/unknown.xhtml", chapter_progress=0.2, pct=0.1468),
        )

        self.assertEqual(txt, "percentage text")

    def test_cfi_readers_are_untouched(self):
        self.parser.get_text_around_cfi.return_value = "cfi text"

        txt = self.client.get_text_from_current_state(
            self.book, self._state(cfi="epubcfi(/6/14!/4/2/1:0)", pct=0.5)
        )

        self.assertEqual(txt, "cfi text")
        self.parser.resolve_href_progression.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
