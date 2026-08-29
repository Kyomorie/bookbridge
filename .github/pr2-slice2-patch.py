from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, got {count}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


# ---------------------------------------------------------------------------
# ABS: extract the existing text -> timestamp resolver so policy can preview
# the exact same target without performing an ABS write or a second ABS read.
# ---------------------------------------------------------------------------
old_abs = r'''    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_title = book.abs_title or 'Unknown Book'
        if request.locator_result.percentage == 0.0:
            logger.info(f"🔄 '{book_title}' Locator percentage is 0.0% — Setting ABS progress to start of book")
            result, final_ts = self._update_abs_progress_with_offset(book.abs_id, 0.0)
            updated_state = {
                'ts': final_ts,
                'pct': 0.0
            }
            return SyncResult(
                final_ts,
                result.get("success", False),
                updated_state,
                error_code=self._stale_item_error_code(book.abs_id, result),
            )

        # Route database-managed books to AlignmentService and legacy books to Transcriber.
        ts_for_text = None
        
        if book.transcript_file == "DB_MANAGED" and self.alignment_service:
            # Use database alignment.
            # We use the match_index (character offset) found by the EbookParser
            char_index = request.locator_result.match_index
            if char_index is not None:
                ts_for_text = self.alignment_service.get_time_for_text(
                    book.abs_id, 
                    request.txt, 
                    char_offset_hint=char_index
                )
            else:
                logger.debug(f"🔍 '{book_title}' Alignment lookup skipped: No character index provided in request")
                
        elif book.transcript_file and book.transcript_file != "DB_MANAGED":
            # Legacy Path: Use JSON File
            ts_for_text = self.transcriber.find_time_for_text(
                book.transcript_file, request.txt,
                hint_percentage=request.locator_result.percentage,
                char_offset=request.locator_result.match_index,
                book_title=book_title
            )
        if ts_for_text is not None:
'''
new_abs = r'''    def _resolve_progress_target(self, book: Book, request: UpdateProgressRequest) -> Optional[float]:
        """Resolve the ABS timestamp a normal progress update would target.

        This is deliberately side-effect free: it reuses the same alignment path as
        ``update_progress`` but performs neither an ABS progress read nor a write.
        The rewind-approval policy can therefore inspect the proposed target against
        the ABS ``ServiceState`` already fetched by the current sync cycle.
        """
        if request.locator_result is None:
            return None
        if request.locator_result.percentage == 0.0:
            return 0.0

        book_title = book.abs_title or 'Unknown Book'
        ts_for_text = None
        if book.transcript_file == "DB_MANAGED" and self.alignment_service:
            char_index = request.locator_result.match_index
            if char_index is not None:
                ts_for_text = self.alignment_service.get_time_for_text(
                    book.abs_id,
                    request.txt,
                    char_offset_hint=char_index,
                )
            else:
                logger.debug(
                    f"🔍 '{book_title}' Alignment lookup skipped: No character index provided in request"
                )
        elif book.transcript_file and book.transcript_file != "DB_MANAGED":
            ts_for_text = self.transcriber.find_time_for_text(
                book.transcript_file,
                request.txt,
                hint_percentage=request.locator_result.percentage,
                char_offset=request.locator_result.match_index,
                book_title=book_title,
            )

        try:
            return float(ts_for_text) if ts_for_text is not None else None
        except (TypeError, ValueError):
            logger.warning(
                f"⚠️ '{book_title}' Resolved ABS timestamp is invalid: {ts_for_text!r}"
            )
            return None

    def preview_progress_update(self, book: Book, request: UpdateProgressRequest) -> Optional[dict]:
        """Return the proposed ABS target without reading or mutating ABS.

        ``ts`` is the raw alignment timestamp used by the existing monotonic guard;
        ``adjusted_ts`` is what ``_update_abs_progress_with_offset`` would submit.
        Keeping both avoids changing existing offset/guard semantics while giving the
        approval flow an exact description of the eventual write.
        """
        ts = self._resolve_progress_target(book, request)
        if ts is None:
            return None
        adjusted_ts = max(round(ts + self.abs_progress_offset, 2), 0.0)
        return {
            'ts': ts,
            'adjusted_ts': adjusted_ts,
            'pct': self._abs_to_percentage(adjusted_ts, book) or 0.0,
        }

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_title = book.abs_title or 'Unknown Book'
        if request.locator_result.percentage == 0.0:
            logger.info(f"🔄 '{book_title}' Locator percentage is 0.0% — Setting ABS progress to start of book")
            result, final_ts = self._update_abs_progress_with_offset(book.abs_id, 0.0)
            updated_state = {
                'ts': final_ts,
                'pct': 0.0
            }
            return SyncResult(
                final_ts,
                result.get("success", False),
                updated_state,
                error_code=self._stale_item_error_code(book.abs_id, result),
            )

        ts_for_text = self._resolve_progress_target(book, request)
        if ts_for_text is not None:
'''
replace_once('src/sync_clients/abs_sync_client.py', old_abs, new_abs)


# ---------------------------------------------------------------------------
# DatabaseService: expose the existing user-resolution contract publicly so
# policy code does not reach into the private _resolve_uid implementation.
# ---------------------------------------------------------------------------
old_uid = r'''    def _resolve_uid(self, user_id):
        if user_id is not None:
            return user_id
'''
new_uid = r'''    def resolve_user_id(self, user_id: int = None) -> Optional[int]:
        """Resolve an explicit/ambient user using the normal state-owner rules."""
        return self._resolve_uid(user_id)

    def _resolve_uid(self, user_id):
        if user_id is not None:
            return user_id
'''
replace_once('src/db/database_service.py', old_uid, new_uid)


# ---------------------------------------------------------------------------
# SyncManager: narrow KoSync -> ABS policy. It runs before client.update_progress
# and returns True only when the backward write must be suppressed for approval.
# ---------------------------------------------------------------------------
anchor = r'''    def _completion_propagation_enabled(self) -> bool:
        return env_truthy('SYNC_COMPLETION_PROPAGATION')
'''
helpers = r'''    _KOSYNC_REWIND_SOURCE_KEYS = (
        'pct',
        'xpath',
        'service_updated_at',
        '_kosync_last_put_device',
        '_kosync_last_put_device_id',
    )
    _ABS_REWIND_TARGET_KEYS = ('pct', 'ts', 'service_updated_at', 'service_duration')

    @staticmethod
    def _rewind_snapshot(state: ServiceState | None, keys: tuple[str, ...]) -> dict:
        current = getattr(state, 'current', None)
        if not isinstance(current, dict):
            return {}
        # Exclude transient metadata such as _kosync_last_put_age_seconds: including
        # a clock-like value in the source fingerprint would recreate a dismissed
        # request every cycle even though the reader never moved.
        return {key: current[key] for key in keys if current.get(key) is not None}

    @staticmethod
    def _kosync_abs_rewind_ttl_hours() -> Optional[float]:
        raw = os.environ.get('KOSYNC_ABS_REWIND_TTL_HOURS', '24')
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _maybe_queue_kosync_abs_rewind(
        self,
        book: Book,
        leader: str,
        leader_state: ServiceState,
        client_name: str,
        client: SyncClient,
        client_state: ServiceState | None,
        request: UpdateProgressRequest,
        title_snip: str,
    ) -> bool:
        """Suppress and persist a KoSync -> ABS backward write pending approval.

        Returns True when the normal ABS write must not run. This path is purposely
        narrow: every other leader/target pair keeps the existing dispatch behavior.
        The safe default ``KOSYNC_FURTHEST_WINS=true`` short-circuits before even
        previewing a candidate, exactly as the user-facing setting promises.
        """
        if leader != 'KoSync' or client_name != 'ABS':
            return False
        if env_truthy('KOSYNC_FURTHEST_WINS', 'true'):
            return False

        locator = getattr(request, 'locator_result', None)
        # Explicit Clear Progress is an existing, deliberate reset workflow. Never
        # convert its exact 0% write into a pending rewind decision.
        if locator is None or getattr(locator, 'percentage', None) == 0.0:
            return False

        current_ts = None
        current = getattr(client_state, 'current', None)
        if isinstance(current, dict):
            try:
                current_ts = float(current.get('ts')) if current.get('ts') is not None else None
            except (TypeError, ValueError):
                current_ts = None
        if current_ts is None:
            # No snapshot means we cannot bind an approval safely. Fall through to
            # the long-standing ABS client guard, which will re-read ABS before any
            # backward write and remains the final defensive boundary.
            return False

        preview = getattr(client, 'preview_progress_update', None)
        if not callable(preview):
            return False
        proposal = preview(book, request)
        if not isinstance(proposal, dict) or proposal.get('ts') is None:
            return False
        try:
            proposed_ts = float(proposal['ts'])
        except (TypeError, ValueError):
            return False

        if proposed_ts >= current_ts:
            return False

        ttl_hours = self._kosync_abs_rewind_ttl_hours()
        if ttl_hours is None:
            logger.error(
                "⛔ '%s' '%s' Blocking KoSync → ABS rewind %.2fs → %.2fs: "
                "KOSYNC_ABS_REWIND_TTL_HOURS is invalid; approval creation fails closed",
                book.abs_id,
                title_snip,
                current_ts,
                proposed_ts,
            )
            return True

        user_id = self.database_service.resolve_user_id(get_current_user_id())
        if user_id is None:
            logger.error(
                "⛔ '%s' '%s' Blocking KoSync → ABS rewind because no user could be resolved",
                book.abs_id,
                title_snip,
            )
            return True

        source_snapshot = self._rewind_snapshot(
            leader_state,
            self._KOSYNC_REWIND_SOURCE_KEYS,
        )
        target_snapshot = self._rewind_snapshot(
            client_state,
            self._ABS_REWIND_TARGET_KEYS,
        )
        if source_snapshot.get('pct') is None or target_snapshot.get('ts') is None:
            logger.error(
                "⛔ '%s' '%s' Blocking KoSync → ABS rewind because a snapshot is incomplete",
                book.abs_id,
                title_snip,
            )
            return True

        try:
            pending, created = self.database_service.get_or_create_pending_rewind(
                user_id=user_id,
                abs_id=book.abs_id,
                source_snapshot=source_snapshot,
                target_snapshot=target_snapshot,
                proposed_timestamp=proposed_ts,
                proposed_percentage=float(proposal.get('pct') or 0.0),
                ttl_hours=ttl_hours,
            )
        except Exception as exc:
            logger.error(
                "⛔ '%s' '%s' Blocking KoSync → ABS rewind because pending state "
                "could not be persisted: %s",
                book.abs_id,
                title_snip,
                exc,
                exc_info=True,
            )
            return True

        if created:
            logger.info(
                "⏸️ '%s' '%s' KoSync → ABS rewind %.2fs → %.2fs awaits user approval (request %s)",
                book.abs_id,
                title_snip,
                current_ts,
                proposed_ts,
                pending.get('id'),
            )
        else:
            logger.debug(
                "KoSync → ABS rewind for '%s' matched existing request %s (%s)",
                book.abs_id,
                pending.get('id'),
                pending.get('status'),
            )
        return True

    def _completion_propagation_enabled(self) -> bool:
        return env_truthy('SYNC_COMPLETION_PROPAGATION')
'''
replace_once('src/sync_manager.py', anchor, helpers)

old_dispatch = r'''                        result = client.update_progress(book, request)
                        results[client_name] = result
                        self._record_bridge_write(client_name, abs_id, result)
'''
new_dispatch = r'''                        if self._maybe_queue_kosync_abs_rewind(
                            book,
                            leader,
                            leader_state,
                            client_name,
                            client,
                            client_state,
                            request,
                            title_snip,
                        ):
                            continue
                        result = client.update_progress(book, request)
                        results[client_name] = result
                        self._record_bridge_write(client_name, abs_id, result)
'''
replace_once('src/sync_manager.py', old_dispatch, new_dispatch)


# ---------------------------------------------------------------------------
# Focused policy tests. These exercise the choke-point directly so failures say
# whether the decision contract broke, not whether unrelated sync machinery did.
# ---------------------------------------------------------------------------
test_path = Path('tests/test_kosync_abs_rewind_policy.py')
if test_path.exists():
    raise SystemExit('tests/test_kosync_abs_rewind_policy.py already exists')
test_path.write_text(r'''import os
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
''')
