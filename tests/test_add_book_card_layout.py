"""Issue #381 — a long title hid the source badge on the Add Book cards.

The reporter had the same book in ABS, BookOrbit and CWA and could not tell the
candidate cards apart, because the badge naming the source was clipped away. Sort
order is not stable either, so position was no guide.

Mechanism: `.resource-card` is a flex column with `aspect-ratio: 1/1` and
`overflow: hidden`. Per the flexbox spec, `overflow: hidden` makes the item's
automatic minimum size resolve to zero, so the square could not grow — a title
long enough to overflow pushed the icon off the top and the badge off the bottom,
and both were clipped.

The fix has four parts, and removing any one of them brings the bug back, so each
is pinned here:

1. `min-height: min-content` on the card, so it grows instead of clipping.
2. A line clamp on the title, so one absurd title cannot dominate the grid.
3. A line clamp on the subtitle, for the same reason.
4. `flex-shrink: 0` on the title, subtitle and badge, so the flex parent cannot
   squeeze the clamped boxes and cut a line in half.

CSS cannot be executed here, so these are structural assertions. The fix itself
was verified by rendering the real card CSS in headless Edge before and after.
"""

import re
import unittest
from pathlib import Path

_ADD_BOOK = Path(__file__).resolve().parents[1] / "templates" / "add_book.html"


def _rule(selector: str) -> str:
    """Return the declaration block for a top-level CSS rule in add_book.html."""
    source = _ADD_BOOK.read_text(encoding="utf-8")
    match = re.search(
        r"(?m)^\s*" + re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", source
    )
    if not match:
        raise AssertionError(f"CSS rule {selector} not found in add_book.html")
    return match.group("body")


class AddBookCardLayoutTests(unittest.TestCase):
    def test_card_can_grow_instead_of_clipping_its_contents(self):
        body = _rule(".resource-card")

        self.assertIn("aspect-ratio: 1/1", body, "square look is intentional")
        self.assertRegex(
            body,
            r"min-height:\s*min-content",
            "without min-height the overflow:hidden square clips its own badge",
        )

    def test_title_is_line_clamped(self):
        body = _rule(".resource-title")

        self.assertRegex(body, r"-webkit-line-clamp:\s*\d+")
        self.assertIn("-webkit-box-orient: vertical", body)
        self.assertRegex(body, r"flex-shrink:\s*0")

    def test_subtitle_is_line_clamped(self):
        body = _rule(".resource-subtitle")

        self.assertRegex(body, r"-webkit-line-clamp:\s*\d+")
        self.assertIn("-webkit-box-orient: vertical", body)
        self.assertRegex(body, r"flex-shrink:\s*0")

    def test_source_badge_never_shrinks(self):
        """The badge is the whole point of the card for a multi-source library."""
        body = _rule(".source-badge")

        self.assertRegex(body, r"flex-shrink:\s*0")

    def test_every_ebook_card_still_renders_its_source(self):
        """Guard the markup as well as the CSS — a badge that is not emitted
        cannot be un-clipped."""
        source = _ADD_BOOK.read_text(encoding="utf-8")

        self.assertIn('<span class="source-badge {{ badge_cls }}">{{ eb.source }}</span>', source)

    def test_full_title_stays_available_on_hover(self):
        """Clamping is only acceptable because the untruncated title is kept."""
        source = _ADD_BOOK.read_text(encoding="utf-8")

        self.assertIn('<div class="resource-title" title="{{ eb.display_name }}">', source)


if __name__ == "__main__":
    unittest.main()
