import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, SyncResult, UpdateProgressRequest
from src.sync_manager import SyncManager


class ABSPolicySkipResultTestCase(unittest.TestCase):
    def test_backward_abs_policy_skip_is_successful_but_not_applied(self):
        abs_api = Mock()
        abs_api.get_progress.return_value = {"currentTime": 700.0}
        alignment = Mock()
        alignment.get_time_for_text.return_value = 550.0
        client = ABSSyncClient(abs_api, Mock(), Mock(), alignment_service=alignment)
        book = SimpleNamespace(
            abs_id="book-1",
            abs_title="Test Book",
            transcript_file="DB_MANAGED",
            duration=1000.0,
        )
        request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.55, match_index=123),
            txt="target text",
        )

        result = client.update_progress(book, request)

        self.assertTrue(result.success)
        self.assertTrue(result.updated_state.get("skipped"))
        self.assertEqual(result.location, 700.0)
        self.assertEqual(result.updated_state.get("ts"), 700.0)
        self.assertAlmostEqual(result.updated_state.get("pct"), 0.7)
        abs_api.update_progress.assert_not_called()

    def test_skipped_success_is_not_an_applied_write(self):
        result = SyncResult(700.0, True, {"ts": 700.0, "pct": 0.7, "skipped": True})
        self.assertFalse(SyncManager._sync_result_was_applied(result))

    def test_normal_success_is_an_applied_write(self):
        result = SyncResult(700.0, True, {"ts": 700.0, "pct": 0.7})
        self.assertTrue(SyncManager._sync_result_was_applied(result))

    def test_skipped_success_does_not_stamp_own_write_marker(self):
        manager = SyncManager.__new__(SyncManager)
        result = SyncResult(700.0, True, {"ts": 700.0, "pct": 0.7, "skipped": True})
        with patch("src.services.write_tracker.record_write") as record_write:
            manager._record_bridge_write("ABS", "book-1", result)
        record_write.assert_not_called()

    def test_normal_success_still_stamps_own_write_marker(self):
        manager = SyncManager.__new__(SyncManager)
        result = SyncResult(700.0, True, {"ts": 700.0, "pct": 0.7})
        with patch("src.services.write_tracker.record_write") as record_write:
            manager._record_bridge_write("ABS", "book-1", result)
        record_write.assert_called_once_with("ABS", "book-1", 0.7)


if __name__ == "__main__":
    unittest.main()
