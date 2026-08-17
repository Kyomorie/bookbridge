"""The device-sync manifest cache must survive a restart.

Building a manifest walks the whole catalogue — minutes on a large library — and
the in-memory cache dies with the process. Readers commonly sync straight after a
restart, so a cold start previously blocked them on an inline rebuild.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from src.api import kosync_server


class TestManifestCachePersistence(unittest.TestCase):
    def setUp(self):
        self._saved_container = kosync_server._container
        self._saved_cache = kosync_server._manifest_cache

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)

        container = MagicMock()
        container.data_dir.return_value = self.data_dir
        kosync_server._container = container
        kosync_server._manifest_cache = None

    def tearDown(self):
        kosync_server._container = self._saved_container
        kosync_server._manifest_cache = self._saved_cache

    def _manifest(self, revision="abc123"):
        return {
            "generated_at": 1,
            "revision": revision,
            "delete_mode": "mirror",
            "books": [{"abs_id": "a", "title": "A", "content_hash": "h", "filename": "a.epub"}],
        }

    def test_persist_then_load_round_trip(self):
        manifest = self._manifest()
        kosync_server._persist_manifest_cache(manifest)

        self.assertTrue((self.data_dir / "device_sync_manifest.json").exists())
        self.assertEqual(kosync_server._load_persisted_manifest(), manifest)

    def test_load_returns_none_when_never_written(self):
        self.assertIsNone(kosync_server._load_persisted_manifest())

    def test_corrupt_file_is_ignored_rather_than_raising(self):
        (self.data_dir / "device_sync_manifest.json").write_text("{not json")
        self.assertIsNone(kosync_server._load_persisted_manifest())

    def test_wrong_shape_is_rejected(self):
        (self.data_dir / "device_sync_manifest.json").write_text(json.dumps({"nope": 1}))
        self.assertIsNone(kosync_server._load_persisted_manifest())

    def test_persist_is_atomic_and_leaves_no_temp_file(self):
        kosync_server._persist_manifest_cache(self._manifest())
        leftovers = [p.name for p in self.data_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_rewrite_replaces_the_previous_manifest(self):
        kosync_server._persist_manifest_cache(self._manifest(revision="old"))
        kosync_server._persist_manifest_cache(self._manifest(revision="new"))
        self.assertEqual(kosync_server._load_persisted_manifest()["revision"], "new")

    def test_missing_data_dir_is_a_no_op(self):
        container = MagicMock()
        container.data_dir.side_effect = RuntimeError("no data dir")
        kosync_server._container = container

        kosync_server._persist_manifest_cache(self._manifest())  # must not raise
        self.assertIsNone(kosync_server._load_persisted_manifest())


class TestDeviceSyncActivity(unittest.TestCase):
    def setUp(self):
        self._saved = kosync_server._last_device_sync_activity

    def tearDown(self):
        kosync_server._last_device_sync_activity = self._saved

    def test_never_active_reports_infinity(self):
        kosync_server._last_device_sync_activity = 0.0
        self.assertEqual(kosync_server.seconds_since_device_sync_activity(), float("inf"))

    def test_recording_activity_resets_the_clock(self):
        kosync_server.note_device_sync_activity()
        self.assertLess(kosync_server.seconds_since_device_sync_activity(), 5)


if __name__ == "__main__":
    unittest.main()
