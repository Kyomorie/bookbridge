import unittest
from unittest.mock import Mock

from lxml import html

from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.utils.ebook_utils import EbookParser


class TestKoSyncChapterFallback(unittest.TestCase):
    def setUp(self):
        self.parser = EbookParser("/tmp")

    def test_nested_inline_text_preserves_real_parent_structure(self):
        content = b"<html><body><div><p><span>Readable text</span></p></div></body></html>"

        xpath = self.parser._build_sentence_level_chapter_fallback_xpath(content, 56)

        self.assertEqual(xpath, "/body/DocFragment[56]/body/div/p.0")

    def test_direct_text_keeps_existing_text_node_behavior(self):
        content = b"<html><body><div><p>Readable text</p></div></body></html>"

        xpath = self.parser._build_sentence_level_chapter_fallback_xpath(content, 7)

        self.assertEqual(xpath, "/body/DocFragment[7]/body/div/p/text().0")

    def test_multiple_paragraphs_keep_positional_index(self):
        content = (
            b"<html><body><div>"
            b"<p><span></span></p>"
            b"<p><span>Readable text</span></p>"
            b"</div></body></html>"
        )

        xpath = self.parser._build_sentence_level_chapter_fallback_xpath(content, 9)

        self.assertEqual(xpath, "/body/DocFragment[9]/body/div/p[2].0")

    def test_crengine_safe_path_does_not_invent_direct_text_node(self):
        content = b"<html><body><div><p><span>Readable text</span></p></div></body></html>"
        tree = html.fromstring(content)
        paragraph = tree.xpath("//p")[0]

        xpath = self.parser._build_crengine_safe_text_xpath(paragraph, 56, content)

        self.assertEqual(xpath, "/body/DocFragment[56]/body/div/p.0")

    def test_sanitizer_preserves_structural_parent_chain(self):
        content = b"<html><body><div><p><span>Readable text</span></p></div></body></html>"
        xpath = self.parser._build_sentence_level_chapter_fallback_xpath(content, 56)
        client = KoSyncSyncClient(Mock(), self.parser)

        sanitized = client._sanitize_kosync_xpath(xpath, 0.798)

        self.assertEqual(sanitized, "/body/DocFragment[56]/body/div/p.0")

    def test_unreadable_or_malformed_content_does_not_invent_locator(self):
        self.assertIsNone(
            self.parser._build_sentence_level_chapter_fallback_xpath(
                b"<html><body><div></div></body></html>", 3
            )
        )
        self.assertIsNone(
            self.parser._build_sentence_level_chapter_fallback_xpath(b"", 3)
        )


if __name__ == "__main__":
    unittest.main()
