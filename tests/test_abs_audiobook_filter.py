import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.api_clients import ABSClient


class TestABSAudiobookFilter(unittest.TestCase):
    """Tests for ABSClient.get_audiobooks_for_lib audio filtering behavior."""

    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token"},
            clear=False,
        )
        self.env_patch.start()
        self.client = ABSClient()
        self.client.session = MagicMock()

    def tearDown(self):
        self.env_patch.stop()

    def _make_response(self, status_code: int, items: list[dict]):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = {"results": items}
        return resp

    def _item_with_audiofiles(self, title: str, count: int = 1) -> dict:
        return {
            "id": f"id-{title}",
            "title": title,
            "media": {"audioFiles": [{"ino": i} for i in range(count)]},
        }

    def _item_with_num_audio_files(self, title: str, num: int) -> dict:
        return {
            "id": f"id-{title}",
            "title": title,
            "media": {"numAudioFiles": num},
        }

    def _item_with_duration(self, title: str, duration: float) -> dict:
        return {
            "id": f"id-{title}",
            "title": title,
            "media": {"duration": duration},
        }

    def _ebook_only_item(self, title: str) -> dict:
        return {
            "id": f"id-{title}",
            "title": title,
            "media": {"ebookFile": "file.epub"},
        }

    def _item_no_audio_metadata(self, title: str) -> dict:
        return {
            "id": f"id-{title}",
            "title": title,
            "media": {"audioFiles": [], "numAudioFiles": 0, "duration": 0},
        }

    def test_regression_mediaType_path_filters_ebook_only_items(self):
        """The reported bug: mediaType=audiobook returns mixed list but filter was skipped."""
        audiobook = self._item_with_audiofiles("Real Audiobook", 5)
        ebook1 = self._ebook_only_item("Ebook Only 1")
        ebook2 = self._ebook_only_item("Ebook Only 2")

        # mediaType path returns non-empty mixed list
        self.client.session.get.side_effect = [
            self._make_response(200, [audiobook, ebook1, ebook2]),
        ]

        result = self.client.get_audiobooks_for_lib("lib-123")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Real Audiobook")
        self.assertNotIn("Ebook Only 1", [r["title"] for r in result])
        self.assertNotIn("Ebook Only 2", [r["title"] for r in result])

    def test_each_audio_signal_is_honoured(self):
        """Items with any audio signal (audioFiles, numAudioFiles, duration) are kept."""
        with_audiofiles = self._item_with_audiofiles("Has AudioFiles", 3)
        with_num_audio = self._item_with_num_audio_files("Has NumAudioFiles", 3)
        with_duration = self._item_with_duration("Has Duration", 3600.0)
        no_audio = self._item_no_audio_metadata("No Audio At All")

        self.client.session.get.side_effect = [
            self._make_response(200, [with_audiofiles, with_num_audio, with_duration, no_audio]),
        ]

        result = self.client.get_audiobooks_for_lib("lib-123")

        titles = [r["title"] for r in result]
        self.assertEqual(len(result), 3)
        self.assertIn("Has AudioFiles", titles)
        self.assertIn("Has NumAudioFiles", titles)
        self.assertIn("Has Duration", titles)
        self.assertNotIn("No Audio At All", titles)

    def test_safety_valve_on_mediaType_path_returns_unfiltered_when_all_filtered(self):
        """If mediaType response is non-empty but no items have audio, return unfiltered list."""
        ebook1 = self._ebook_only_item("Ebook 1")
        ebook2 = self._ebook_only_item("Ebook 2")

        self.client.session.get.side_effect = [
            self._make_response(200, [ebook1, ebook2]),
        ]

        with self.assertLogs("src.api.api_clients", level="DEBUG") as captured:
            result = self.client.get_audiobooks_for_lib("lib-123")

        self.assertEqual(len(result), 2)
        titles = [r["title"] for r in result]
        self.assertIn("Ebook 1", titles)
        self.assertIn("Ebook 2", titles)
        self.assertTrue(
            any("safety fallback" in line for line in captured.output),
            "Expected debug log about safety fallback",
        )

    def test_fallback_path_still_works_when_mediaType_returns_empty(self):
        """When mediaType request returns empty results, fallback is used and filtered."""
        audiobook = self._item_with_audiofiles("Fallback Audiobook", 2)
        ebook = self._ebook_only_item("Fallback Ebook")

        # First call (mediaType) returns empty results, second call (fallback) returns mixed
        self.client.session.get.side_effect = [
            self._make_response(200, []),  # mediaType path: empty
            self._make_response(200, [audiobook, ebook]),  # fallback path: mixed
        ]

        result = self.client.get_audiobooks_for_lib("lib-123")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Fallback Audiobook")
        self.assertEqual(self.client.session.get.call_count, 2)

    def test_both_requests_failing_returns_empty(self):
        """Non-200 responses on both paths return empty list."""
        self.client.session.get.side_effect = [
            self._make_response(500, []),  # mediaType path fails
            self._make_response(500, []),  # fallback path fails
        ]

        result = self.client.get_audiobooks_for_lib("lib-123")

        self.assertEqual(result, [])

    def test_regression_guard_one_audiobook_plus_fifty_ebooks(self):
        """Reported symptom: 1 audiobook + 50 ebook-only items returned exactly 1 item."""
        audiobook = self._item_with_audiofiles("Only Audiobook", 10)
        ebooks = [self._ebook_only_item(f"Ebook {i}") for i in range(50)]

        self.client.session.get.side_effect = [
            self._make_response(200, [audiobook] + ebooks),
        ]

        result = self.client.get_audiobooks_for_lib("lib-123")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Only Audiobook")

    def test_safety_valve_on_fallback_path_returns_unfiltered_when_all_filtered(self):
        """Fallback path also has safety valve when filtering would blank the library."""
        ebook1 = self._ebook_only_item("Fallback Ebook 1")
        ebook2 = self._ebook_only_item("Fallback Ebook 2")

        self.client.session.get.side_effect = [
            self._make_response(404, []),  # mediaType path fails (non-200)
            self._make_response(200, [ebook1, ebook2]),  # fallback returns non-empty no-audio
        ]

        with self.assertLogs("src.api.api_clients", level="DEBUG") as captured:
            result = self.client.get_audiobooks_for_lib("lib-123")

        self.assertEqual(len(result), 2)
        titles = [r["title"] for r in result]
        self.assertIn("Fallback Ebook 1", titles)
        self.assertIn("Fallback Ebook 2", titles)
        self.assertTrue(
            any("safety fallback" in line for line in captured.output),
            "Expected debug log about safety fallback on fallback path",
        )

    def test_mediaType_returns_200_but_empty_results_triggers_fallback(self):
        """mediaType path returns 200 with empty results -> fallback is attempted."""
        audiobook = self._item_with_audiofiles("From Fallback", 1)

        self.client.session.get.side_effect = [
            self._make_response(200, []),  # mediaType: 200 but empty
            self._make_response(200, [audiobook]),  # fallback: has audiobook
        ]

        result = self.client.get_audiobooks_for_lib("lib-123")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "From Fallback")
        self.assertEqual(self.client.session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()