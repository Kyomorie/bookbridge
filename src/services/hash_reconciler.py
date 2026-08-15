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

from src.utils.user_context import reset_current_user_id, set_current_user_id

logger = logging.getLogger(__name__)

# Per-user device-sync services, cached so their mtime/size hash caches survive
# between passes. Keyed by user id.
_user_services: dict = {}

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


def _scoped_service(global_service, registry, user_id: int):
    """Build a device-sync service that sees one user's books with their clients.

    The global bundle carries the admin's credentials, so a book belonging to
    another user — say their CWA library — can never be revalidated by it. Mirrors
    the client wiring of the global service, swapping in the user's API clients.
    """
    bundle = registry.get_clients(user_id)
    scoped = type(global_service)(
        database_service=global_service.database_service,
        ebook_parser=global_service.ebook_parser,
        abs_client=bundle.abs_client,
        booklore_client=bundle.booklore_client,
        cwa_client=bundle.cwa_client,
        kavita_client=global_service.kavita_client,
        epub_cache_dir=global_service.epub_cache_dir,
        bookorbit_client=bundle.bookorbit_client,
        user_id=user_id,
    )
    # A file's content hash does not depend on who is asking, and users share the
    # catalogue, so share the mtime/size cache instead of re-hashing the same
    # files once per user and again for the global sweep.
    scoped._content_hash_cache = global_service._content_hash_cache
    scoped._content_hash_cache_lock = global_service._content_hash_cache_lock
    return scoped


def _reconcile_all_users(global_service, registry, database_service) -> None:
    """Run one reconcile pass per active user, then a global sweep.

    Per-user services are cached across passes so their mtime/size hash caches
    survive; the global sweep still runs afterwards to catch books nobody has
    claimed (single-user installs land here exclusively).
    """
    users = []
    if registry is not None and database_service is not None:
        try:
            users = [u for u in database_service.list_users() if getattr(u, "active", 1)]
        except Exception as e:
            logger.warning("🔗 Hash reconcile could not list users: %s", e, exc_info=True)
            users = []

    for user in users:
        user_id = getattr(user, "id", None)
        if user_id is None:
            continue
        service = _user_services.get(user_id)
        if service is None:
            try:
                service = _scoped_service(global_service, registry, user_id)
            except Exception as e:
                logger.warning(
                    "🔗 Hash reconcile: could not build clients for user %s: %s",
                    user_id, e, exc_info=True,
                )
                continue
            _user_services[user_id] = service

        # Threads do not inherit contextvars, so bind the user explicitly for
        # anything downstream that resolves per-user settings.
        token = set_current_user_id(user_id)
        try:
            logger.debug("🔗 Hash reconcile: pass for user %s", user_id)
            service.reconcile_hashes()
        except Exception as e:
            logger.warning("🔗 Hash reconcile failed for user %s: %s", user_id, e, exc_info=True)
        finally:
            reset_current_user_id(token)

    global_service.reconcile_hashes()


def run_hash_reconciler_daemon(device_sync_service, initial_delay_sec: float = 120.0,
                               user_client_registry=None, database_service=None) -> None:
    """Loop forever reconciling hashes. Intended as a daemon thread target."""
    if initial_delay_sec > 0:
        time.sleep(initial_delay_sec)

    logger.info("🔗 Hash reconciler thread started")
    while True:
        if _reconcile_enabled():
            try:
                _reconcile_all_users(device_sync_service, user_client_registry, database_service)
            except Exception as e:
                logger.warning("🔗 Hash reconcile pass failed: %s", e, exc_info=True)
        else:
            logger.debug("🔗 Hash reconcile disabled; skipping pass")

        # Wait out the interval, but wake early when an unresolvable hash is seen.
        # Signals raised while a pass was running coalesce into this single wait.
        _wake_event.clear()
        if _wake_event.wait(timeout=_reconcile_interval_seconds()):
            logger.info("🔗 Hash reconcile woken early by an unresolved document hash")


def start_hash_reconciler_thread(device_sync_service, initial_delay_sec: float = 120.0,
                                 user_client_registry=None,
                                 database_service=None) -> threading.Thread:
    """Launch the reconciler in a daemon thread and return it."""
    thread = threading.Thread(
        target=run_hash_reconciler_daemon,
        args=(device_sync_service, initial_delay_sec, user_client_registry, database_service),
        daemon=True,
        name="hash-reconciler",
    )
    thread.start()
    return thread
