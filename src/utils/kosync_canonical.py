"""Persistent/prewarmed canonical XPath ordering for KoSync.

#386 bounds XPath ordering tightly but still resolves both XPaths from the EPUB
on a cache miss in the KoSync GET path.  This module keeps that decision logic
unchanged while moving the expensive work off the request path whenever
possible:

* bridge-generated KoSync writes already have the target XPath and a hot parser;
  resolve that target immediately and prewarm the current device-vs-synced pair
  in the background;
* safely prewarmed pairs are persisted in a tiny SQLite cache so a restart or
  the in-memory cache TTL does not force the same pair to be parsed again;
* persisted entries are tied to the local EPUB cache identity (path + mtime_ns +
  size, hashed before storage).  A normal changed/replaced EPUB is a miss.

If any lookup, DB operation, or background resolution fails, callers fall back
to the existing #386 behavior.  Canonical caching is therefore an optimization,
not a new source of truth.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from src.utils.logging_utils import get_persistent_condition_logger

logger = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kosync-canonical")
_PREWARM_LOCK = threading.Lock()
_PENDING_PREWARMS: dict[tuple, tuple] = {}
_ACTIVE_PREWARMS: set[tuple] = set()
_PERSISTED_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
_PERSISTED_MAX_PER_DOCUMENT = 64


def canonical_file_key(parser, filename: str) -> Optional[str]:
    """Return an opaque identity matching the parser cache file invariants."""
    filename = str(filename or "").strip()
    if not filename:
        return None
    try:
        path = Path(parser.resolve_book_path(filename)).resolve()
        stat = path.stat()
    except (OSError, TypeError, ValueError):
        return None
    material = f"{path}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def resolve_canonical_position(parser, filename: str, progress: str) -> tuple[Optional[int], Optional[str]]:
    """Resolve one XPath and bind the result to one unchanged EPUB version."""
    filename = str(filename or "").strip()
    progress = str(progress or "").strip()
    if not filename or not progress:
        return None, None

    before_key = canonical_file_key(parser, filename)
    if not before_key:
        return None, None

    index = parser.resolve_xpath_to_index(filename, progress)
    if index is None:
        return None, before_key
    try:
        index = int(index)
    except (TypeError, ValueError):
        return None, before_key
    if index < 0:
        return None, before_key

    after_key = canonical_file_key(parser, filename)
    if not after_key or after_key != before_key:
        logger.debug("KoSync canonical result discarded because '%s' changed during parse", filename)
        return None, None
    return index, before_key


def _pair_hash(cache_key: tuple) -> str:
    document_hash, filename, device_xpath, synced_xpath = cache_key
    material = "\0".join((
        str(document_hash or ""),
        str(filename or ""),
        str(device_xpath or ""),
        str(synced_xpath or ""),
    )).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _server_module():
    # Import lazily: kosync_server imports many application services and this
    # helper is also unit-tested in isolation.
    from src.api import kosync_server
    return kosync_server


def _database_service():
    try:
        return getattr(_server_module(), "_database_service", None)
    except Exception:
        return None


def _persist_pair(cache_key: tuple, device_index: Optional[int], synced_index: Optional[int], file_key: str) -> None:
    # #386 intentionally caches failed resolutions in RAM for ten minutes.  Do
    # not persist those failures across restarts/software upgrades: a newer
    # parser may be able to resolve the exact same pair later.
    if device_index is None or synced_index is None:
        return

    db = _database_service()
    if db is None or not file_key:
        return
    try:
        document_hash = str(cache_key[0])
        success = db.save_kosync_xpath_order(
            key_hash=_pair_hash(cache_key),
            document_hash=document_hash,
            filename=str(cache_key[1]),
            device_xpath=str(cache_key[2]),
            synced_xpath=str(cache_key[3]),
            device_index=device_index,
            synced_index=synced_index,
            file_key=file_key,
            ttl_seconds=_PERSISTED_CACHE_TTL_SECONDS,
            max_per_document=_PERSISTED_MAX_PER_DOCUMENT,
        )
        if success:
            get_persistent_condition_logger().resolve(
                logger,
                "kosync_xpath_order_cache",
                "✅ KoSync XPath order cache available again",
            )
    except Exception as exc:
        get_persistent_condition_logger().warn(
            logger,
            "kosync_xpath_order_cache",
            f"⚠️ KoSync XPath order cache unavailable: {exc}",
            exc_info=True,
        )


def load_persisted_pair(cache_key: tuple, parser) -> Optional[tuple[Optional[int], Optional[int]]]:
    """Load a persisted XPath order pair from the cache.

    Pairs resolved by the GET path are **not** persisted, because that path has
    no before/after file-version proof spanning its parse, so persisting them
    could bind pre-replacement indices to a file that changed mid-parse.
    Only `_prewarm_once`, whose two resolutions both pass
    `resolve_canonical_position`'s file-stability checks, persists.
    """
    db = _database_service()
    if db is None:
        return None
    try:
        current_key = canonical_file_key(parser, str(cache_key[1]))
        if not current_key:
            return None
        result = db.get_kosync_xpath_order(_pair_hash(cache_key), current_key)
        if result is not None:
            get_persistent_condition_logger().resolve(
                logger,
                "kosync_xpath_order_cache",
                "✅ KoSync XPath order cache available again",
            )
        return result
    except Exception as exc:
        get_persistent_condition_logger().warn(
            logger,
            "kosync_xpath_order_cache",
            f"⚠️ KoSync XPath order cache unavailable: {exc}",
            exc_info=True,
        )
        return None


def _current_user_id():
    try:
        from src.utils.user_context import get_current_user_id
        return get_current_user_id()
    except Exception:
        return None


def _drain_prewarm(key: tuple, parser) -> None:
    while True:
        with _PREWARM_LOCK:
            payload = _PENDING_PREWARMS.pop(key, None)
            if payload is None:
                _ACTIVE_PREWARMS.discard(key)
                return
        try:
            _prewarm_once(parser, *payload)
        except Exception as exc:
            logger.debug("KoSync canonical prewarm failed for %s: %s", key[0], exc, exc_info=True)


def _prewarm_once(parser, book, synced_xpath: str, synced_index: int, synced_file_key: str, user_id) -> bool:
    server = _server_module()
    db = getattr(server, "_database_service", None)
    if db is None:
        return False

    doc_id = str(getattr(book, "kosync_doc_id", "") or "").strip()
    if not doc_id:
        return False
    document = db.get_kosync_document(doc_id)
    filename = str(getattr(document, "filename", "") or "").strip() if document else ""
    if not filename:
        return False

    book_files = {
        str(getattr(book, "ebook_filename", "") or "").strip(),
        str(getattr(book, "original_ebook_filename", "") or "").strip(),
    }
    book_files.discard("")
    if filename not in book_files:
        return False

    current_file_key = canonical_file_key(parser, filename)
    if not current_file_key or current_file_key != synced_file_key:
        return False

    row = db.get_user_kosync_progress(doc_id, user_id)
    if row is None and document is not None:
        doc_user_id = getattr(document, "user_id", None)
        if user_id is None or doc_user_id in (None, user_id):
            row = document
    device_xpath = str(getattr(row, "progress", "") or "").strip() if row else ""
    if not device_xpath:
        return False

    device_index, device_file_key = resolve_canonical_position(parser, filename, device_xpath)
    if device_index is None or device_file_key != synced_file_key:
        return False

    cache_key = (doc_id, filename, device_xpath, synced_xpath)
    server._xpath_index_cache_put(cache_key, device_index, synced_index)
    # This function already runs on the background executor, and both canonical
    # resolutions have verified the same unchanged file identity.
    _persist_pair(cache_key, device_index, synced_index, synced_file_key)
    return True


def prewarm_xpath_order_cache(book, parser, synced_xpath: str, synced_index: Optional[int], synced_file_key: Optional[str]) -> bool:
    """Queue a coalesced device-vs-new-synced pair before the next KoSync GET."""
    try:
        synced_index = int(synced_index) if synced_index is not None else None
    except (TypeError, ValueError):
        return False
    synced_xpath = str(synced_xpath or "").strip()
    synced_file_key = str(synced_file_key or "").strip()
    doc_id = str(getattr(book, "kosync_doc_id", "") or "").strip() if book else ""
    if not doc_id or not synced_xpath or synced_index is None or synced_index < 0 or not synced_file_key:
        return False

    user_id = _current_user_id()
    key = (doc_id, user_id)
    payload = (book, synced_xpath, synced_index, synced_file_key, user_id)
    with _PREWARM_LOCK:
        _PENDING_PREWARMS[key] = payload
        if key in _ACTIVE_PREWARMS:
            return True
        _ACTIVE_PREWARMS.add(key)
    try:
        _EXECUTOR.submit(_drain_prewarm, key, parser)
    except RuntimeError:
        with _PREWARM_LOCK:
            _ACTIVE_PREWARMS.discard(key)
            _PENDING_PREWARMS.pop(key, None)
        return False
    return True
