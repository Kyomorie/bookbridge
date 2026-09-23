import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT

from src.services.alignment_service import AlignmentService
from src.utils.ebook_utils import EbookParser


class TestEbookInlineTextExtraction(unittest.TestCase):
    def setUp(self):
        self.parser = EbookParser(books_dir=".")

    def _extract(self, html_content: str) -> str:
        soup = BeautifulSoup(html_content, "html.parser")
        return self.parser._extract_text_from_soup(soup)

    def test_bionic_inline_markup_joins_word_fragments(self):
        html_content = "<html><body><h1><b>T</b>he <b>Dung</b>eon</h1><p>Next paragraph.</p></body></html>"
        soup = BeautifulSoup(html_content, "html.parser")
        extracted = self._extract(html_content)

        self.assertEqual(extracted.replace(EbookParser.INLINE_TEXT_JOINER, ""), "The Dungeon Next paragraph.")
        self.assertGreater(extracted.count(EbookParser.INLINE_TEXT_JOINER), 0)
        self.assertEqual(
            len(extracted),
            len(soup.get_text(separator=" ", strip=True)),
        )

    def test_normal_bold_sections_keep_literal_word_boundaries(self):
        html_content = (
            "<html><body><p>This is <b>bold text</b> and "
            "<strong>more bold text</strong>.</p><p>Next paragraph.</p></body></html>"
        )

        extracted = self._extract(html_content)

        self.assertEqual(
            extracted.replace(EbookParser.INLINE_TEXT_JOINER, ""),
            "This is bold text and more bold text. Next paragraph.",
        )

    def test_extract_text_and_map_uses_inline_aware_text(self):
        content = b"<html><body><h1><b>T</b>he <b>Dung</b>eon.</h1></body></html>"
        item = SimpleNamespace(
            get_type=lambda: ITEM_DOCUMENT,
            get_content=lambda: content,
            get_name=lambda: "chapter.xhtml",
        )
        book = SimpleNamespace(
            spine=[("chapter", None)],
            get_item_with_id=lambda _item_id: item,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            epub_path = Path(temp_dir) / "book.epub"
            epub_path.write_bytes(b"fixture")
            self.parser._build_href_resolver = lambda _path: lambda name: name
            with patch("src.utils.ebook_utils.epub.read_epub", return_value=book):
                extracted, spine_map = self.parser.extract_text_and_map(epub_path)

        self.assertEqual(extracted.replace(EbookParser.INLINE_TEXT_JOINER, ""), "The Dungeon.")
        self.assertEqual(spine_map[0]["char_len"], len(extracted))

    def test_non_content_script_and_style_text_is_ignored(self):
        html_content = (
            "<html><head><style>.hidden { color: red; }</style>"
            "<script>alert('ignored')</script></head><body><p>Text.</p></body></html>"
        )

        extracted = self._extract(html_content)

        self.assertEqual(extracted, "Text.")

    def test_block_boundaries_still_create_spaces(self):
        html_content = "<html><body><p>First.</p><p>Second.</p></body></html>"

        extracted = self._extract(html_content)

        self.assertEqual(extracted, "First. Second.")

    def test_content_guard_joins_inline_fragments_before_matching(self):
        visible_text = " ".join(f"word{i}" for i in range(200))
        fragmented_text = visible_text.replace("word", f"wo{EbookParser.INLINE_TEXT_JOINER}rd")
        service = AlignmentService.__new__(AlignmentService)
        service.ollama_client = None

        with patch.dict(
            os.environ,
            {
                "OLLAMA_ALIGN_CONTENT_GUARD": "true",
                "CONTENT_MATCH_GUARD": "true",
                "CONTENT_MATCH_MIN_OVERLAP": "0.15",
            },
        ):
            self.assertTrue(
                service._verify_content_match(
                    [{"start": 0.0, "end": 1.0, "text": visible_text}],
                    fragmented_text,
                    abs_id="inline-fixture",
                )
            )


if __name__ == "__main__":
    unittest.main()
