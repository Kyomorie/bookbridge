import json
import logging
import os
from typing import Optional
from pathlib import Path

from src.api.api_clients import ABSClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import (
    SyncClient,
    SyncResult,
    UpdateProgressRequest,
    ServiceState,
    ABS_ITEM_NOT_FOUND,
)
from src.utils.ebook_utils import EbookParser
from src.utils.progress_metadata import parse_service_timestamp
from src.utils.transcriber import AudioTranscriber

logger = logging.getLogger(__name__)


class ABSSyncClient(SyncClient):
    def __init__(self, abs_client: ABSClient, transcriber: AudioTranscriber, ebook_parser: EbookParser, alignment_service=None):
        super().__init__(ebook_parser)
        self.abs_client = abs_client
        self.transcriber = transcriber
        self.alignment_service = alignment_service
        self.abs_progress_offset = float(os.getenv("ABS_PROGRESS_OFFSET_SECONDS", 0))
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))

    def is_configured(self) -> bool:
        return self.abs_client.is_configured()

    def check_connection(self):
        return self.abs_client.check_connection()

    def fetch_bulk_state(self):
        return self.abs_client.get_all_progress_raw()

    def get_supported_sync_types(self) -> set:
        return {'audiobook'}

    def supports_book(self, book: Book) -> bool:
        return (getattr(book, "audio_source", None) or "ABS") == "ABS"

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        abs_id = book.abs_id
        if bulk_context and abs_id in bulk_context:
            item_data = bulk_context[abs_id]
            abs_ts = item_data.get('currentTime', 0)
            abs_last_update = item_data.get('lastUpdate')
            abs_finished = bool(item_data.get('isFinished'))
            abs_duration = item_data.get('duration')
        else:
            response = self.abs_client.get_progress(abs_id)
            abs_ts = response.get('currentTime') if response is not None else None
            abs_last_update = response.get('lastUpdate') if response is not None else None
            abs_finished = bool(response.get('isFinished')) if response is not None else False
            abs_duration = response.get('duration') if response is not None else None

        if abs_ts is None:
            logger.info("🔍 ABS timestamp is None, probably not started the book yet")
            abs_ts = 0.0

        corrected_duration = self._corrected_duration(book, abs_duration)
        if corrected_duration is not None:
            logger.info(
                "📏 ABS duration changed for '%s': %.1fs -> %.1fs (percentages recomputed)",
                title_snip or abs_id,
                float(book.duration or 0.0),
                corrected_duration,
            )
        effective_duration = corrected_duration if corrected_duration is not None else book.duration

        if abs_finished and effective_duration and effective_duration > 0:
            abs_ts = float(effective_duration)
        abs_pct = 1.0 if abs_finished else self._abs_to_percentage(abs_ts, book, duration_override=effective_duration)

        prev_abs_ts = prev_state.timestamp if prev_state else 0
        prev_abs_pct = prev_state.percentage if prev_state else 0
        delta = abs(abs_ts - prev_abs_ts) if abs_ts else 0

        current = {'pct': abs_pct, 'ts': abs_ts}
        service_updated_at = parse_service_timestamp(abs_last_update)
        if service_updated_at is not None:
            current['service_updated_at'] = service_updated_at
        if corrected_duration is not None:
            current['service_duration'] = corrected_duration

        return ServiceState(
            current=current,
            previous_pct=prev_abs_pct,
            delta=delta,
            threshold=self.delta_abs_thresh,
            is_configured=True,
            display=("ABS", "{prev:.4%} -> {curr:.4%}"),
            value_seconds_formatter=lambda v: f"{v:.2f}s",
            value_formatter=lambda v: f"{v:.4%}",
        )

    _DURATION_DRIFT_TOLERANCE = 0.005

    @classmethod
    def _corrected_duration(cls, book: Book, service_duration) -> Optional[float]:
        try:
            candidate = float(service_duration)
        except (TypeError, ValueError):
            return None
        if candidate <= 0:
            return None
        stored = book.duration if book else None
        try:
            stored = float(stored) if stored else 0.0
        except (TypeError, ValueError):
            stored = 0.0
        if stored <= 0:
            return candidate
        if abs(candidate - stored) / stored <= cls._DURATION_DRIFT_TOLERANCE:
            return None
        return candidate

    def _abs_to_percentage(self, abs_seconds, book: Book, duration_override: Optional[float] = None):
        effective_duration = duration_override if (duration_override is not None and duration_override > 0) else book.duration
        if effective_duration and effective_duration > 0:
            return min(max(abs_seconds / effective_duration, 0.0), 1.0)

        transcript_path = book.transcript_file
        if not transcript_path:
            return None
        if transcript_path == "DB_MANAGED":
            if self.alignment_service:
                dur = self.alignment_service.get_book_duration(book.abs_id)
                if dur:
                    return min(max(abs_seconds / dur, 0.0), 1.0)
            return None
        try:
            if not os.path.exists(transcript_path):
                return None
            with open(transcript_path, 'r') as f:
                data = json.load(f)
                dur = data[-1]['end'] if isinstance(data, list) else data.get('duration', 0)
                return min(max(abs_seconds / dur, 0.0), 1.0) if dur > 0 else None
        except Exception as e:
            logger.debug(f"Failed to parse transcript for duration calculation: {e}")
            return None

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        abs_ts = state.current.get('ts')
        if not book or abs_ts is None:
            return None
        if book.transcript_file == "DB_MANAGED" and self.alignment_service:
            char_offset = self.alignment_service.get_char_for_time(book.abs_id, abs_ts)
            if char_offset is not None:
                book_path = self.ebook_parser.resolve_book_path(book.ebook_filename)
                if book_path and book_path.exists():
                    full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
                    start = max(0, char_offset - 50)
                    end = min(len(full_text), char_offset + 150)
                    return full_text[start:end]
            return None

        if hasattr(book, 'transcript_file') and book.transcript_file:
            path = Path(book.transcript_file)
            if not path.exists() and self.alignment_service:
                logger.warning(f"⚠️ '{book.abs_id}' Legacy transcript file missing: '{path}' — Attempting DB fallback")
                char_offset = self.alignment_service.get_char_for_time(book.abs_id, abs_ts)
                if char_offset is not None:
                    logger.info(f"✅ '{book.abs_id}' Found in DB despite missing file — Self-healing state")
                    book_path = self.ebook_parser.resolve_book_path(book.ebook_filename)
                    if book_path and book_path.exists():
                        full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
                        start = max(0, char_offset - 50)
                        end = min(len(full_text), char_offset + 150)
                        return full_text[start:end]
        return self.transcriber.get_text_at_time(book.transcript_file, abs_ts)

    def get_fallback_text(self, book: Book, state: ServiceState) -> Optional[str]:
        abs_ts = state.current.get('ts')
        if not book or abs_ts is None:
            return None
        if book.transcript_file == "DB_MANAGED" and self.alignment_service:
            earlier_ts = max(0, abs_ts - 10)
            return self.get_text_from_current_state(book, ServiceState({'ts': earlier_ts}))
        return self.transcriber.get_previous_segment_text(book.transcript_file, abs_ts)

    @staticmethod
    def _observed_abs_state(response: dict | None, book: Book, pct_fn) -> dict:
        response = response or {}
        abs_ts = response.get('currentTime')
        state = {
            'ts': abs_ts,
            'pct': pct_fn(abs_ts, book) if abs_ts is not None else 0,
        }
        service_updated_at = parse_service_timestamp(response.get('lastUpdate'))
        if service_updated_at is not None:
            state['service_updated_at'] = service_updated_at
        return state

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_title = book.abs_title or 'Unknown Book'
        if request.locator_result.percentage == 0.0:
            logger.info(f"🔄 '{book_title}' Locator percentage is 0.0% — Setting ABS progress to start of book")
            result, final_ts = self._update_abs_progress_with_offset(book.abs_id, 0.0)
            return SyncResult(
                final_ts,
                result.get("success", False),
                {'ts': final_ts, 'pct': 0.0},
                error_code=self._stale_item_error_code(book.abs_id, result),
            )

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
                logger.debug(f"🔍 '{book_title}' Alignment lookup skipped: No character index provided in request")
        elif book.transcript_file and book.transcript_file != "DB_MANAGED":
            ts_for_text = self.transcriber.find_time_for_text(
                book.transcript_file,
                request.txt,
                hint_percentage=request.locator_result.percentage,
                char_offset=request.locator_result.match_index,
                book_title=book_title,
            )

        if ts_for_text is not None:
            response = self.abs_client.get_progress(book.abs_id)
            abs_ts = response.get('currentTime') if response is not None else None
            if abs_ts is not None and ts_for_text < abs_ts:
                logger.info(
                    f"🔄 '{book_title}' Not updating ABS progress — target timestamp "
                    f"{ts_for_text:.2f}s is before current ABS position {abs_ts:.2f}s"
                )
                observed = self._observed_abs_state(response, book, self._abs_to_percentage)
                # Private key: SyncManager persists the genuine observed ABS state but
                # state_metadata_kwargs/locator persistence ignores underscore-prefixed
                # fields. The proposal is therefore available to the pending-decision
                # layer without fabricating ABS state.
                observed['_proposed_ts'] = float(ts_for_text)
                return SyncResult(abs_ts, True, observed, skipped=True)

            prev_ts = abs_ts if abs_ts is not None else 0.0
            time_listened = (ts_for_text - prev_ts) if request.credit_listening else 0.0
            result, final_ts = self._update_abs_progress_with_offset(
                book.abs_id, ts_for_text, prev_ts, time_listened=time_listened
            )
            pct = self._abs_to_percentage(final_ts, book)
            return SyncResult(
                final_ts,
                result.get("success", False),
                {'ts': final_ts, 'pct': pct or 0},
                error_code=self._stale_item_error_code(book.abs_id, result),
            )

        logger.warning(f"⚠️ '{book_title}' Not updating ABS progress — could not find timestamp for provided text")
        return SyncResult(None, False)

    def apply_approved_rewind(
        self,
        book: Book,
        target_ts: float,
        *,
        expected_current_ts: float,
        expected_service_updated_at: float | None = None,
    ) -> SyncResult:
        """Apply a previously approved rewind after one final live ABS compare.

        PendingRewindService already checks KoSync -> ABS -> KoSync. This final read
        closes the remaining target-side race window. A mismatch is a deliberate
        non-write (skipped=True), never an error and never an auto-retry write.
        """
        response = self.abs_client.get_progress(book.abs_id)
        if response is None or response.get('currentTime') is None:
            return SyncResult(None, False)

        observed = self._observed_abs_state(response, book, self._abs_to_percentage)
        current_ts = float(observed['ts'])
        live_service_ts = observed.get('service_updated_at')
        target_same = abs(current_ts - float(expected_current_ts)) <= 0.05
        service_same = (
            expected_service_updated_at is None
            or (
                live_service_ts is not None
                and abs(float(live_service_ts) - float(expected_service_updated_at)) <= 1e-3
            )
        )
        if not target_same or not service_same:
            logger.info(
                "⏪ Approved ABS rewind became stale before write for '%s'; keeping live position %.2fs",
                book.abs_id,
                current_ts,
            )
            return SyncResult(current_ts, True, observed, skipped=True)

        result, final_ts = self._update_abs_progress_with_offset(
            book.abs_id,
            float(target_ts),
            current_ts,
            time_listened=0.0,
        )
        pct = self._abs_to_percentage(final_ts, book)
        return SyncResult(
            final_ts,
            result.get("success", False),
            {'ts': final_ts, 'pct': pct or 0},
            error_code=self._stale_item_error_code(book.abs_id, result),
        )

    def _update_abs_progress_with_offset(self, abs_id, ts, prev_abs_ts: float = 0, time_listened: float = 0):
        adjusted_ts = max(round(ts + self.abs_progress_offset, 2), 0)
        if self.abs_progress_offset != 0:
            logger.debug(f"   📐 Adjusted timestamp: {ts}s → {adjusted_ts}s (offset: {self.abs_progress_offset:+.1f}s)")
        time_listened = max(0.0, round(float(time_listened or 0.0), 2))
        if time_listened > 0:
            logger.debug(f"   ⏱️ time_listened: {time_listened:.1f}s (listening push; prev: {prev_abs_ts:.1f}s → new: {adjusted_ts:.1f}s)")
        else:
            logger.debug(f"   ⏱️ time_listened: 0s (reading-driven push; prev: {prev_abs_ts:.1f}s → new: {adjusted_ts:.1f}s)")
        abs_ok = self.abs_client.update_progress(abs_id, adjusted_ts, time_listened)
        if isinstance(abs_ok, dict) and abs_ok.get("success"):
            try:
                from src.services.write_tracker import record_write
                record_write('ABS', abs_id)
            except ImportError:
                pass
        return abs_ok, adjusted_ts

    def _stale_item_error_code(self, abs_id: str, write_result) -> Optional[str]:
        succeeded = write_result.get("success") if isinstance(write_result, dict) else bool(write_result)
        if succeeded:
            return None
        try:
            exists = self.abs_client.item_exists(abs_id)
        except Exception as e:
            logger.debug(f"ABS stale-item probe failed for '{abs_id}': {e}", exc_info=True)
            return None
        if exists is False:
            logger.warning(
                f"⚠️ ABS library item not found for '{abs_id}' — the mapping looks stale "
                f"(the library item was renamed, moved, or removed in Audiobookshelf)"
            )
            return ABS_ITEM_NOT_FOUND
        return None
