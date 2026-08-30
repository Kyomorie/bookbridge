"""Durable, per-user approval flow for KoSync -> Audiobookshelf rewinds.

Issue #215 deliberately keeps Audiobookshelf's monotonic write guard intact.
When KoSync is the accepted leader and that guard blocks the backwards ABS
write, SyncManager may offer the skipped write here. Nothing in this module
auto-applies a rewind: approval always revalidates both stored snapshots and
expires fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

from sqlalchemy import text

from src.utils.config_loader import env_truthy
from src.utils.user_context import get_current_user_id

logger = logging.getLogger(__name__)

_SOURCE_KEYS = ("pct", "xpath", "service_updated_at")
_TARGET_KEYS = ("pct", "ts", "service_updated_at")


def _snapshot(data: dict | None, keys: tuple[str, ...]) -> dict:
    data = data or {}
    return {key: data.get(key) for key in keys}


def _canonical_json(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()


def _number_close(left: Any, right: Any, tolerance: float) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


class PendingRewindService:
    """Persistence and approval policy for the narrowly scoped KoSync -> ABS flow."""

    def __init__(self, database_service):
        self.database_service = database_service

    @staticmethod
    def _ttl_seconds() -> float:
        """Read per call so Settings changes apply without restarting."""
        try:
            hours = float(os.environ.get("KOSYNC_PENDING_REWIND_TTL_HOURS", "24") or 24)
        except (TypeError, ValueError):
            hours = 24.0
        return max(0.0, min(hours, 24.0 * 30.0)) * 3600.0

    def _resolve_user_id(self, user_id: int | None = None) -> int | None:
        if user_id is not None:
            return int(user_id)
        ambient = get_current_user_id()
        if ambient is not None:
            return int(ambient)
        resolver = getattr(self.database_service, "_default_user_id", None)
        if not callable(resolver):
            return None
        resolved = resolver()
        return int(resolved) if resolved is not None else None

    @staticmethod
    def source_snapshot(source_state: dict | None) -> dict:
        return _snapshot(source_state, _SOURCE_KEYS)

    @staticmethod
    def target_snapshot(target_state: dict | None) -> dict:
        return _snapshot(target_state, _TARGET_KEYS)

    @classmethod
    def source_fingerprint(cls, source_state: dict | None) -> str:
        return _fingerprint(cls.source_snapshot(source_state))

    def _expire_pending(self, user_id: int | None = None, now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        params = {"now": now}
        scope = ""
        if user_id is not None:
            scope = " AND user_id = :user_id"
            params["user_id"] = int(user_id)
        with self.database_service.get_session() as session:
            result = session.execute(
                text(
                    "UPDATE pending_rewinds SET status='expired', decided_at=:now "
                    "WHERE status='pending' AND expires_at <= :now" + scope
                ),
                params,
            )
            return int(result.rowcount or 0)

    @staticmethod
    def _row_dict(row) -> dict | None:
        if row is None:
            return None
        data = dict(row._mapping)
        for key in ("source_snapshot_json", "target_snapshot_json"):
            parsed_key = key.removesuffix("_json")
            try:
                data[parsed_key] = json.loads(data.get(key) or "{}")
            except (TypeError, json.JSONDecodeError):
                data[parsed_key] = {}
        return data

    def offer(
        self,
        *,
        book,
        source_state: dict,
        skipped_result,
        user_id: int | None = None,
        now: float | None = None,
    ) -> dict | None:
        """Create/dedupe a pending decision for a skipped KoSync -> ABS write."""
        if env_truthy("KOSYNC_FURTHEST_WINS", "true"):
            return None
        if not skipped_result or getattr(skipped_result, "skipped", False) is not True:
            return None

        updated = getattr(skipped_result, "updated_state", None) or {}
        proposed_ts = updated.get("_proposed_ts")
        target = self.target_snapshot(updated)
        current_ts = target.get("ts")
        if proposed_ts is None or current_ts is None:
            return None
        try:
            proposed_ts = float(proposed_ts)
            current_ts = float(current_ts)
        except (TypeError, ValueError):
            return None
        if proposed_ts >= current_ts - 0.01:
            return None

        uid = self._resolve_user_id(user_id)
        abs_id = getattr(book, "abs_id", None)
        if uid is None or not abs_id:
            logger.warning("Pending rewind not created: concrete user/book identity unavailable")
            return None

        source = self.source_snapshot(source_state)
        source_fp = _fingerprint(source)
        created_at = time.time() if now is None else float(now)
        expires_at = created_at + self._ttl_seconds()
        proposed_pct = source.get("pct")

        with self.database_service.get_session() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO pending_rewinds "
                    "(user_id, abs_id, source_client, source_fingerprint, "
                    " source_snapshot_json, target_snapshot_json, proposed_abs_ts, "
                    " proposed_pct, status, created_at, expires_at) "
                    "VALUES (:user_id, :abs_id, 'kosync', :fingerprint, :source_json, "
                    " :target_json, :proposed_ts, :proposed_pct, 'pending', :created_at, :expires_at)"
                ),
                {
                    "user_id": uid,
                    "abs_id": str(abs_id),
                    "fingerprint": source_fp,
                    "source_json": _canonical_json(source),
                    "target_json": _canonical_json(target),
                    "proposed_ts": proposed_ts,
                    "proposed_pct": proposed_pct,
                    "created_at": created_at,
                    "expires_at": expires_at,
                },
            )
            row = session.execute(
                text(
                    "SELECT * FROM pending_rewinds "
                    "WHERE user_id=:user_id AND abs_id=:abs_id AND source_fingerprint=:fingerprint"
                ),
                {"user_id": uid, "abs_id": str(abs_id), "fingerprint": source_fp},
            ).first()

        decision = self._row_dict(row)
        if decision and decision.get("status") == "pending":
            logger.info(
                "⏪ Pending KoSync -> ABS rewind for '%s' user_id=%s: %.2f%% -> %.2f%%",
                abs_id,
                uid,
                float(target.get("pct") or 0) * 100,
                float(proposed_pct or 0) * 100,
            )
        return decision

    def list_pending(self, user_id: int | None = None, now: float | None = None) -> list[dict]:
        uid = self._resolve_user_id(user_id)
        if uid is None:
            return []
        self._expire_pending(uid, now=now)
        with self.database_service.get_session() as session:
            rows = session.execute(
                text(
                    "SELECT pr.*, b.abs_title AS book_title FROM pending_rewinds pr "
                    "LEFT JOIN books b ON b.abs_id = pr.abs_id "
                    "WHERE pr.user_id=:user_id AND pr.status='pending' "
                    "ORDER BY pr.created_at DESC"
                ),
                {"user_id": uid},
            ).all()
        return [self._row_dict(row) for row in rows]

    def get(self, rewind_id: int, user_id: int | None = None) -> dict | None:
        uid = self._resolve_user_id(user_id)
        if uid is None:
            return None
        self._expire_pending(uid)
        with self.database_service.get_session() as session:
            row = session.execute(
                text("SELECT * FROM pending_rewinds WHERE id=:id AND user_id=:user_id"),
                {"id": int(rewind_id), "user_id": uid},
            ).first()
        return self._row_dict(row)

    def _set_status(self, rewind_id: int, user_id: int, status: str, now: float | None = None) -> bool:
        decided_at = time.time() if now is None else float(now)
        with self.database_service.get_session() as session:
            result = session.execute(
                text(
                    "UPDATE pending_rewinds SET status=:status, decided_at=:decided_at "
                    "WHERE id=:id AND user_id=:user_id AND status='pending'"
                ),
                {
                    "status": status,
                    "decided_at": decided_at,
                    "id": int(rewind_id),
                    "user_id": int(user_id),
                },
            )
            return bool(result.rowcount)

    def dismiss(self, rewind_id: int, user_id: int | None = None) -> bool:
        uid = self._resolve_user_id(user_id)
        if uid is None:
            return False
        self._expire_pending(uid)
        return self._set_status(rewind_id, uid, "dismissed")

    @staticmethod
    def _source_matches(expected: dict, live: dict) -> bool:
        return (
            _number_close(expected.get("pct"), live.get("pct"), 1e-6)
            and (expected.get("xpath") or "") == (live.get("xpath") or "")
            and _number_close(expected.get("service_updated_at"), live.get("service_updated_at"), 1e-3)
        )

    @staticmethod
    def _target_matches(expected: dict, live: dict) -> bool:
        return (
            _number_close(expected.get("pct"), live.get("pct"), 1e-6)
            and _number_close(expected.get("ts"), live.get("ts"), 0.05)
            and _number_close(expected.get("service_updated_at"), live.get("service_updated_at"), 1e-3)
        )

    def approve(
        self,
        rewind_id: int,
        *,
        sync_clients: dict,
        user_id: int | None = None,
        now: float | None = None,
    ) -> dict:
        """Revalidate source+target snapshots, then apply exactly one ABS rewind."""
        uid = self._resolve_user_id(user_id)
        if uid is None:
            return {"status": "not_found", "applied": False}

        self._expire_pending(uid, now=now)
        decision = self.get(rewind_id, uid)
        if not decision or decision.get("status") != "pending":
            return {"status": (decision or {}).get("status", "not_found"), "applied": False}

        try:
            if not self.database_service.is_user_linked(uid, decision["abs_id"]):
                return {"status": "not_found", "applied": False}
        except Exception:
            logger.warning("Pending rewind ownership check failed closed", exc_info=True)
            return {"status": "not_found", "applied": False}

        book = self.database_service.get_book(decision["abs_id"])
        kosync = (sync_clients or {}).get("KoSync")
        abs_sync = (sync_clients or {}).get("ABS")
        if book is None or kosync is None or abs_sync is None:
            return {"status": "unavailable", "applied": False}

        expected_source = decision.get("source_snapshot") or {}
        expected_target = decision.get("target_snapshot") or {}

        try:
            source_1_state = kosync.get_service_state(book, None)
            target_state = abs_sync.get_service_state(book, None)
            source_2_state = kosync.get_service_state(book, None)
        except Exception:
            logger.warning("Pending rewind snapshot refresh failed; leaving request pending", exc_info=True)
            return {"status": "unavailable", "applied": False}

        source_1 = self.source_snapshot(getattr(source_1_state, "current", None))
        source_2 = self.source_snapshot(getattr(source_2_state, "current", None))
        target = self.target_snapshot(getattr(target_state, "current", None))
        if (
            not self._source_matches(expected_source, source_1)
            or not self._target_matches(expected_target, target)
            or not self._source_matches(expected_source, source_2)
        ):
            self._set_status(rewind_id, uid, "stale", now=now)
            return {"status": "stale", "applied": False}

        apply_fn = getattr(abs_sync, "apply_approved_rewind", None)
        if not callable(apply_fn):
            return {"status": "unavailable", "applied": False}

        result = apply_fn(
            book,
            float(decision["proposed_abs_ts"]),
            expected_current_ts=float(expected_target["ts"]),
            expected_service_updated_at=expected_target.get("service_updated_at"),
        )
        if not result or not getattr(result, "success", False):
            return {"status": "write_failed", "applied": False}
        if getattr(result, "skipped", False) is True:
            self._set_status(rewind_id, uid, "stale", now=now)
            return {"status": "stale", "applied": False}

        self._set_status(rewind_id, uid, "approved", now=now)
        return {"status": "approved", "applied": True, "result": result}
