import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.services.pending_rewind_service import PendingRewindService
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, ServiceState, SyncResult, UpdateProgressRequest


class _DB:
    def __init__(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        self.Session = sessionmaker(bind=self.engine, future=True)
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE books (abs_id VARCHAR(255) PRIMARY KEY, abs_title VARCHAR(255))"))
            conn.execute(text("INSERT INTO books(abs_id, abs_title) VALUES ('book-1', 'Test Book')"))
            conn.execute(text("""
                CREATE TABLE pending_rewinds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    abs_id VARCHAR(255) NOT NULL,
                    source_client VARCHAR(32) NOT NULL DEFAULT 'kosync',
                    source_fingerprint VARCHAR(64) NOT NULL,
                    source_snapshot_json TEXT NOT NULL,
                    target_snapshot_json TEXT NOT NULL,
                    proposed_abs_ts FLOAT NOT NULL,
                    proposed_pct FLOAT,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    created_at FLOAT NOT NULL,
                    expires_at FLOAT NOT NULL,
                    decided_at FLOAT,
                    UNIQUE(user_id, abs_id, source_fingerprint)
                )
            """))
        self.book = SimpleNamespace(abs_id="book-1", abs_title="Test Book")
        self.linked = {1: True, 2: True}

    @contextmanager
    def get_session(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _default_user_id(self):
        return 1

    def get_book(self, abs_id):
        return self.book if abs_id == "book-1" else None

    def is_user_linked(self, user_id, abs_id):
        return abs_id == "book-1" and bool(self.linked.get(user_id))

    def rows(self):
        with self.get_session() as session:
            return [dict(row._mapping) for row in session.execute(text("SELECT * FROM pending_rewinds ORDER BY id"))]


def _source(pct=0.20, xpath="/body/DocFragment[4]/body/p[2].0", updated=1000.0):
    return {"pct": pct, "xpath": xpath, "service_updated_at": updated}


def _skipped(current_pct=0.50, current_ts=500.0, proposed_ts=200.0, updated=2000.0):
    return SyncResult(
        location=current_ts,
        success=True,
        updated_state={
            "pct": current_pct,
            "ts": current_ts,
            "service_updated_at": updated,
            "_proposed_ts": proposed_ts,
        },
        skipped=True,
    )


def _service_state(current):
    return ServiceState(
        current=current,
        previous_pct=0.0,
        delta=0.0,
        threshold=0.01,
        is_configured=True,
        display=("X", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda value: f"{value:.2%}",
    )


@pytest.fixture(autouse=True)
def _restore_env():
    old = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(old)


def test_default_furthest_wins_never_creates_prompt():
    db = _DB()
    service = PendingRewindService(db)
    os.environ["KOSYNC_FURTHEST_WINS"] = "true"

    assert service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1) is None
    assert db.rows() == []


def test_offer_is_per_user_and_dedupes_same_source_snapshot():
    db = _DB()
    service = PendingRewindService(db)
    os.environ["KOSYNC_FURTHEST_WINS"] = "false"
    os.environ["KOSYNC_PENDING_REWIND_TTL_HOURS"] = "24"

    first = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1, now=100.0)
    second = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1, now=200.0)
    other_user = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=2, now=200.0)

    assert first["id"] == second["id"]
    assert other_user["id"] != first["id"]
    assert len(db.rows()) == 2
    assert service.get(first["id"], user_id=2) is None


def test_dismissed_source_does_not_nag_until_kosync_moves():
    db = _DB()
    service = PendingRewindService(db)
    os.environ["KOSYNC_FURTHEST_WINS"] = "false"

    first = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1)
    assert service.dismiss(first["id"], user_id=1) is True

    same = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1)
    moved = service.offer(book=db.book, source_state=_source(pct=0.21, updated=1001.0), skipped_result=_skipped(), user_id=1)

    assert same["status"] == "dismissed"
    assert moved["status"] == "pending"
    assert moved["id"] != first["id"]
    assert len(service.list_pending(user_id=1)) == 1


def test_expiry_is_fail_closed_and_same_source_remains_deduped():
    db = _DB()
    service = PendingRewindService(db)
    os.environ["KOSYNC_FURTHEST_WINS"] = "false"
    os.environ["KOSYNC_PENDING_REWIND_TTL_HOURS"] = "1"

    first = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1, now=100.0)
    assert service.list_pending(user_id=1, now=3701.0) == []

    same = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1, now=3800.0)
    assert same["id"] == first["id"]
    assert same["status"] == "expired"


def test_approval_revalidates_both_snapshots_and_applies_exact_proposal():
    db = _DB()
    service = PendingRewindService(db)
    os.environ["KOSYNC_FURTHEST_WINS"] = "false"
    decision = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1)

    kosync = MagicMock()
    kosync.get_service_state.side_effect = [
        _service_state(_source()),
        _service_state(_source()),
    ]
    abs_sync = MagicMock()
    abs_sync.get_service_state.return_value = _service_state({"pct": 0.50, "ts": 500.0, "service_updated_at": 2000.0})
    abs_sync.apply_approved_rewind.return_value = SyncResult(200.0, True, {"pct": 0.20, "ts": 200.0})

    outcome = service.approve(decision["id"], sync_clients={"KoSync": kosync, "ABS": abs_sync}, user_id=1)

    assert outcome["status"] == "approved"
    assert outcome["applied"] is True
    abs_sync.apply_approved_rewind.assert_called_once_with(
        db.book,
        200.0,
        expected_current_ts=500.0,
        expected_service_updated_at=2000.0,
    )
    assert service.get(decision["id"], user_id=1)["status"] == "approved"


@pytest.mark.parametrize(
    "changed_source,changed_target",
    [
        (_source(pct=0.22, updated=1001.0), None),
        (None, {"pct": 0.51, "ts": 510.0, "service_updated_at": 2001.0}),
    ],
)
def test_approval_marks_changed_snapshot_stale_without_writing(changed_source, changed_target):
    db = _DB()
    service = PendingRewindService(db)
    os.environ["KOSYNC_FURTHEST_WINS"] = "false"
    decision = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1)

    live_source = changed_source or _source()
    live_target = changed_target or {"pct": 0.50, "ts": 500.0, "service_updated_at": 2000.0}
    kosync = MagicMock()
    kosync.get_service_state.side_effect = [_service_state(live_source), _service_state(live_source)]
    abs_sync = MagicMock()
    abs_sync.get_service_state.return_value = _service_state(live_target)

    outcome = service.approve(decision["id"], sync_clients={"KoSync": kosync, "ABS": abs_sync}, user_id=1)

    assert outcome == {"status": "stale", "applied": False}
    abs_sync.apply_approved_rewind.assert_not_called()
    assert service.get(decision["id"], user_id=1)["status"] == "stale"


def test_final_abs_compare_skip_marks_request_stale():
    db = _DB()
    service = PendingRewindService(db)
    os.environ["KOSYNC_FURTHEST_WINS"] = "false"
    decision = service.offer(book=db.book, source_state=_source(), skipped_result=_skipped(), user_id=1)

    kosync = MagicMock()
    kosync.get_service_state.side_effect = [_service_state(_source()), _service_state(_source())]
    abs_sync = MagicMock()
    abs_sync.get_service_state.return_value = _service_state({"pct": 0.50, "ts": 500.0, "service_updated_at": 2000.0})
    abs_sync.apply_approved_rewind.return_value = SyncResult(510.0, True, {"pct": 0.51, "ts": 510.0}, skipped=True)

    outcome = service.approve(decision["id"], sync_clients={"KoSync": kosync, "ABS": abs_sync}, user_id=1)

    assert outcome == {"status": "stale", "applied": False}
    assert service.get(decision["id"], user_id=1)["status"] == "stale"


def test_abs_policy_skip_carries_target_snapshot_and_proposed_timestamp():
    abs_client = MagicMock()
    abs_client.is_configured.return_value = True
    abs_client.get_progress.return_value = {
        "currentTime": 500.0,
        "lastUpdate": 2_000_000_000_000,
        "duration": 1000.0,
    }
    transcriber = MagicMock()
    transcriber.find_time_for_text.return_value = 200.0
    client = ABSSyncClient(abs_client, transcriber, MagicMock())
    book = SimpleNamespace(
        abs_id="book-1",
        abs_title="Test Book",
        transcript_file="legacy.json",
        duration=1000.0,
    )

    result = client.update_progress(
        book,
        UpdateProgressRequest(LocatorResult(percentage=0.20), txt="rewind target"),
    )

    assert result.success is True
    assert result.skipped is True
    assert result.updated_state["ts"] == 500.0
    assert result.updated_state["pct"] == pytest.approx(0.50)
    assert result.updated_state["service_updated_at"] == pytest.approx(2_000_000_000.0)
    assert result.updated_state["_proposed_ts"] == 200.0
    abs_client.update_progress.assert_not_called()


def test_abs_approved_rewind_final_compare_is_fail_closed():
    abs_client = MagicMock()
    abs_client.is_configured.return_value = True
    abs_client.get_progress.return_value = {
        "currentTime": 510.0,
        "lastUpdate": 2_001_000,
        "duration": 1000.0,
    }
    client = ABSSyncClient(abs_client, MagicMock(), MagicMock())
    book = SimpleNamespace(abs_id="book-1", abs_title="Test Book", transcript_file=None, duration=1000.0)

    result = client.apply_approved_rewind(
        book,
        200.0,
        expected_current_ts=500.0,
        expected_service_updated_at=2000.0,
    )

    assert result.success is True
    assert result.skipped is True
    assert result.location == 510.0
    abs_client.update_progress.assert_not_called()


def test_pending_rewind_ttl_setting_is_registered():
    from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

    assert "KOSYNC_PENDING_REWIND_TTL_HOURS" in ALL_SETTINGS
    assert DEFAULT_CONFIG["KOSYNC_PENDING_REWIND_TTL_HOURS"] == "24"


def test_sync_manager_offers_only_kosync_to_abs_policy_skip():
    from unittest.mock import patch
    from src.sync_manager import SyncManager

    manager = SyncManager.__new__(SyncManager)
    manager.database_service = MagicMock()
    book = SimpleNamespace(abs_id="book-1")
    leader_state = _service_state(_source())
    skipped = _skipped()

    with patch(
        "src.services.pending_rewind_service.PendingRewindService"
    ) as service_cls:
        service_cls.return_value.offer.return_value = {"id": 1}

        result = manager._maybe_offer_pending_rewind(
            book=book,
            leader="KoSync",
            leader_state=leader_state,
            client_name="ABS",
            result=skipped,
        )

        assert result == {"id": 1}
        service_cls.return_value.offer.assert_called_once()


def test_sync_manager_does_not_offer_non_kosync_rewind():
    from unittest.mock import patch
    from src.sync_manager import SyncManager

    manager = SyncManager.__new__(SyncManager)
    manager.database_service = MagicMock()
    book = SimpleNamespace(abs_id="book-1")
    leader_state = _service_state(_source())

    with patch(
        "src.services.pending_rewind_service.PendingRewindService"
    ) as service_cls:
        result = manager._maybe_offer_pending_rewind(
            book=book,
            leader="Storyteller",
            leader_state=leader_state,
            client_name="ABS",
            result=_skipped(),
        )

        assert result is None
        service_cls.assert_not_called()
