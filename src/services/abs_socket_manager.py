"""ABS Socket.IO manager — supervises one listener per user (multi-user).

Audiobookshelf emits ``user_item_progress_updated`` only over the socket of the
user whose progress changed, so a single listener (authenticated as the admin)
never sees other users' playback. This manager starts one
:class:`ABSSocketListener` per active user that has their own ABS token, each
authenticated as that user and triggering that user's scoped sync cycle.

The global/admin listener (``user_id=None``) is always started when a global
``ABS_KEY`` is configured, preserving single-user behavior exactly. Per-user
listeners are only added for users whose resolved ABS token differs from the
global one, so an admin (whose token falls back to the global key) is not
double-listened.
"""

import logging
import os
import threading
import time

from src.services.abs_socket_listener import ABSSocketListener
from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)


class ABSSocketManager:
    """Starts and supervises per-user ABS Socket.IO listeners."""

    def __init__(self, database_service, sync_manager, user_client_registry=None):
        self._db = database_service
        self._sync_manager = sync_manager
        self._registry = user_client_registry
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # scope label -> the listener currently running for it (for clean stop()).
        self._current_listeners: dict = {}
        # Restart/backoff tuning (instance attrs so tests can zero them out).
        self._restart_base_secs = 5.0
        self._restart_max_secs = 60.0
        self._healthy_session_secs = 60.0

    def _listener_targets(self) -> list[tuple]:
        """Return ``[(user_id, server_url, token)]`` for each listener to start.

        Always includes the global listener (``user_id=None``) when a global
        ``ABS_KEY`` is set. Adds one per active user whose ABS client is
        configured with a token distinct from the global one.
        """
        global_server = os.environ.get("ABS_SERVER", "")
        global_token = os.environ.get("ABS_KEY", "")

        targets: list[tuple] = []
        seen_tokens: set[str] = set()

        if global_token:
            targets.append((None, global_server, global_token))
            seen_tokens.add(global_token)

        registry = self._registry
        if registry is None or not hasattr(self._db, "list_users"):
            return targets

        try:
            users = [u for u in self._db.list_users() if getattr(u, "active", 1)]
        except Exception as e:
            logger.warning("ABS Socket.IO: could not list users for per-user listeners: %s", e, exc_info=True)
            return targets

        for user in users:
            try:
                bundle = registry.get_clients(user.id)
            except Exception as e:
                logger.warning(
                    "ABS Socket.IO: skipping user %s (client build failed): %s",
                    getattr(user, "id", None), e, exc_info=True,
                )
                continue

            abs_sync = (getattr(bundle, "sync_clients", None) or {}).get("ABS")
            abs_client = getattr(abs_sync, "abs_client", None)
            if not abs_client or not abs_client.is_configured():
                continue

            token = resolve_setting(bundle.credentials, "ABS_KEY")
            if not token or token in seen_tokens:
                continue
            seen_tokens.add(token)
            server = resolve_setting(bundle.credentials, "ABS_SERVER", global_server)
            targets.append((user.id, server, token))

        return targets

    def start(self) -> None:
        """Start a supervised listener thread for every target."""
        targets = self._listener_targets()
        if not targets:
            logger.warning(
                "ABS Socket.IO: no configured ABS token found — no listeners started"
            )
            return

        for user_id, server, token in targets:
            scope = "global" if user_id is None else f"user {user_id}"
            thread = threading.Thread(
                target=self._supervise,
                args=(user_id, server, token, scope),
                daemon=True,
                name=f"abs-socket-{scope.replace(' ', '-')}",
            )
            thread.start()
            self._threads.append(thread)

        logger.info(
            "🔌 ABS Socket.IO: started %d supervised listener(s) — %s",
            len(targets),
            ", ".join("global" if uid is None else f"user {uid}" for uid, _, _ in targets),
        )

    def _supervise(self, user_id, server, token, scope: str) -> None:
        """Run one listener and restart it (with backoff) whenever it exits.

        ``ABSSocketListener.start()`` blocks while connected and only returns when
        the socket session ends — including an uncaught engineio teardown race
        (``write_loop_task`` ``None``) that kills the transport thread and leaves
        the client dead with no reconnect. Without this loop a dead listener stays
        dead until the process restarts, silently dropping real-time ABS instant
        sync (the poll cycle still covers it, just slower). A session that lasted a
        while resets the backoff so a transient death restarts promptly, while
        rapid immediate exits (bad token, ABS down) back off up to the cap.
        """
        backoff = self._restart_base_secs
        while not self._stop_event.is_set():
            listener = ABSSocketListener(
                abs_server_url=server,
                abs_api_token=token,
                database_service=self._db,
                sync_manager=self._sync_manager,
                user_id=user_id,
            )
            with self._lock:
                if self._stop_event.is_set():
                    break
                self._current_listeners[scope] = listener

            t0 = time.monotonic()
            try:
                listener.start()  # blocks until the socket session ends
            except Exception as e:
                logger.warning("🔌 ABS Socket.IO: %s listener crashed: %s", scope, e, exc_info=True)
            finally:
                listener.stop()

            if self._stop_event.is_set():
                break

            elapsed = time.monotonic() - t0
            if elapsed >= self._healthy_session_secs:
                backoff = self._restart_base_secs
            get_persistent_condition_logger().warn(
                logger,
                f"abs_socket:{user_id}",
                "🔌 ABS Socket.IO: %s listener exited after %.0fs — restarting in %.0fs",
                scope, elapsed, backoff,
            )
            self._stop_event.wait(backoff)
            backoff = min(backoff * 2, self._restart_max_secs)

    def stop(self) -> None:
        """Stop supervising and disconnect all listeners."""
        self._stop_event.set()
        with self._lock:
            listeners = list(self._current_listeners.values())
        for listener in listeners:
            try:
                listener.stop()
            except Exception as e:
                logger.debug("ABS Socket.IO: error stopping listener: %s", e)
