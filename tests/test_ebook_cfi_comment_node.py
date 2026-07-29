"""Regression test for the CFI resolver crash on lxml comment nodes (finding #711 / GitHub #341).

lxml comment (and processing-instruction) nodes have a *callable* ``.tag`` attribute, so the
old element-child filter ``hasattr(child, 'tag')`` let them into the CFI element list. A CFI
even-index step could then set ``current_element`` to a comment node; when the walk ends there,
``current_element.text_content()`` raises ``Input object is not an XML element: HtmlComment``
(a comment node has the inherited method but it is not callable on it) — logged as
``Error resolving CFI->index '...': ...`` and returning None instead of a position.

The fix restricts the filter to ``isinstance(child.tag, str)`` (real elements only). These tests
mock the spine content so the comment node is guaranteed to reach the resolver (the real EPUB
extraction may strip comments), and use a CFI that lands the final element step on the comment,
so the crash path is actually exercised — the test fails if the fix is reverted.
"""
import shutil
import tempfile
import unittest
from unittest.mock import patch

from lxml import etree

from src.utils.ebook_utils import EbookParser

# body children (elements): [p, div, p]; div children: [<comment>, p#target]
SPINE_CONTENT = (
    "<html><body>"
    "<p>First paragraph before the container element goes here.</p>"
    "<div id=\"container\"><!-- leading nav comment -->"
    "<p id=\"target\">Target paragraph inside the container element.</p></div>"
    "<p>Paragraph after the container.</p>"
    "</body></html>"
)


class TestCFICommentNode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.parser = EbookParser(books_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _resolve(self, cfi: str):
        spine_map = [{"spine_index": 1, "start": 0, "content": SPINE_CONTENT}]
        with patch.object(self.parser, "resolve_book_path", return_value="dummy.epub"), \
                patch.object(self.parser, "extract_text_and_map", return_value=("full text", spine_map)):
            return self.parser.resolve_cfi_to_index("book.epub", cfi)

    def test_cfi_landing_on_comment_node_does_not_crash(self):
        """CFI whose final element step lands on the div's first child (a comment pre-fix)
        must resolve, not crash.

        /6/2! -> spine_index 1; element steps /2/4/2 walk html -> body -> div -> div's first
        child. Pre-fix that first child is the comment node, and the resolver's
        current_element.text_content() then raises 'not an XML element: HtmlComment' -> None.
        Post-fix the comment is excluded, so /2 lands on <p id="target"> and the resolver
        returns a valid offset.
        """
        result = self._resolve("epubcfi(/6/2!/2/4/2)")
        self.assertIsNotNone(result, "resolver returned None (comment-node crash not fixed)")
        self.assertGreaterEqual(result, 0)

    def test_comment_node_tag_is_callable_root_cause(self):
        """Document the root cause + the fix predicate: comment .tag is callable, element .tag is str."""
        div = etree.HTML("<div><!-- c --><p>text</p></div>").find(".//div")
        children = list(div)
        self.assertEqual(len(children), 2)
        comment, para = children
        self.assertTrue(callable(comment.tag))          # the old hasattr filter let this through
        self.assertIsInstance(para.tag, str)
        kept = [c for c in children if isinstance(c.tag, str)]
        self.assertEqual(kept, [para])                  # the fix predicate keeps only real elements


if __name__ == "__main__":
    unittest.main()
