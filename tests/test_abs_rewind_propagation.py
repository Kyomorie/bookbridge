# Regression coverage for the remaining Audiobookshelf side of issue #215.

import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def _make_client(*, current_time=700.0, target_time=300.0):
    abs_client = Mock()
    abs_client.get_progress.return_value = {"currentTime": current_time}
    abs_client.update_progress.return_value = {"success": True}

    alignment_service = Mock()
    alignment_service.get_time_for_text.return_value = target_time

    client = ABSSyncClient(
        abs_client=abs_client,
        transcriber=Mock(),
        ebook_parser=Mock(),
        alignment_service=alignment_service,
    )
    client.abs_progress_offset = 0.0
    return client, abs_client, alignment_service


def _book(title="Issue 215 Rewind"):
    return SimpleNamespace(
        abs_id="abs-1",
        abs_title=title,
        transcript_file="DB_MANAGED",
        duration=1000.0,
    )


def _request(*, percentage=0.3, match_index=300, txt="rewind anchor"):
    return UpdateProgressRequest(
        LocatorResult(percentage=percentage, match_index=match_index),
        txt=txt,
        credit_listening=False,
    )


def test_safe_default_still_blocks_backward_abs_write():
    """The stale-reader protection remains fail-closed by default."""
    client, abs_client, alignment_service = _make_client()

    with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "true"}, clear=False):
        result = client.update_progress(_book(), _request())

    alignment_service.get_time_for_text.assert_called_once_with(
        "abs-1", "rewind anchor", char_offset_hint=300
    )
    abs_client.update_progress.assert_not_called()
    assert result.success is True
    assert result.location == 700.0
    assert result.updated_state["ts"] == 700.0
    assert result.updated_state["pct"] == 0.7


def test_explicit_rewind_opt_in_propagates_backward_position_to_abs():
    """#391's opt-in must actually move Audiobookshelf back as promised.

    KOSYNC_FURTHEST_WINS=false is the deliberate unsafe-side-of-the-tradeoff
    setting: BookBridge accepts a newer backward reader position, including from
    another device. Once that policy is selected, the ABS adapter must not apply
    an older unconditional furthest-only veto and silently keep the audiobook at
    its previous higher position (#215).
    """
    client, abs_client, alignment_service = _make_client()

    with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "false"}, clear=False):
        result = client.update_progress(_book(), _request())

    alignment_service.get_time_for_text.assert_called_once_with(
        "abs-1", "rewind anchor", char_offset_hint=300
    )
    abs_client.update_progress.assert_called_once_with("abs-1", 300.0, 0.0)
    assert result.success is True
    assert result.location == 300.0
    assert result.updated_state["ts"] == 300.0
    assert result.updated_state["pct"] == 0.3


def test_forward_abs_sync_is_unchanged_and_credits_no_listening():
    """The rewind policy must not affect ordinary forward reader progress."""
    client, abs_client, _alignment_service = _make_client(
        current_time=200.0,
        target_time=600.0,
    )

    with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "true"}, clear=False):
        result = client.update_progress(
            _book("Forward Control"),
            _request(percentage=0.6, match_index=600, txt="forward anchor"),
        )

    abs_client.update_progress.assert_called_once_with("abs-1", 600.0, 0.0)
    assert result.success is True
    assert result.updated_state["pct"] == 0.6
