"""Dashboard API for the on-demand reading-position text preview.

The route is attached to the existing KoSync admin blueprint because that
blueprint already hosts authenticated dashboard-management endpoints around
reader positions.  Unlike the device-facing ``kosync`` blueprint, it is not
exempt from the web-session auth guard.
"""
from flask import jsonify

from src.api import kosync_server
from src.api.kosync_server import kosync_admin_bp
from src.services.reading_position_preview import build_reading_position_preview
from src.utils.user_context import get_current_user_id


@kosync_admin_bp.route("/api/books/<abs_id>/position-preview", methods=["GET"])
def api_book_position_preview(abs_id: str):
    """Return a bounded text excerpt around the current user's saved position."""
    database_service = kosync_server._database_service
    manager = kosync_server._manager
    user_id = get_current_user_id()

    if user_id is None:
        return jsonify({"error": "authentication required"}), 401
    if database_service is None or manager is None:
        return jsonify({"error": "reading position service unavailable"}), 503

    book = database_service.get_book(abs_id)
    if book is None:
        return jsonify({"error": "book not found"}), 404
    if not database_service.is_user_linked(user_id, abs_id):
        return jsonify({"error": "book not available for this user"}), 403

    states = database_service.get_states_for_book(abs_id, user_id=user_id)
    reading_stats = database_service.get_reading_stats(abs_id, user_id=user_id) or {}

    payload = build_reading_position_preview(
        book=book,
        states=states,
        last_leader=reading_stats.get("last_leader"),
        ebook_parser=manager.ebook_parser,
        alignment_service=manager.alignment_service,
    )
    response = jsonify(payload)
    # The payload contains copyrighted book text and should never be retained by
    # a shared proxy/browser cache.  The parser's own in-process EPUB cache is
    # sufficient for repeated requests.
    response.headers["Cache-Control"] = "private, no-store"
    return response
