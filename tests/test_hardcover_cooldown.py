import os
import threading
import unittest
from unittest.mock import patch

from src.db.models import State
from src.sync_clients.sync_client_interface import SyncResult
from src.sync_manager import SyncManager


class FakeServiceState:
    """Minimal stand-in for ServiceState — the handler only reads .current['pct']."""

    def __init__(self, pct):
        self.current = {'pct': pct}


class FakeBook:
    def __init__(self, abs_id):
        self.abs_id = abs_id


class FakeHardcoverClient:
    def __init__(self, configured=True):
        self._configured = configured
        self.calls = []  # percentages passed to update_progress

    def is_configured(self):
        return self._configured

    def update_progress(self, book, request):
        pct = request.locator_result.percentage
        self.calls.append(pct)
        return SyncResult(location=pct, success=True)


class FakeDatabaseService:
    def __init__(self):
        self.states = {}  # (abs_id, client_name) -> State
        self.saved = []

    def get_state(self, abs_id, client_name):
        return self.states.get((abs_id, client_name))

    def save_state(self, state):
        self.states[(state.abs_id, state.client_name)] = state
        self.saved.append(state)
        return state


class FakeTimer:
    instances = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or []
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args, **self.kwargs)


class TestHardcoverCooldown(unittest.TestCase):
    def setUp(self):
        self.hc = FakeHardcoverClient()
        self.db = FakeDatabaseService()
        self.mgr = SyncManager.__new__(SyncManager)
        self.mgr.sync_clients = {'Hardcover': self.hc}
        self.mgr.database_service = self.db
        self.mgr._hardcover_cooldown = {}
        self.mgr._hardcover_cooldown_lock = threading.Lock()
        self.mgr._tracker_cooldown_timers = {}
        self.mgr._tracker_cooldown_timers_lock = threading.Lock()
        FakeTimer.instances = []
        self.timer_patch = patch('src.sync_manager.threading.Timer', FakeTimer)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        os.environ['HARDCOVER_UPDATE_COOLDOWN_MINS'] = '60'
        self.book = FakeBook('book1')

    def tearDown(self):
        os.environ.pop('HARDCOVER_UPDATE_COOLDOWN_MINS', None)

    def _config(self, pct):
        return {'ABS': FakeServiceState(pct)}

    def test_progress_changed_does_not_post(self):
        # First observation starts the cooldown; nothing should post yet.
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.5), now=0.0)
        self.assertEqual(self.hc.calls, [])

    def test_posts_once_after_idle_then_no_duplicate(self):
        # t=0: progress observed, cooldown starts.
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.5), now=0.0)
        # t=3600 (60 min idle, pct unchanged): settled -> post.
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.5), now=3600.0)
        self.assertEqual(self.hc.calls, [0.5])
        # Saved Hardcover state reflects the post.
        saved = self.db.get_state('book1', 'hardcover')
        self.assertIsNotNone(saved)
        self.assertAlmostEqual(saved.percentage, 0.5)
        # t=3601: same pct, already in sync -> no duplicate post.
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.5), now=3601.0)
        self.assertEqual(self.hc.calls, [0.5])

    def test_five_minute_cooldown_posts_without_later_external_sync_cycle(self):
        os.environ['HARDCOVER_UPDATE_COOLDOWN_MINS'] = '5'
        config = self._config(0.5)
        self.mgr.user_client_registry = None
        self.mgr._sync_lock = threading.Lock()
        self.mgr._post_cycle_callbacks = []
        self.mgr._dispatch_pending_syncs = lambda: None
        self.mgr._sync_cycle_internal = lambda target_abs_id=None: (
            self.mgr._handle_hardcover_cooldown(self.book, config, now=300.0)
        )

        self.mgr._handle_hardcover_cooldown(self.book, config, now=0.0)

        self.assertEqual(self.hc.calls, [])
        self.assertEqual(len(FakeTimer.instances), 1)
        timer = FakeTimer.instances[0]
        self.assertEqual(timer.interval, 300)
        self.assertTrue(timer.daemon)
        self.assertTrue(timer.started)

        with self.assertLogs('src.sync_manager', level='INFO') as captured:
            timer.fire()

        self.assertEqual(self.hc.calls, [0.5])
        self.assertTrue(any(
            "Hardcover cooldown post: 50.0% (idle≥5m)" in line
            for line in captured.output
        ))

    def test_resume_within_cooldown_resets_timer(self):
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.5), now=0.0)
        # t=1800: progress moved (brief pause ended) -> timer resets.
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.55), now=1800.0)
        # t=2000: only 200s since the reset -> not settled, no post.
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.55), now=2000.0)
        self.assertEqual(self.hc.calls, [])

    def test_completion_bypasses_cooldown(self):
        # Completion on the very first observation, well inside the cooldown window.
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.995), now=0.0)
        self.assertEqual(len(self.hc.calls), 1)
        self.assertAlmostEqual(self.hc.calls[0], 0.995)

    def test_zero_cooldown_posts_immediately(self):
        os.environ['HARDCOVER_UPDATE_COOLDOWN_MINS'] = '0'
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.5), now=0.0)
        self.assertEqual(self.hc.calls, [0.5])

    def test_unconfigured_client_is_skipped(self):
        self.hc._configured = False
        self.mgr._handle_hardcover_cooldown(self.book, self._config(0.995), now=0.0)
        self.assertEqual(self.hc.calls, [])


if __name__ == '__main__':
    unittest.main()
