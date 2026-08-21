import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.hardcover_client import HardcoverClient


class TestHardcoverRereadHistory(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {"HARDCOVER_TOKEN": "test-token", "HARDCOVER_ENABLED": "true"},
            clear=False,
        )
        self.env_patcher.start()
        self.client = HardcoverClient()
        self.client._get_today_date = Mock(return_value="2026-08-20")

    def tearDown(self):
        self.env_patcher.stop()

    @staticmethod
    def _completed_read():
        return {
            "id": 22,
            "started_at": "2026-07-01",
            "finished_at": "2026-07-05",
        }

    def test_reread_progress_creates_new_read_instead_of_overwriting_history(self):
        self.client.query = Mock(
            side_effect=[
                {"user_book_reads": [self._completed_read()]},
                {
                    "insert_user_book_read": {
                        "error": None,
                        "user_book_read": {"id": 23},
                    }
                },
            ]
        )

        updated = self.client.update_progress(
            11,
            50,
            edition_id=44,
            current_percentage=0.25,
            allow_new_read=True,
        )

        self.assertTrue(updated)
        self.assertEqual(self.client.query.call_count, 2)
        mutation, variables = self.client.query.call_args_list[1].args
        self.assertIn("InsertUserBookRead", mutation)
        self.assertNotIn("UpdateBookProgress", mutation)
        self.assertEqual(variables["id"], 11)
        self.assertEqual(variables["pages"], 50)
        self.assertEqual(variables["editionId"], 44)
        self.assertEqual(variables["startedAt"], "2026-08-20")
        self.assertIsNone(variables["finishedAt"])

    def test_repeated_finished_sync_does_not_create_phantom_reread(self):
        self.client.query = Mock(
            return_value={"user_book_reads": [self._completed_read()]}
        )

        updated = self.client.update_progress(
            11,
            200,
            edition_id=44,
            is_finished=True,
            current_percentage=1.0,
        )

        self.assertTrue(updated)
        self.client.query.assert_called_once()
        query, variables = self.client.query.call_args.args
        self.assertIn("user_book_reads", query)
        self.assertEqual(variables, {"userBookId": 11})

    def test_tiny_post_completion_progress_does_not_create_reread(self):
        self.client.query = Mock(
            return_value={"user_book_reads": [self._completed_read()]}
        )

        updated = self.client.update_progress(
            11,
            2,
            edition_id=44,
            current_percentage=0.01,
        )

        self.assertTrue(updated)
        self.client.query.assert_called_once()

    def test_audio_reread_creates_new_seconds_based_read(self):
        self.client.query = Mock(
            side_effect=[
                {"user_book_reads": [self._completed_read()]},
                {
                    "insert_user_book_read": {
                        "error": None,
                        "user_book_read": {"id": 23},
                    }
                },
            ]
        )

        updated = self.client.update_progress(
            11,
            0,
            edition_id=44,
            current_percentage=0.25,
            audio_seconds=3600,
            allow_new_read=True,
        )

        self.assertTrue(updated)
        mutation, variables = self.client.query.call_args_list[1].args
        self.assertIn("InsertUserBookRead", mutation)
        self.assertEqual(variables["seconds"], 900)
        self.assertEqual(variables["editionId"], 44)
        self.assertEqual(variables["startedAt"], "2026-08-20")
        self.assertIsNone(variables["finishedAt"])


    def test_midrange_progress_without_opt_in_writes_nothing(self):
        """A finished read is never rewritten from a position alone.

        Mid-range progress against a completed read used to be read as evidence
        of a reread. Confirming a reread is now the caller's job, so without
        ``allow_new_read`` the completed read is left exactly as it is.
        """
        self.client.query = Mock(
            return_value={"user_book_reads": [self._completed_read()]}
        )

        updated = self.client.update_progress(
            11,
            100,
            edition_id=44,
            current_percentage=0.5,
        )

        self.assertTrue(updated)
        self.client.query.assert_called_once()
        query, variables = self.client.query.call_args.args
        self.assertIn("user_book_reads", query)
        self.assertNotIn("mutation", query)
        self.assertEqual(variables, {"userBookId": 11})


if __name__ == "__main__":
    unittest.main(verbosity=2)
