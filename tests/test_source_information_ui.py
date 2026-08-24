import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUGGESTIONS_TEMPLATE = (ROOT / "templates" / "suggestions.html").read_text(encoding="utf-8")


class SourceInformationUiTests(unittest.TestCase):
    def test_suggestions_show_audiobookshelf_and_grimmory_sources(self):
        self.assertIn("{% set suggestion_source_label = 'Audiobookshelf' %}", SUGGESTIONS_TEMPLATE)
        self.assertIn("{% set suggestion_source_label = 'Grimmory' %}", SUGGESTIONS_TEMPLATE)
        self.assertIn("{% set suggestion_source_label = 'BookOrbit' %}", SUGGESTIONS_TEMPLATE)
        self.assertIn("{% set suggestion_source_label = suggestion_source %}", SUGGESTIONS_TEMPLATE)
        self.assertIn(
            '<span class="source-badge {{ suggestion_source_class }} suggestion-provider-badge">{{ suggestion_source_label }}</span>',
            SUGGESTIONS_TEMPLATE,
        )

    def test_suggestion_provider_badge_is_positioned_in_card_corner(self):
        self.assertIn(".suggestion-card { position: relative;", SUGGESTIONS_TEMPLATE)
        self.assertIn(".suggestion-provider-badge { position: absolute; top: 10px; right: 10px;", SUGGESTIONS_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
