"""Tests for _propagate_completion and its helper methods.

These tests verify the completion propagation logic that marks a book finished
on non-leader clients once the leader crosses the configured completion threshold.
"""

import os
import types
import unittest
from unittest.mock import MagicMock, patch

from src.sync_clients.sync_client_interface import SyncResult
from src.sync_manager import SyncManager
from src.db.models import Book


def _make_minimal_manager():
    """Build a minimal SyncManager without calling __init__.

    _propagate_completion only uses self._iter_update_targets,
    self._persist_state_snapshot, and the module-level exclusion set.
    This pattern is used in tests/test_bookorbit_multiuser_ownership.py.
    """
    mgr = SyncManager.__new__(SyncManager)
    mgr._iter_update_targets = types.MethodType(SyncManager._iter_update_targets, mgr)
    mgr._persist_state_snapshot = MagicMock()
    return mgr


def _make_book(abs_id="test-book", ebook_filename="test.epub"):
    """Create a test book."""
    return Book(
        abs_id=abs_id,
        abs_title="Test Book",
        ebook_filename=ebook_filename,
        status="active",
        duration=1000.0,
    )


class TestPropagateCompletion(unittest.TestCase):
    """Tests for _propagate_completion behavior."""

    def test_abs_success_records_write(self):
        """ABS success records the write in the write tracker.

        This is the headline regression. mark_finished bypasses
        ABSSyncClient.update_progress, and the ABS Socket.IO listener checks
        is_own_write('ABS', abs_id) before reacting. Without the recorded
        write, our own PATCH comes back as external movement and triggers a
        spurious instant sync.

        Note: the production code imports record_write lazily inside the
        function body, so we patch it at its definition site
        (src.services.write_tracker.record_write) rather than as an attribute
        of src.sync_manager.
        """
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_abs_client = MagicMock()
        mock_abs_client.abs_client = MagicMock()
        mock_abs_client.abs_client.mark_finished.return_value = True

        active_clients = {"ABS": mock_abs_client}

        with patch("src.services.write_tracker.record_write") as mock_record_write:
            mgr._propagate_completion(book, active_clients, "BookLore", "test-book", "Test Book")

            mock_abs_client.abs_client.mark_finished.assert_called_once_with("test-book")
            mock_record_write.assert_called_once_with("ABS", "test-book")

    def test_abs_failure_does_not_record_write(self):
        """ABS failure does not record the write and logs a warning."""
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_abs_client = MagicMock()
        mock_abs_client.abs_client = MagicMock()
        mock_abs_client.abs_client.mark_finished.return_value = False

        active_clients = {"ABS": mock_abs_client}

        with patch("src.services.write_tracker.record_write") as mock_record_write:
            with self.assertLogs(logger="src.sync_manager", level="WARNING") as cm:
                mgr._propagate_completion(book, active_clients, "BookLore", "test-book", "Test Book")

                mock_record_write.assert_not_called()
                warning_logs = [log for log in cm.output if "Completion propagation failed" in log]
                self.assertTrue(len(warning_logs) > 0, "Expected warning log for failed ABS propagation")
                self.assertIn("mark_finished returned False", warning_logs[0])

    def test_non_abs_success_persists_state_snapshot_at_100_percent(self):
        """Non-ABS success persists a state snapshot at 100 percent.

        Without this the DB keeps the pre-propagation position, the client
        reads back ~100 percent next cycle, that looks like fresh movement,
        and completion can re-fire.
        """
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_client = MagicMock()
        mock_client.update_progress.return_value = SyncResult(success=True, location=1.0)

        active_clients = {"BookLore": mock_client}

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

            mock_client.update_progress.assert_called_once()
            mock_persist.assert_called_once()
            args, _ = mock_persist.call_args
            state_dict = args[2]
            self.assertEqual(state_dict.get("pct"), 1.0)

    def test_non_abs_failure_persists_nothing(self):
        """Non-ABS failure persists nothing and logs a warning."""
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_client = MagicMock()
        mock_client.update_progress.return_value = SyncResult(success=False, error_code="some_error")

        active_clients = {"BookLore": mock_client}

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            with self.assertLogs(logger="src.sync_manager", level="WARNING") as cm:
                mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

                mock_client.update_progress.assert_called_once()
                mock_persist.assert_not_called()
                warning_logs = [log for log in cm.output if "Completion propagation failed" in log]
                self.assertTrue(len(warning_logs) > 0, "Expected warning log for failed propagation")
                self.assertIn("client reported unsuccessful write", warning_logs[0])

    def test_client_returning_none_is_treated_as_failure(self):
        """A client returning None is treated as a failure (defensive).

        No snapshot should be persisted and a warning should be logged.
        """
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_client = MagicMock()
        mock_client.update_progress.return_value = None

        active_clients = {"BookLore": mock_client}

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            with self.assertLogs(logger="src.sync_manager", level="WARNING") as cm:
                mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

                mock_client.update_progress.assert_called_once()
                mock_persist.assert_not_called()
                warning_logs = [log for log in cm.output if "Completion propagation failed" in log]
                self.assertTrue(len(warning_logs) > 0, "Expected warning log for None return")

    def test_excluded_clients_are_skipped_entirely(self):
        """Excluded clients (StoryGraph, Hardcover, ABSEbook) are skipped entirely.

        The trackers are driven exclusively by the trailing-edge idle-cooldown
        handlers which already bypass the cooldown at completion, and ABSEbook
        rejects a percentage-only locator because its cfi is None.
        """
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_storygraph = MagicMock()
        mock_hardcover = MagicMock()
        mock_absebook = MagicMock()
        mock_ordinary = MagicMock()
        mock_ordinary.update_progress.return_value = SyncResult(success=True, location=1.0)

        active_clients = {
            "StoryGraph": mock_storygraph,
            "Hardcover": mock_hardcover,
            "ABSEbook": mock_absebook,
            "BookLore": mock_ordinary,
        }

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

            mock_storygraph.update_progress.assert_not_called()
            mock_hardcover.update_progress.assert_not_called()
            mock_absebook.update_progress.assert_not_called()
            mock_ordinary.update_progress.assert_called_once()
            mock_persist.assert_called_once()

    def test_leader_is_never_written_to(self):
        """The leader itself is never written to."""
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_leader = MagicMock()
        mock_leader.update_progress.return_value = SyncResult(success=True, location=1.0)

        active_clients = {"BookLore": mock_leader}

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            mgr._propagate_completion(book, active_clients, "BookLore", "test-book", "Test Book")

            mock_leader.update_progress.assert_not_called()
            mock_persist.assert_not_called()

    def test_exception_in_one_client_does_not_abort_others(self):
        """An exception raised by one client does not abort propagation to the others."""
        mgr = _make_minimal_manager()
        book = _make_book()

        mock_failing = MagicMock()
        mock_failing.update_progress.side_effect = Exception("boom")

        mock_healthy = MagicMock()
        mock_healthy.update_progress.return_value = SyncResult(success=True, location=1.0)

        active_clients = {
            "FailingClient": mock_failing,
            "HealthyClient": mock_healthy,
        }

        with patch.object(mgr, "_persist_state_snapshot") as mock_persist:
            with self.assertLogs(logger="src.sync_manager", level="WARNING") as cm:
                mgr._propagate_completion(book, active_clients, "ABS", "test-book", "Test Book")

                mock_failing.update_progress.assert_called_once()
                mock_healthy.update_progress.assert_called_once()
                mock_persist.assert_called_once()  # Healthy client's persist
                warning_logs = [log for log in cm.output if "Completion propagation failed" in log]
                self.assertTrue(len(warning_logs) > 0, "Expected warning log for exception")


class TestCompletionThreshold(unittest.TestCase):
    """Tests for _completion_threshold helper method."""

    def _set_env_and_test(self, value, expected):
        """Helper to set SYNC_COMPLETION_THRESHOLD and test the conversion."""
        mgr = _make_minimal_manager()
        prev = os.environ.get("SYNC_COMPLETION_THRESHOLD")
        try:
            if value is None:
                os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)
            else:
                os.environ["SYNC_COMPLETION_THRESHOLD"] = value
            result = mgr._completion_threshold()
            self.assertEqual(result, expected)
        finally:
            if prev is None:
                os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)
            else:
                os.environ["SYNC_COMPLETION_THRESHOLD"] = prev

    def test_converts_0_100_setting_to_0_1_fraction(self):
        """_completion_threshold converts the 0-100 setting into a 0-1 fraction."""
        mgr = _make_minimal_manager()
        self._set_env_and_test("99", 0.99)
        self._set_env_and_test("100", 1.0)
        self._set_env_and_test("50", 0.5)

    def test_fallback_to_0_99_on_unparseable(self):
        """_completion_threshold falls back to 0.99 when the value is unparseable."""
        mgr = _make_minimal_manager()
        self._set_env_and_test("abc", 0.99)
        self._set_env_and_test("", 0.99)

    def test_clamps_out_of_range_values(self):
        """_completion_threshold clamps out-of-range values."""
        mgr = _make_minimal_manager()
        self._set_env_and_test("-10", 0.0)
        self._set_env_and_test("150", 1.0)

    def test_returns_0_99_when_variable_absent(self):
        """_completion_threshold returns 0.99 when the variable is absent entirely."""
        mgr = _make_minimal_manager()
        prev = os.environ.get("SYNC_COMPLETION_THRESHOLD")
        try:
            os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)
            result = mgr._completion_threshold()
            self.assertEqual(result, 0.99)
        finally:
            if prev is None:
                os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)
            else:
                os.environ["SYNC_COMPLETION_THRESHOLD"] = prev


class TestCompletionPropagationEnabled(unittest.TestCase):
    """Tests for _completion_propagation_enabled helper method."""

    def _set_env_and_test(self, value, expected):
        """Helper to set SYNC_COMPLETION_PROPAGATION and test the result."""
        mgr = _make_minimal_manager()
        prev = os.environ.get("SYNC_COMPLETION_PROPAGATION")
        try:
            if value is None:
                os.environ.pop("SYNC_COMPLETION_PROPAGATION", None)
            else:
                os.environ["SYNC_COMPLETION_PROPAGATION"] = value
            result = mgr._completion_propagation_enabled()
            self.assertEqual(result, expected)
        finally:
            if prev is None:
                os.environ.pop("SYNC_COMPLETION_PROPAGATION", None)
            else:
                os.environ["SYNC_COMPLETION_PROPAGATION"] = prev

    def test_false_when_setting_absent(self):
        """_completion_propagation_enabled is False when the setting is absent."""
        mgr = _make_minimal_manager()
        prev = os.environ.get("SYNC_COMPLETION_PROPAGATION")
        try:
            os.environ.pop("SYNC_COMPLETION_PROPAGATION", None)
            result = mgr._completion_propagation_enabled()
            self.assertFalse(result)
        finally:
            if prev is None:
                os.environ.pop("SYNC_COMPLETION_PROPAGATION", None)
            else:
                os.environ["SYNC_COMPLETION_PROPAGATION"] = prev

    def test_true_for_true_spelling(self):
        """_completion_propagation_enabled is True for 'true' spelling."""
        mgr = _make_minimal_manager()
        self._set_env_and_test("true", True)

    def test_true_for_on_spelling(self):
        """_completion_propagation_enabled is True for 'on' spelling.

        The 'on' spelling matters because settings checkboxes in this app
        POST "on", and this is a recurring source of bugs, so cover both
        explicitly.
        """
        mgr = _make_minimal_manager()
        self._set_env_and_test("on", True)


if __name__ == "__main__":
    unittest.main()