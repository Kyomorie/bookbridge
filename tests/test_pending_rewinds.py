from datetime import datetime, timedelta

from sqlalchemy import inspect

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG, KNOWN_SETTING_KEYS


def _service(tmp_path):
    db = DatabaseService(str(tmp_path / "rewinds.db"))
    user1 = db.create_user("reader-one", role="admin")
    user2 = db.create_user("reader-two")
    db.create_book(Book(abs_id="abs-book", abs_title="Book", duration=1000))
    db.link_user_book(user1.id, "abs-book")
    db.link_user_book(user2.id, "abs-book")
    return db, user1, user2


def _source(pct=0.55, xpath="/body/DocFragment[5]"):
    return {
        "client": "KoSync",
        "pct": pct,
        "xpath": xpath,
        "device": "kindle",
        "device_id": "k1",
        "service_updated_at": 1234.0,
    }


def _target(pct=0.79, ts=790.0):
    return {"client": "ABS", "pct": pct, "ts": ts, "service_updated_at": 1200.0}


def test_pending_rewind_schema_and_setting_are_registered(tmp_path):
    db, _, _ = _service(tmp_path)
    assert "pending_rewinds" in inspect(db.db_manager.engine).get_table_names()
    assert "KOSYNC_ABS_REWIND_TTL_HOURS" in ALL_SETTINGS
    assert "KOSYNC_ABS_REWIND_TTL_HOURS" in KNOWN_SETTING_KEYS
    assert DEFAULT_CONFIG["KOSYNC_ABS_REWIND_TTL_HOURS"] == "24"


def test_pending_rewind_round_trips_source_and_target_snapshots(tmp_path):
    db, user, _ = _service(tmp_path)
    now = datetime(2026, 8, 29, 12, 0, 0)
    row, created = db.get_or_create_pending_rewind(
        user.id, "abs-book", _source(), _target(), 550.0, 0.55, 24, now=now
    )
    assert created is True
    assert row["source_snapshot"] == _source()
    assert row["target_snapshot"] == _target()
    assert row["proposed_timestamp"] == 550.0
    assert row["proposed_percentage"] == 0.55
    assert row["status"] == "pending"
    assert row["expires_at"] == now + timedelta(hours=24)


def test_dedupe_survives_dismiss_and_only_source_movement_creates_new_request(tmp_path):
    db, user, _ = _service(tmp_path)
    now = datetime(2026, 8, 29, 12, 0, 0)
    first, created = db.get_or_create_pending_rewind(
        user.id, "abs-book", _source(), _target(), 550.0, 0.55, 24, now=now
    )
    assert created is True
    assert db.resolve_pending_rewind(user.id, first["id"], "dismissed", now=now) is True

    same, created = db.get_or_create_pending_rewind(
        user.id, "abs-book", _source(), _target(pct=0.82, ts=820.0), 550.0, 0.55, 24, now=now
    )
    assert created is False
    assert same["id"] == first["id"]
    assert same["status"] == "dismissed"
    assert same["target_snapshot"] == _target()

    moved, created = db.get_or_create_pending_rewind(
        user.id, "abs-book", _source(pct=0.56), _target(pct=0.82, ts=820.0), 560.0, 0.56, 24, now=now
    )
    assert created is True
    assert moved["id"] != first["id"]


def test_pending_rewinds_are_strictly_per_user(tmp_path):
    db, user1, user2 = _service(tmp_path)
    now = datetime(2026, 8, 29, 12, 0, 0)
    r1, _ = db.get_or_create_pending_rewind(
        user1.id, "abs-book", _source(), _target(), 550.0, 0.55, 24, now=now
    )
    r2, created = db.get_or_create_pending_rewind(
        user2.id, "abs-book", _source(), _target(), 550.0, 0.55, 24, now=now
    )
    assert created is True
    assert r1["id"] != r2["id"]
    assert [r["id"] for r in db.get_pending_rewinds(user1.id, now=now)] == [r1["id"]]
    assert [r["id"] for r in db.get_pending_rewinds(user2.id, now=now)] == [r2["id"]]
    assert db.get_pending_rewind(user1.id, r2["id"], now=now) is None
    assert db.resolve_pending_rewind(user1.id, r2["id"], "dismissed", now=now) is False


def test_expiry_is_fail_closed_and_deduped(tmp_path):
    db, user, _ = _service(tmp_path)
    created_at = datetime(2026, 8, 29, 12, 0, 0)
    first, _ = db.get_or_create_pending_rewind(
        user.id, "abs-book", _source(), _target(), 550.0, 0.55, 1, now=created_at
    )
    after_expiry = created_at + timedelta(hours=2)
    assert db.get_pending_rewind(user.id, first["id"], now=after_expiry) is None
    assert db.get_pending_rewinds(user.id, now=after_expiry) == []
    assert db.resolve_pending_rewind(user.id, first["id"], "accepted", now=after_expiry) is False

    same, created = db.get_or_create_pending_rewind(
        user.id, "abs-book", _source(), _target(), 550.0, 0.55, 24, now=after_expiry
    )
    assert created is False
    assert same["id"] == first["id"]
    assert same["status"] == "expired"


def test_pending_rewind_persists_across_service_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    db = DatabaseService(str(db_path))
    user = db.create_user("reader", role="admin")
    db.create_book(Book(abs_id="abs-book", abs_title="Book", duration=1000))
    db.link_user_book(user.id, "abs-book")
    now = datetime(2026, 8, 29, 12, 0, 0)
    row, _ = db.get_or_create_pending_rewind(
        user.id, "abs-book", _source(), _target(), 550.0, 0.55, 24, now=now
    )

    restarted = DatabaseService(str(db_path))
    loaded = restarted.get_pending_rewind(user.id, row["id"], now=now + timedelta(minutes=5))
    assert loaded is not None
    assert loaded["status"] == "pending"
    assert loaded["source_snapshot"] == _source()
    assert loaded["target_snapshot"] == _target()


def test_invalid_ttl_and_terminal_transition_fail_closed(tmp_path):
    db, user, _ = _service(tmp_path)
    now = datetime(2026, 8, 29, 12, 0, 0)
    for ttl in (0, -1, "not-a-number"):
        try:
            db.get_or_create_pending_rewind(
                user.id, "abs-book", _source(), _target(), 550.0, 0.55, ttl, now=now
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"ttl={ttl!r} must fail closed")

    row, _ = db.get_or_create_pending_rewind(
        user.id, "abs-book", _source(), _target(), 550.0, 0.55, 24, now=now
    )
    assert db.resolve_pending_rewind(user.id, row["id"], "dismissed", now=now) is True
    assert db.resolve_pending_rewind(user.id, row["id"], "accepted", now=now) is False
