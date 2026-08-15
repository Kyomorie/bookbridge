"""Daemon that keeps KoSync document hashes bound to their books.

A KoSync document hash is a hash of the ebook's bytes, so any edit that rewrites
the file — a metadata change in Calibre/CWA, a new cover, a re-stamped download —
produces a new hash and breaks the device's link to the book.

`KOReaderDeviceSyncService` already re-links the current hash as a sibling while
building the device-sync manifest, but that only runs once a device has asked for
a manifest. This daemon runs the same reconciliation on its own schedule so
installs that never use the managed-folder sync are covered too, and nobody has to
update a hash by hand.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_MINUTES = 360
_MIN_INTERVAL_MINUTES = 5

# Set when something observes a hash we cannot resolve, so the next pass runs now
# instead of up to a full interval later. Several signals coalesce into one pass.
_wake_event = threading.Event()


def signal_reconcile_soon() -> None:
    """Ask the reconciler to run its next pass immediately.

    Called when an unknown document hash turns up: the binding it needs may
    already exist on disk, and waiting a whole interval to discover that leaves
    the device unresolved in the meantime.
    """
    _wake_event.set()


def _reconcile_enabled() -> bool:
    """Read the enable toggle per call so the setting applies without a restart."""
    from src.utils.config_loader import env_truthy
    return env_truthy("KOSYNC_HASH_RECONCILE_ENABLED", "true")


def _reconcile_interval_seconds() -> int:
    """Reconcile interval in seconds, read per call and floored at 5 minutes."""
    raw = os.environ.get("KOSYNC_HASH_RECONCILE_MINUTES", str(_DEFAULT_INTERVAL_MINUTES))
    try:
        minutes = int(float(raw))
    except (TypeError, ValueError):
        minutes = _DEFAULT_INTERVAL_MINUTES
    return max(minutes, _MIN_INTERVAL_MINUTES) * 60


def run_hash_reconciler_daemon(device_sync_service, initial_delay_sec: float = 120.0) -> None:
    """Loop forever reconciling hashes. Intended as a daemon thread target."""
    if initial_delay_sec > 0:
        time.sleep(initial_delay_sec)

    logger.info("🔗 Hash reconciler thread started")
    while True:
        if _reconcile_enabled():
            try:
                device_sync_service.reconcile_hashes()
            except Exception as e:
                logger.warning("🔗 Hash reconcile pass failed: %s", e, exc_info=True)
        else:
            logger.debug("🔗 Hash reconcile disabled; skipping pass")

        # Wait out the interval, but wake early when an unresolvable hash is seen.
        # Signals raised while a pass was running coalesce into this single wait.
        _wake_event.clear()
        if _wake_event.wait(timeout=_reconcile_interval_seconds()):
            logger.info("🔗 Hash reconcile woken early by an unresolved document hash")


def start_hash_reconciler_thread(device_sync_service, initial_delay_sec: float = 120.0) -> threading.Thread:
    """Launch the reconciler in a daemon thread and return it."""
    thread = threading.Thread(
        target=run_hash_reconciler_daemon,
        args=(device_sync_service, initial_delay_sec),
        daemon=True,
        name="hash-reconciler",
    )
    thread.start()
    return thread
