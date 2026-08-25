"""Issue #381 - a long title hid the source badge on the Add Book cards.

The reporter had the same book in ABS, BookOrbit and CWA and could not tell the
candidate cards apart, because the badge naming the source was clipped away. Sort
order is not stable either, so position was no guide.

Mechanism: `.resource-card` is a centred flex column, and `aspect-ratio: 1/1`
pinned its height to its width. Content taller than that square was centred and
sliced at *both* edges - the icon off the top, the source badge off the bottom.

The first attempt at a fix added `min-height: min-content`, which was not enough:
a line-clamped `-webkit-box` contributes almost nothing to a min-content height,
so the card grew by a few pixels and went on clipping its badge (measured in
headless Edge on the reporter's book: card 162px, content 173px). The card only
stops clipping once nothing pins its height, so `aspect-ratio` is gone and a plain
`min-height` floor keeps a short-title card square.

The badges then moved into their own row at the top of the card, so the source is
in the same place on every candidate - which is what the reporter asked for - and
the selection tick moved to the bottom corner so it cannot cover them.

CSS cannot be executed here, so these are structural assertions. The fix itself
was verified by rendering the real card CSS in headless Edge before and after.
"""

import re
import unittest
from pathlib import Path

_ADD_BOOK = Path(__file__).resolve().parents[1] / "templates" / "add_book.html"


def _source() -> str:
    return _ADD_BOOK.read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """Return the declaration block for a top-level CSS rule, comments stripped."""
    match = re.search(
        r"(?m)^\s*" + re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", _source()
    )
    if not match:
        raise AssertionError(f"CSS rule {selector} not found in add_book.html")
    return re.sub(r"/\*.*?\*/", "", match.group("body"), flags=re.S)


_CARD_OPEN = '<div class="resource-card'


def _card_block(marker: str) -> str:
    """Return the markup of the .resource-card whose opening tag contains `marker`."""
    source = _source()
    start = source.rindex(_CARD_OPEN, 0, source.index(marker))
    end = source.find(_CARD_OPEN, start + len(_CARD_OPEN))
    return source[start:end if end != -1 else len(source)]


class AddBookCardLayoutTests(unittest.TestCase):
    def test_card_height_is_never_pinned_to_its_width(self):
        """`aspect-ratio: 1/1` is what sliced the badge off a tall card (#381)."""
        body = _rule(".resource-card")

        self.assertNotIn(
            "aspect-ratio",
            body,
            "a pinned aspect ratio clips the badge on a long title (#381)",
        )
        self.assertRegex(
            body,
            r"min-height:\s*\d+px",
            "a pixel floor is what keeps a short-title card square",
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

    def test_badge_row_never_shrinks_and_can_wrap(self):
        """A card can carry both a source badge and the 'Linked' badge."""
        body = _rule(".card-badges")

        self.assertIn("flex-wrap: wrap", body)
        self.assertRegex(body, r"flex-shrink:\s*0")

    def test_selection_tick_stays_clear_of_the_badge_row(self):
        """The tick is absolutely positioned and paints over whatever is beneath
        it, so it must not sit in the corner the badges now occupy."""
        body = _rule(".resource-card.selected::after")

        self.assertIn("position: absolute", body)
        self.assertRegex(body, r"bottom:\s*\d+px")
        self.assertNotRegex(
            body,
            r"(?m)^\s*top:",
            "a top-anchored tick covers the source badge (#381)",
        )

    def test_every_ebook_card_renders_its_source_in_the_badge_row(self):
        """Guard the markup as well as the CSS - a badge that is not emitted
        cannot be un-clipped."""
        block = _card_block('data-source-type="{{ eb.source }}"')

        self.assertIn('<div class="card-badges">', block)
        self.assertIn('<span class="source-badge {{ badge_cls }}">{{ eb.source }}</span>', block)

    def test_badge_row_comes_before_the_title_on_every_card(self):
        """The reporter's ask: the source is in the same place on every candidate,
        not trailing a title whose length varies."""
        for marker in (
            'data-source-type="{{ eb.source }}"',      # ebook candidates
            "onclick=\"selectStoryteller(this, '{{ st.uuid }}')\"",  # Storyteller
            'data-audio-only="true"',                  # audio-only ghost card
        ):
            with self.subTest(card=marker):
                block = _card_block(marker)

                self.assertLess(
                    block.index('class="card-badges"'),
                    block.index('class="resource-icon"'),
                    "badges must lead the card, ahead of the icon and title",
                )

    def test_full_title_stays_available_on_hover(self):
        """Clamping is only acceptable because the untruncated title is kept."""
        self.assertIn(
            '<div class="resource-title" title="{{ eb.display_name }}">', _source()
        )


if __name__ == "__main__":
    unittest.main()
