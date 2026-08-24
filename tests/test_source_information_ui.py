"""PR #407 - provider badges on the Suggestions cards.

The badges name the audiobook's provider so a user with the same title in ABS,
Grimmory and BookOrbit can tell the candidate cards apart.

Two defects in the original implementation are guarded here:

1. The badge was `position: absolute; top: 10px; right: 10px`, which is exactly
   where `.suggestion-card.is-queued::before` floats its "Queued" marker (both
   top-right, card padding 10px). Queueing a suggestion painted the two labels
   on top of each other - the same failure #381 fixed on the Add Book cards.
   The badge is now an inline chip beside the language chip, so there is no
   shared corner and no gutter arithmetic to keep in sync.

2. The label was resolved in Jinja from `s.audio_source or 'ABS'`, which skips
   the bridge-key prefix fallback the rest of the page uses. A suggestion
   rehydrated from the persisted scan cache carries `bridge_key="booklore:..."`
   with no `audio_source`, so it was badged "Audiobookshelf" - mislabelling
   precisely the non-ABS books the feature exists to identify. Resolution now
   lives in `web_server.suggestion_source_badge()`.

CSS cannot be executed here, so the layout guards are structural assertions on
the real rule bodies. The label guards render the real template.
"""

import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.web_server import suggestion_source_badge
from tests.test_webserver import MockContainer

_SUGGESTIONS = Path(__file__).resolve().parents[1] / "templates" / "suggestions.html"


def _source() -> str:
    return _SUGGESTIONS.read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """Return the declaration block for a top-level CSS rule."""
    match = re.search(
        r"(?m)^\s*" + re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", _source()
    )
    assert match is not None, f"no CSS rule for {selector}"
    return match.group("body")


class SuggestionSourceBadgeResolutionTests(unittest.TestCase):
    """The provider -> (label, css class) mapping, at its single source of truth."""

    def test_explicit_sources_map_to_their_product_names(self):
        self.assertEqual(suggestion_source_badge("ABS"), ("Audiobookshelf", "abs"))
        self.assertEqual(suggestion_source_badge("BookLore"), ("Grimmory", "grimmory"))
        self.assertEqual(suggestion_source_badge("BookOrbit"), ("BookOrbit", "bookorbit"))

    def test_missing_source_falls_back_to_the_bridge_key_prefix(self):
        """The defect: a cached suggestion has only a prefixed bridge key."""
        self.assertEqual(
            suggestion_source_badge(None, "booklore:123"), ("Grimmory", "grimmory")
        )
        self.assertEqual(
            suggestion_source_badge("", "bookorbit:abc"), ("BookOrbit", "bookorbit")
        )

    def test_unprefixed_bridge_key_is_an_abs_item_id(self):
        self.assertEqual(
            suggestion_source_badge(None, "li_abcdef"), ("Audiobookshelf", "abs")
        )

    def test_no_source_and_no_key_defaults_to_abs(self):
        self.assertEqual(suggestion_source_badge(None, None), ("Audiobookshelf", "abs"))

    def test_unrecognised_source_keeps_its_own_name_and_neutral_styling(self):
        self.assertEqual(suggestion_source_badge("Kavita"), ("Kavita", "unknown"))


class SuggestionProviderBadgeLayoutTests(unittest.TestCase):
    """The badge must not share a corner with the queued marker (#381's lesson)."""

    def test_badge_is_not_absolutely_positioned_in_the_card_corner(self):
        self.assertNotIn(
            ".suggestion-provider-badge",
            _source(),
            "an absolutely positioned badge collides with the 'Queued' float",
        )

    def test_queued_marker_keeps_the_top_right_corner_to_itself(self):
        body = _rule(".suggestion-card.is-queued::before")
        self.assertIn("float: right", body)

    def test_card_reserves_no_gutter_for_an_absolute_badge(self):
        """The 108px gutter and the badge's max-width disagreed; an inline chip
        needs neither, so both must be gone or they will drift apart again."""
        self.assertNotRegex(_rule(".suggestion-main"), r"padding-right:")
        self.assertNotRegex(_rule(".suggestion-card"), r"position:\s*relative")

    def test_provider_chip_reuses_the_shared_badge_styling(self):
        for css_class in ("abs", "grimmory", "bookorbit", "unknown"):
            self.assertRegex(
                _source(),
                r"\.source-badge\." + css_class + r"\s*\{",
                f"no .source-badge.{css_class} rule backs the emitted class",
            )


class SuggestionProviderBadgeRenderTests(unittest.TestCase):
    """Render the real template through the real route."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        os.environ["TEMPLATE_DIR"] = str(Path(__file__).parent.parent / "templates")

        self.mock_container = MockContainer()
        mock_db = self.mock_container.mock_database_service
        mock_db.get_all_books.return_value = []
        mock_db.get_all_pending_suggestions.return_value = []
        mock_db.get_ignored_suggestion_source_ids.return_value = []

        def _mock_initialize_database(_data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils

        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = _mock_initialize_database

        with web_server.SUGGESTIONS_SCAN_JOBS_LOCK:
            web_server.SUGGESTIONS_SCAN_JOBS.clear()
        with web_server.SUGGESTIONS_STATE_LOCK:
            web_server.SUGGESTIONS_STATE_STORE.clear()

        self.app, _ = web_server.create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        web_server._match_queue_clear()

    def tearDown(self):
        import shutil
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self.original_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_cached_suggestion(self, key: str, entry: dict) -> None:
        entry = {"abs_id": key, "bridge_key": key, **entry}
        web_server._save_persisted_suggestions_cache({
            "scan_cache_by_abs": {key: entry},
            "scan_cache_no_match_abs_ids": [],
            "scan_last_stats": {"scanned_new": 1, "reused_cached": 0},
            "updated_at": time.time(),
        })

    def _render(self) -> str:
        response = self.client.get("/suggestions")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_abs_suggestion_renders_the_audiobookshelf_chip(self):
        self._seed_cached_suggestion("ab-1", {
            "audio_source": "ABS",
            "audio_title": "An ABS Book",
        })

        html = self._render()

        self.assertIn("An ABS Book", html)
        self.assertIn(
            '<span class="source-badge abs" title="Audiobook source">Audiobookshelf</span>',
            html,
        )

    def test_cached_grimmory_suggestion_without_audio_source_is_not_badged_abs(self):
        """The reported defect: only the bridge key names the provider."""
        self._seed_cached_suggestion("booklore:77", {
            "audio_title": "A Grimmory Book",
        })

        html = self._render()

        self.assertIn("A Grimmory Book", html)
        self.assertIn(
            '<span class="source-badge grimmory" title="Audiobook source">Grimmory</span>',
            html,
        )
        self.assertNotIn("Audiobookshelf", html)

    def test_badge_renders_inside_the_card_title_not_the_card_corner(self):
        self._seed_cached_suggestion("bookorbit:9", {"audio_title": "An Orbit Book"})

        html = self._render()

        self.assertNotIn("suggestion-provider-badge", html)
        title_block = re.search(
            r'<div class="title">(?P<body>.*?)</div>', html, re.DOTALL
        )
        self.assertIsNotNone(title_block, "no card title rendered")
        self.assertIn("BookOrbit", title_block.group("body"))


if __name__ == "__main__":
    unittest.main()
