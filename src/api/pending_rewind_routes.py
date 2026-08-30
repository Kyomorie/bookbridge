"""Authenticated dashboard API for KoSync -> ABS rewind decisions."""

from flask import jsonify

from src.api import kosync_server
from src.api.kosync_server import kosync_admin_bp
from src.services.pending_rewind_service import PendingRewindService
from src.utils.user_context import get_current_user_id


def _dependencies():
    database_service = kosync_server._database_service
    manager = kosync_server._manager
    if database_service is None or manager is None:
        return None, None, None
    return database_service, manager, PendingRewindService(database_service)


def _user_sync_clients(manager, user_id: int):
    registry = getattr(manager, "user_client_registry", None)
    if registry is not None:
        bundle = registry.get_clients(user_id)
        return getattr(bundle, "sync_clients", None) or {}
    return getattr(manager, "sync_clients", None) or {}


def _public_row(row: dict) -> dict:
    source = row.get("source_snapshot") or {}
    target = row.get("target_snapshot") or {}
    return {
        "id": row.get("id"),
        "abs_id": row.get("abs_id"),
        "book_title": row.get("book_title") or row.get("abs_id") or "Unknown book",
        "current_pct": target.get("pct"),
        "proposed_pct": row.get("proposed_pct"),
        "source_updated_at": source.get("service_updated_at"),
        "target_updated_at": target.get("service_updated_at"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "status": row.get("status"),
    }


@kosync_admin_bp.route("/api/pending-rewinds", methods=["GET"])
def api_pending_rewinds():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "authentication required"}), 401

    database_service, manager, service = _dependencies()
    if database_service is None or manager is None or service is None:
        return jsonify({"error": "pending rewind service unavailable"}), 503

    return jsonify({"items": [_public_row(row) for row in service.list_pending(user_id=user_id)]})


@kosync_admin_bp.route("/api/pending-rewinds/<int:rewind_id>/dismiss", methods=["POST"])
def api_dismiss_pending_rewind(rewind_id: int):
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "authentication required"}), 401

    database_service, manager, service = _dependencies()
    if database_service is None or manager is None or service is None:
        return jsonify({"error": "pending rewind service unavailable"}), 503

    if not service.dismiss(rewind_id, user_id=user_id):
        return jsonify({"error": "pending rewind not found"}), 404
    return jsonify({"status": "dismissed"})


@kosync_admin_bp.route("/api/pending-rewinds/<int:rewind_id>/approve", methods=["POST"])
def api_approve_pending_rewind(rewind_id: int):
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"error": "authentication required"}), 401

    database_service, manager, service = _dependencies()
    if database_service is None or manager is None or service is None:
        return jsonify({"error": "pending rewind service unavailable"}), 503

    try:
        sync_clients = _user_sync_clients(manager, user_id)
    except Exception:
        return jsonify({"error": "user sync clients unavailable"}), 503

    outcome = service.approve(
        rewind_id,
        sync_clients=sync_clients,
        user_id=user_id,
    )
    status = outcome.get("status")
    http_status = {
        "approved": 200,
        "stale": 409,
        "expired": 409,
        "dismissed": 409,
        "not_found": 404,
        "unavailable": 503,
        "write_failed": 502,
    }.get(status, 400)
    return jsonify({"status": status, "applied": bool(outcome.get("applied"))}), http_status
