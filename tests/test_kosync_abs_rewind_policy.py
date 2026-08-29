import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, ServiceState, UpdateProgressRequest
from src.sync_manager import SyncManager
from src.utils.user_context import reset_current_user_id, set_current_user_id


def _state(current):
    return ServiceState(
        current=current,
        previous_pct=0.0,
        delta=0.0,
        threshold=0.0,
        is_configured=True,
        display=("x", "x"),
        value_formatter=str,
    )


def _request(pct=0.55, match_index=100):
    return UpdateProgressRequest(
        LocatorResult(percentage=pct, match_index=match_index),
        txt="target text",
        current_state=_state({"pct": 0.70, "ts": 700.0}),
    )


def _manager(db):
    manager = SyncManager.__new__(SyncManager)
    manager.database_service = db
    return manager


def _book():
    return SimpleNamespace(
        abs_id="abs-book",
        abs_title="Book",
        duration=1000.0,
        transcript_file="DB_MANAGED",
    )


def _leader(pct=0.55, xpath="/body/DocFragment[5]/p[1].0", service_updated_at=1234.0):
    return _state({
        "pct": pct,
        "xpath": xpath,
        "service_updated_at": service_updated_at,
        "_kosync_recent_external_put": True,
        "_kosync_last_put_device": "Kindle",
        "_kosync_last_put_device_id": "device-1",
        "_kosync_last_put_age_seconds": 17.0,
    })


def _abs_state(pct=0.70, ts=700.0, service_updated_at=1200.0):
    return _state({"pct": pct, "ts": ts, "service_updated_at": service_updated_at})


class _DB:
    def __init__(self, user_id=7):
        self.user_id = user_id
        self.calls = []

    def resolve_user_id(self, requested):
        return requested if requested is not None else self.user_id

    def get_or_create_pending_rewind(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": 11, "status": "pending"}, len(self.calls) == 1


def test_abs_preview_uses_same_alignment_target_without_abs_io():
    abs_api = Mock()
    alignment = Mock()
    alignment.get_time_for_text.return_value = 550.0
    with patch.dict(os.environ, {"ABS_PROGRESS_OFFSET_SECONDS": "2"}, clear=False):
        client = ABSSyncClient(abs_api, Mock(), Mock(), alignment_service=alignment)
    request = _request()

    preview = client.preview_progress_update(_book(), request)

    assert preview == {"ts": 550.0, "adjusted_ts": 552.0, "pct": 0.552}
    alignment.get_time_for_text.assert_called_once_with(
        "abs-book", "target text", char_offset_hint=100
    )
    abs_api.get_progress.assert_not_called()
    abs_api.update_progress.assert_not_called()


def test_abs_update_and_preview_share_the_resolver():
    client = ABSSyncClient(Mock(), Mock(), Mock(), alignment_service=Mock())
    client._resolve_progress_target = Mock(return_value=800.0)
    client.abs_client.get_progress.return_value = {"currentTime": 700.0}
    client.abs_client.update_progress.return_value = {"success": True}
    request = _request(pct=0.8)

    preview = client.preview_progress_update(_book(), request)
    result = client.update_progress(_book(), request)

    assert preview["ts"] == 800.0
    assert result.success is True
    assert client._resolve_progress_target.call_count == 2
    client.abs_client.update_progress.assert_called_once_with("abs-book", 800.0, 0.0)


def test_furthest_wins_true_short_circuits_without_preview_or_pending():
    db = _DB()
    manager = _manager(db)
    client = Mock()
    with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "true"}, clear=False):
        blocked = manager._maybe_queue_kosync_abs_rewind(
            _book(), "KoSync", _leader(), "ABS", client, _abs_state(), _request(), "Book"
        )
    assert blocked is False
    client.preview_progress_update.assert_not_called()
    assert db.calls == []


def test_opt_in_backward_kosync_abs_write_is_queued_and_suppressed():
    db = _DB()
    manager = _manager(db)
    client = Mock()
    client.preview_progress_update.return_value = {"ts": 550.0, "adjusted_ts": 550.0, "pct": 0.55}
    token = set_current_user_id(7)
    try:
        with patch.dict(os.environ, {
            "KOSYNC_FURTHEST_WINS": "false",
            "KOSYNC_ABS_REWIND_TTL_HOURS": "24",
        }, clear=False):
            blocked = manager._maybe_queue_kosync_abs_rewind(
                _book(), "KoSync", _leader(), "ABS", client, _abs_state(), _request(), "Book"
            )
    finally:
        reset_current_user_id(token)

    assert blocked is True
    assert len(db.calls) == 1
    call = db.calls[0]
    assert call["user_id"] == 7
    assert call["abs_id"] == "abs-book"
    assert call["proposed_timestamp"] == 550.0
    assert call["proposed_percentage"] == 0.55
    assert call["ttl_hours"] == 24.0
    assert call["source_snapshot"] == {
        "pct": 0.55,
        "xpath": "/body/DocFragment[5]/p[1].0",
        "service_updated_at": 1234.0,
        "_kosync_last_put_device": "Kindle",
        "_kosync_last_put_device_id": "device-1",
    }
    assert "_kosync_last_put_age_seconds" not in call["source_snapshot"]
    assert call["target_snapshot"] == {
        "pct": 0.70,
        "ts": 700.0,
        "service_updated_at": 1200.0,
    }


def test_same_source_can_be_deduped_without_allowing_the_write():
    db = _DB()
    manager = _manager(db)
    client = Mock()
    client.preview_progress_update.return_value = {"ts": 550.0, "pct": 0.55}
    with patch.dict(os.environ, {
        "KOSYNC_FURTHEST_WINS": "false",
        "KOSYNC_ABS_REWIND_TTL_HOURS": "24",
    }, clear=False):
        assert manager._maybe_queue_kosync_abs_rewind(
            _book(), "KoSync", _leader(), "ABS", client, _abs_state(), _request(), "Book"
        ) is True
        assert manager._maybe_queue_kosync_abs_rewind(
            _book(), "KoSync", _leader(), "ABS", client, _abs_state(), _request(), "Book"
        ) is True
    assert len(db.calls) == 2
    assert db.calls[0]["source_snapshot"] == db.calls[1]["source_snapshot"]


def test_forward_kosync_abs_write_is_not_queued():
    db = _DB()
    manager = _manager(db)
    client = Mock()
    client.preview_progress_update.return_value = {"ts": 750.0, "pct": 0.75}
    with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "false"}, clear=False):
        blocked = manager._maybe_queue_kosync_abs_rewind(
            _book(), "KoSync", _leader(pct=0.75), "ABS", client, _abs_state(), _request(pct=0.75), "Book"
        )
    assert blocked is False
    assert db.calls == []


def test_exact_clear_progress_never_becomes_pending():
    db = _DB()
    manager = _manager(db)
    client = Mock()
    with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "false"}, clear=False):
        blocked = manager._maybe_queue_kosync_abs_rewind(
            _book(), "KoSync", _leader(pct=0.0), "ABS", client, _abs_state(), _request(pct=0.0), "Book"
        )
    assert blocked is False
    client.preview_progress_update.assert_not_called()
    assert db.calls == []


def test_near_zero_collapse_is_pending_not_an_automatic_abs_write():
    db = _DB()
    manager = _manager(db)
    client = Mock()
    client.preview_progress_update.return_value = {"ts": 3.0, "pct": 0.003}
    with patch.dict(os.environ, {
        "KOSYNC_FURTHEST_WINS": "false",
        "KOSYNC_ABS_REWIND_TTL_HOURS": "24",
    }, clear=False):
        blocked = manager._maybe_queue_kosync_abs_rewind(
            _book(), "KoSync", _leader(pct=0.003), "ABS", client,
            _abs_state(pct=0.79, ts=790.0), _request(pct=0.003), "Book"
        )
    assert blocked is True
    assert db.calls[0]["proposed_percentage"] == 0.003


def test_invalid_ttl_blocks_backward_write_without_creating_pending():
    db = _DB()
    manager = _manager(db)
    client = Mock()
    client.preview_progress_update.return_value = {"ts": 550.0, "pct": 0.55}
    for value in ("0", "-1", "not-a-number"):
        db.calls.clear()
        with patch.dict(os.environ, {
            "KOSYNC_FURTHEST_WINS": "false",
            "KOSYNC_ABS_REWIND_TTL_HOURS": value,
        }, clear=False):
            assert manager._maybe_queue_kosync_abs_rewind(
                _book(), "KoSync", _leader(), "ABS", client, _abs_state(), _request(), "Book"
            ) is True
        assert db.calls == []


def test_pending_persistence_failure_fails_closed():
    db = _DB()
    db.get_or_create_pending_rewind = Mock(side_effect=RuntimeError("db down"))
    manager = _manager(db)
    client = Mock()
    client.preview_progress_update.return_value = {"ts": 550.0, "pct": 0.55}
    with patch.dict(os.environ, {
        "KOSYNC_FURTHEST_WINS": "false",
        "KOSYNC_ABS_REWIND_TTL_HOURS": "24",
    }, clear=False):
        assert manager._maybe_queue_kosync_abs_rewind(
            _book(), "KoSync", _leader(), "ABS", client, _abs_state(), _request(), "Book"
        ) is True


def test_other_leader_or_target_never_enters_rewind_policy():
    db = _DB()
    manager = _manager(db)
    client = Mock()
    with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "false"}, clear=False):
        assert manager._maybe_queue_kosync_abs_rewind(
            _book(), "ABS", _leader(), "KoSync", client, _abs_state(), _request(), "Book"
        ) is False
        assert manager._maybe_queue_kosync_abs_rewind(
            _book(), "Storyteller", _leader(), "ABS", client, _abs_state(), _request(), "Book"
        ) is False
    client.preview_progress_update.assert_not_called()
    assert db.calls == []
