"""Resolve a saved BookBridge reading position into a bounded text preview.

This module is intentionally UI-agnostic.  It selects the same per-user progress
state the dashboard considers current, resolves precise ebook locators when they
exist, maps audio time through the stored alignment when necessary, and only then
falls back to an explicitly approximate percentage position.

The returned payload never contains a raw ebook path, locator, or character index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional


_AUDIO_CLIENTS = {"abs", "bookloreaudio", "bookorbitaudio"}
_SOURCE_LABELS = {
    "abs": "Audiobookshelf",
    "kosync": "KoSync",
    "storyteller": "Storyteller",
    "booklore": "Grimmory",
    "bookloreaudio": "Grimmory Audio",
    "bookorbit": "BookOrbit",
    "bookorbitaudio": "BookOrbit Audio",
    "bookfusion": "BookFusion",
    "cwa": "CWA",
    "readest": "Readest",
}


@dataclass(frozen=True)
class _ResolvedPosition:
    index: int
    status: str
    confidence: str
    detail: str = ""


def _client_key(name: str | None) -> str:
    key = str(name or "").strip().lower()
    if key.startswith("kosync") or key == "bridgesync_plugin":
        return "kosync"
    if key in {"abs", "absebook"}:
        return "abs"
    return key


def _source_label(name: str | None) -> str:
    key = _client_key(name)
    if key in _SOURCE_LABELS:
        return _SOURCE_LABELS[key]
    raw = str(name or "").strip()
    return raw or "BookBridge"


def _as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _select_state(states: Iterable, last_leader: str | None):
    states = list(states or [])
    if not states:
        return None

    by_key = {}
    for state in states:
        key = _client_key(getattr(state, "client_name", None))
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None or (_as_float(getattr(state, "last_updated", None)) or 0) > (
            _as_float(getattr(existing, "last_updated", None)) or 0
        ):
            by_key[key] = state

    leader_key = _client_key(last_leader)
    if leader_key and leader_key in by_key:
        return by_key[leader_key]

    # ReadingSession data can be absent for old/imported rows.  In that case the
    # newest observed state is the least surprising display-only fallback.
    return max(
        states,
        key=lambda state: _as_float(getattr(state, "last_updated", None)) or 0,
    )


def _resolve_filename(book, ebook_parser) -> Optional[str]:
    candidates = []
    for value in (
        getattr(book, "original_ebook_filename", None),
        getattr(book, "ebook_filename", None),
    ):
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    for filename in candidates:
        try:
            ebook_parser.resolve_book_path(filename)
            return filename
        except (FileNotFoundError, OSError):
            continue
    return None


def _resolve_precise_or_mapped_position(
    *,
    state,
    filename: str,
    book,
    ebook_parser,
    alignment_service,
) -> tuple[Optional[_ResolvedPosition], list[str]]:
    failures: list[str] = []

    xpath = str(getattr(state, "xpath", None) or "").strip()
    if xpath:
        index = ebook_parser.resolve_xpath_to_index(filename, xpath)
        if index is not None:
            return _ResolvedPosition(index, "exact", "Exact · XPath"), failures
        failures.append("XPath")

    cfi = str(getattr(state, "cfi", None) or "").strip()
    if cfi.startswith("epubcfi("):
        index = ebook_parser.resolve_cfi_to_index(filename, cfi)
        if index is not None:
            return _ResolvedPosition(index, "exact", "Exact · CFI"), failures
        failures.append("CFI")

    client_key = _client_key(getattr(state, "client_name", None))
    timestamp = _as_float(getattr(state, "timestamp", None))
    if client_key in _AUDIO_CLIENTS and timestamp is not None and alignment_service is not None:
        try:
            index = alignment_service.get_char_for_time(getattr(book, "abs_id", ""), timestamp)
        except Exception:
            index = None
        if index is not None:
            return _ResolvedPosition(index, "mapped", "Mapped · audio alignment"), failures

    return None, failures


def _bounded_excerpt(full_text: str, index: int, context: int) -> tuple[str, str]:
    if not full_text:
        return "", ""
    index = max(0, min(int(index), len(full_text)))
    context = max(80, min(int(context), 300))

    before = full_text[max(0, index - context):index]
    after = full_text[index:min(len(full_text), index + context)]

    # The canonical parser already normalizes chapter text substantially, but
    # collapse remaining line breaks/tabs for a compact dashboard excerpt.  Do it
    # independently on both sides so the marker still represents the boundary.
    before = re.sub(r"\s+", " ", before).strip()
    after = re.sub(r"\s+", " ", after).strip()
    return before, after


def unavailable_preview(message: str, *, source: str = "BookBridge", percentage=None) -> dict:
    result = {
        "status": "unavailable",
        "source": source,
        "confidence": "Unavailable",
        "before": "",
        "after": "",
        "message": message,
    }
    if percentage is not None:
        result["percentage"] = round(float(percentage) * 100, 1)
    return result


def build_reading_position_preview(
    *,
    book,
    states: Iterable,
    last_leader: str | None,
    ebook_parser,
    alignment_service=None,
    context_chars: int = 220,
) -> dict:
    """Return a small human-readable preview for one book's current user state.

    Resolution order is deliberately confidence-first:
      XPath -> EPUB CFI -> stored audio alignment -> percentage estimate.

    A failed precise locator never disappears: if percentage fallback is possible
    the payload remains explicitly ``approximate`` and explains that the precise
    locator could not be resolved.
    """
    state = _select_state(states, last_leader)
    if state is None:
        return unavailable_preview("No saved reading position is available for this book.")

    source = _source_label(getattr(state, "client_name", None))
    percentage = _as_float(getattr(state, "percentage", None))
    filename = _resolve_filename(book, ebook_parser)
    if not filename:
        return unavailable_preview(
            "The linked ebook file is not available to resolve this position.",
            source=source,
            percentage=percentage,
        )

    try:
        book_path = ebook_parser.resolve_book_path(filename)
        full_text, _spine_map = ebook_parser.extract_text_and_map(book_path)
    except Exception:
        full_text = ""

    if not full_text:
        return unavailable_preview(
            "BookBridge could not read text from the linked ebook.",
            source=source,
            percentage=percentage,
        )

    resolved, precise_failures = _resolve_precise_or_mapped_position(
        state=state,
        filename=filename,
        book=book,
        ebook_parser=ebook_parser,
        alignment_service=alignment_service,
    )

    detail = ""
    if resolved is None and percentage is not None:
        pct = max(0.0, min(1.0, percentage))
        index = int(round(pct * max(0, len(full_text) - 1)))
        if precise_failures:
            detail = (
                f"Stored {' and '.join(precise_failures)} could not be resolved; "
                "showing the saved percentage as an estimate."
            )
        else:
            detail = "No exact ebook locator is available; showing the saved percentage as an estimate."
        resolved = _ResolvedPosition(index, "approximate", "Approximate · percentage", detail)

    if resolved is None:
        message = "No reliable ebook text position can be resolved from the saved state."
        if precise_failures:
            message = f"Stored {' and '.join(precise_failures)} could not be resolved safely."
        return unavailable_preview(message, source=source, percentage=percentage)

    index = max(0, min(int(resolved.index), len(full_text)))
    before, after = _bounded_excerpt(full_text, index, context_chars)
    if not before and not after:
        return unavailable_preview(
            "The saved position resolved, but no surrounding ebook text is available.",
            source=source,
            percentage=percentage,
        )

    payload = {
        "status": resolved.status,
        "source": source,
        "confidence": resolved.confidence,
        "before": before,
        "after": after,
        "message": resolved.detail,
    }
    if percentage is not None:
        payload["percentage"] = round(max(0.0, min(1.0, percentage)) * 100, 1)
    return payload
