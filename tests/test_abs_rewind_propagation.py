# Regression coverage for the remaining Audiobookshelf side of issue #215.

from types import SimpleNamespace
from unittest.mock import Mock

from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def _make_client():
    abs_client = Mock()
    abs_client.get_progress.return_value = {"currentTime": 700.0}
    abs_client.update_progress.return_value = {"success": True}

    alignment_service = Mock()
    alignment_service.get_time_for_text.return_value = 300.0

    client = ABSSyncClient(
        abs_client=abs_client,
        transcriber=Mock(),
        ebook_parser=Mock(),
        alignment_service=alignment_service,
    )
    client.abs_progress_offset = 0.0
    return client, abs_client, alignment_service


def test_verified_sync_target_can_rewind_audiobookshelf():
    """The ABS adapter must apply the position selected by SyncManager.

    Direction/conflict policy belongs to SyncManager/KoSync provenance guards.
    Once a locator reaches a follower client, a lower mapped timestamp is a
    legitimate sync target and must not be silently converted back to ABS's
    current (furthest) position. This is the remaining #215 failure mode.
    """
    client, abs_client, alignment_service = _make_client()
    book = SimpleNamespace(
        abs_id="abs-1",
        abs_title="Issue 215 Rewind",
        transcript_file="DB_MANAGED",
        duration=1000.0,
    )
    request = UpdateProgressRequest(
        LocatorResult(percentage=0.3, match_index=300),
        txt="verified rewind anchor",
        credit_listening=False,
    )

    result = client.update_progress(book, request)

    alignment_service.get_time_for_text.assert_called_once_with(
        "abs-1", "verified rewind anchor", char_offset_hint=300
    )
    abs_client.update_progress.assert_called_once_with("abs-1", 300.0, 0.0)
    assert result.success is True
    assert result.location == 300.0
    assert result.updated_state["ts"] == 300.0
    assert result.updated_state["pct"] == 0.3


def test_forward_abs_sync_still_credits_no_listening_for_reader_progress():
    """Removing the rewind veto must not turn reader sync into listening time."""
    client, abs_client, alignment_service = _make_client()
    abs_client.get_progress.return_value = {"currentTime": 200.0}
    alignment_service.get_time_for_text.return_value = 600.0
    book = SimpleNamespace(
        abs_id="abs-1",
        abs_title="Forward Control",
        transcript_file="DB_MANAGED",
        duration=1000.0,
    )
    request = UpdateProgressRequest(
        LocatorResult(percentage=0.6, match_index=600),
        txt="forward anchor",
        credit_listening=False,
    )

    result = client.update_progress(book, request)

    abs_client.update_progress.assert_called_once_with("abs-1", 600.0, 0.0)
    assert result.success is True
    assert result.updated_state["pct"] == 0.6
