import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.api_clients import ABSClient


class TestABSGetAudioFilesExpanded(unittest.TestCase):
    """Regression tests: GET /api/items/{id} must pass expanded=1 so that
    media.audioFiles (and media.chapters, read by get_item_details callers)
    is reliably present in the response, per the ABS API docs
    (https://api.audiobookshelf.org/). Without it, the base Library Item
    response can omit media.audioFiles for a valid, playable audiobook,
    which previously caused get_audio_files() to silently return [].
    """

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

    def _expanded_item_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "id": "item-123",
            "media": {
                "audioFiles": [
                    {"ino": "2", "ext": "mp3", "track": 2, "disc": 1},
                    {"ino": "1", "ext": "mp3", "track": 1, "disc": 1},
                ],
                "chapters": [{"id": 0, "title": "Chapter 1"}],
            },
        }
        return resp

    def test_get_audio_files_requests_expanded_param(self):
        self.client.session.get.return_value = self._expanded_item_response()

        self.client.get_audio_files("item-123")

        self.client.session.get.assert_called_once()
        args, kwargs = self.client.session.get.call_args
        self.assertEqual(args[0], "http://abs.example/api/items/item-123")
        self.assertEqual(kwargs.get("params"), {"expanded": 1})

    def test_get_audio_files_parses_expanded_payload_sorted_by_track(self):
        self.client.session.get.return_value = self._expanded_item_response()

        files = self.client.get_audio_files("item-123")

        self.assertEqual(len(files), 2)
        # Sorted by (disc, track): ino "1" (track 1) before ino "2" (track 2)
        self.assertIn("/file/1?token=token", files[0]["stream_url"])
        self.assertIn("/file/2?token=token", files[1]["stream_url"])
        self.assertEqual(files[0]["ext"], "mp3")

    def test_get_audio_files_returns_empty_list_on_non_200(self):
        resp = MagicMock()
        resp.status_code = 404
        self.client.session.get.return_value = resp

        self.assertEqual(self.client.get_audio_files("missing-item"), [])

    def test_get_item_details_requests_expanded_param(self):
        self.client.session.get.return_value = self._expanded_item_response()

        details = self.client.get_item_details("item-123")

        self.client.session.get.assert_called_once()
        args, kwargs = self.client.session.get.call_args
        self.assertEqual(args[0], "http://abs.example/api/items/item-123")
        self.assertEqual(kwargs.get("params"), {"expanded": 1})
        self.assertEqual(details["media"]["chapters"][0]["title"], "Chapter 1")


if __name__ == "__main__":
    unittest.main()
