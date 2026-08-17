#!/usr/bin/env python3
"""
Leader-selection GUARD decisions visible in the diagnostics evidence ring
(which captures INFO+). Log-line-only changes to sync_manager.py -- zero
behavior change to _determine_leader or sync_cycle.

Covers three changes:
  1. The raw/locator pct-mismatch staleness suppression inside
     _determine_leader (previously logger.debug, silent to the evidence
     ring) is now logger.info, with the exact same message text.
  2. The ABS-ebook CFI hydration guard -- a second, previously-silent
     consult site of _locator_collapsed_to_start, distinct from the
     already-WARNING main skip-write guard covered by
     tests/test_kosync_collapse_guard.py -- now logs a "Collapse guard:"
     INFO line when it blocks a hydrated CFI that resolved to start-of-book.
  3. The two "{leader} leads at ..." lines that fall through to a raw
     percentage "furthest wins" comparison (no cross-format normalization
     available) now carry a short factual reason suffix, matching the style
     of the existing "(only client with change)" / "(normalized: ...)"
     lines.
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase
from src.sync_clients.sync_client_interface import LocatorResult, ServiceState
from src.sync_manager import SyncManager


def _state(current: dict, previous_pct: float = 0.0, delta: float = 0.0) -> ServiceState:
    return ServiceState(
        current=current,
        previous_pct=previous_pct,
        delta=delta,
        threshold=0.01,
        is_configured=True,
        display=("X", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda v: f"{v:.4%}",
    )


def _manager(delta_clients, client_names=("ABS", "KoSync", "BookLore")):
    """A bare SyncManager exercising only _determine_leader (mirrors the
    scaffolding in tests/test_freshness_guards.py)."""
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {name: _Client() for name in client_names}
    manager._has_significant_delta = MagicMock(
        side_effect=lambda name, cfg, book: name in delta_clients
    )
    manager._normalize_for_cross_format_comparison = MagicMock(return_value=None)
    manager.sync_delta_between_clients = 0.01
    return manager


def _book():
    return SimpleNamespace(duration=10000, transcript_file=None, sync_mode="audiobook")


class TestStalePctDeltaSuppressionLogging(unittest.TestCase):
    """Change 1: the locator-vs-raw pct mismatch staleness suppression must
    be visible at INFO with unchanged message text."""

    def test_promoted_to_info_with_unchanged_text(self):
        manager = _manager(delta_clients={"KoSync"})
        config = {
            "ABS": _state({"pct": 0.60}),
            "KoSync": _state(
                {"pct": 0.53, "_locator_pct": 0.10},  # raw/locator mismatch > 1%
                previous_pct=0.10,  # locator matches previous pct -> no real movement
            ),
        }

        with self.assertLogs("src.sync_manager", level="INFO") as cm:
            leader, leader_pct = manager._determine_leader(config, _book(), "abs-1", "book")

        matches = [
            line for line in cm.output
            if line.startswith("INFO:")
            and "Ignoring stale pct delta for 'KoSync'" in line
            and "(raw=53.0000%, locator=10.0000%, prev=10.0000%)" in line
        ]
        self.assertTrue(
            matches,
            f"Expected the promoted INFO staleness line; got: {cm.output}",
        )
        # KoSync's stale delta is suppressed -> ABS (the only remaining
        # candidate) wins the fallback furthest-wins comparison.
        self.assertEqual(leader, "ABS")
        self.assertEqual(leader_pct, 0.60)


class TestFurthestWinsReasonSuffixes(unittest.TestCase):
    """Change 3: the two previously-bare "{leader} leads at ..." lines now
    carry a short, cheaply-derived reason suffix."""

    def test_same_format_fallback_gets_reason(self):
        """No cross-format normalization at all -> raw percentage comparison."""
        manager = _manager(delta_clients={"ABS", "KoSync"})
        config = {
            "ABS": _state({"pct": 0.60}),
            "KoSync": _state({"pct": 0.70}),
        }

        with self.assertLogs("src.sync_manager", level="INFO") as cm:
            leader, leader_pct = manager._determine_leader(config, _book(), "abs-1", "book")

        self.assertEqual(leader, "KoSync")
        self.assertEqual(leader_pct, 0.70)
        matches = [
            line for line in cm.output
            if "KoSync leads at" in line
            and "(furthest progress, same-format comparison)" in line
        ]
        self.assertTrue(matches, f"Expected the same-format reason suffix; got: {cm.output}")

    def test_no_normalized_candidates_fallback_gets_reason(self):
        """Normalized positions exist, but none belong to the delta candidates
        -> falls back to percentage comparison among candidates."""
        manager = _manager(delta_clients={"ABS", "KoSync"})
        manager._normalize_for_cross_format_comparison = MagicMock(
            return_value={"BookLore": 200.0, "Storyteller": 300.0}
        )
        config = {
            "ABS": _state({"pct": 0.60}),
            "KoSync": _state({"pct": 0.70}),
        }

        with self.assertLogs("src.sync_manager", level="INFO") as cm:
            leader, leader_pct = manager._determine_leader(config, _book(), "abs-1", "book")

        self.assertEqual(leader, "KoSync")
        self.assertEqual(leader_pct, 0.70)
        matches = [
            line for line in cm.output
            if "KoSync leads at" in line
            and "(furthest progress, no normalized candidates)" in line
        ]
        self.assertTrue(matches, f"Expected the no-normalized-candidates reason suffix; got: {cm.output}")


class TestCollapseGuardHydrationLogging(BaseSyncCycleTestCase):
    """Change 2: the ABS-ebook CFI hydration guard must not silently swallow
    a blocked hydration. Drives the real sync_cycle() end-to-end (mocked
    clients), mirroring tests/test_kosync_collapse_guard.py, but arranges the
    *hydration* round-trip (not the leader's own locator) to collapse to
    start-of-book: KoSync leads at 53% via a text match whose match_index (0)
    disagrees with its own percentage, so the char-offset hydration attempted
    for the ABS ebook target round-trips back to ~0% while the leader itself
    is materially ahead."""

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-cfi-collapse',
            'abs_title': 'CFI Hydration Collapse Guard Test Book',
            'kosync_doc_id': 'test-kosync-doc-cfi-collapse',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active',
        }

    def get_test_state_data(self):
        return {
            'abs': {'pct': 0.10, 'ts': 100.0, 'last_updated': 1234567890},
            'kosync': {'pct': 0.0, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "KoSync"

    def get_expected_final_percentage(self):
        return 0.53

    def get_progress_mock_returns(self):
        return {
            'abs_progress': {'currentTime': 100.0, 'duration': 1000},  # 10%
            'abs_in_progress': [{'id': 'test-abs-id-cfi-collapse', 'progress': 0.10, 'duration': 1000}],
            'kosync_progress': (0.53, "/body/DocFragment[1]/body/p[1]"),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (0.0, None),
        }

    def setUp(self):
        super().setUp()
        # SyncManager.__init__ -> _setup_sync_clients() drops any client whose
        # is_configured() is False before it ever reaches self.sync_clients;
        # ABSEbookSyncClient.is_configured() requires this flag.
        self._prev_sync_abs_ebook = os.environ.get('SYNC_ABS_EBOOK')
        os.environ['SYNC_ABS_EBOOK'] = 'true'

    def tearDown(self):
        if self._prev_sync_abs_ebook is None:
            os.environ.pop('SYNC_ABS_EBOOK', None)
        else:
            os.environ['SYNC_ABS_EBOOK'] = self._prev_sync_abs_ebook
        super().tearDown()

    def _build_manager(self):
        mocks = self.setup_common_mocks()

        # Leader (KoSync) resolves via text match to 53%, but with a
        # match_index of 0 -- an inconsistency the roundtrip-offset check
        # downstream is designed to catch.
        mocks['ebook_parser'].resolve_xpath.return_value = "some text near 53%"
        mocks['ebook_parser'].get_text_at_percentage.return_value = "some text near 53%"
        mocks['ebook_parser'].find_text_location.return_value = LocatorResult(
            percentage=0.53, match_index=0
        )
        mocks['ebook_parser'].get_perfect_ko_xpath.return_value = None

        # ABS ebook CFI hydration: the char-offset round-trip resolves back
        # near start-of-book (offset 2 of 10000) even though the leader is
        # materially ahead -- the hydrated CFI must be rejected.
        mocks['ebook_parser'].get_locator_from_char_offset.return_value = LocatorResult(
            percentage=0.0002, cfi="epubcfi(/6/2!/4/2/1:0)"
        )
        mocks['ebook_parser'].resolve_cfi_to_index.return_value = 2

        # ABSEbookSyncClient.get_service_state reads this (distinct from the
        # plain ABS audio client's get_progress mock above).
        mocks['abs_client'].get_progress_with_status.return_value = (
            {"ebookProgress": 0.0, "ebookLocation": None}, 200
        )

        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "text"
        transcriber.find_time_for_text.return_value = 530.0

        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
        from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
        from src.sync_clients.booklore_sync_client import BookloreSyncClient

        abs_sync_client = ABSSyncClient(mocks['abs_client'], transcriber, mocks['ebook_parser'])
        kosync_sync_client = KoSyncSyncClient(mocks['kosync_client'], mocks['ebook_parser'])
        abs_ebook_sync_client = ABSEbookSyncClient(mocks['abs_client'], mocks['ebook_parser'])
        storyteller_sync_client = StorytellerSyncClient(mocks['storyteller_client'], mocks['ebook_parser'])
        booklore_sync_client = BookloreSyncClient(mocks['booklore_client'], mocks['ebook_parser'])

        manager = SyncManager(
            abs_client=mocks['abs_client'],
            booklore_client=mocks['booklore_client'],
            transcriber=transcriber,
            ebook_parser=mocks['ebook_parser'],
            database_service=mocks['database_service'],
            sync_clients={
                "ABS": abs_sync_client,
                # Unspaced key: matches the literal "ABSEbook" the hydration
                # guard checks for (di_container.py / user_client_registry.py
                # register it the same way; only the *display* string has a
                # space).
                "ABSEbook": abs_ebook_sync_client,
                "KoSync": kosync_sync_client,
                "Storyteller": storyteller_sync_client,
                "BookLore": booklore_sync_client,
            },
            epub_cache_dir=Path(self.temp_dir) / 'epub_cache',
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / 'books',
        )

        manager.sync_clients['ABS']._update_abs_progress_with_offset = Mock(
            return_value=({"success": True}, 530.0)
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()
        manager._get_local_epub = Mock(return_value=str(Path(self.temp_dir) / 'books' / 'test-book.epub'))
        # Bypass real ebook text/caching -- only total_len and truthiness matter here.
        manager._get_cached_ebook_text = Mock(return_value=("dummy full text " * 200, 10000))
        return manager, mocks

    def test_blocked_hydration_logs_at_info(self):
        manager, mocks = self._build_manager()

        with self.assertLogs("src.sync_manager", level="INFO") as cm:
            manager.sync_cycle()

        collapse_lines = [line for line in cm.output if "Collapse guard:" in line]
        self.assertTrue(
            collapse_lines,
            f"Expected an INFO 'Collapse guard:' line for the blocked ABS-ebook "
            f"CFI hydration; got: {cm.output}",
        )
        info_line = collapse_lines[0]
        self.assertTrue(info_line.startswith("INFO:"), info_line)
        self.assertIn("\U0001f573", info_line)  # the new guard emoji (\U0001F573 = "hole")
        self.assertIn("KoSync", info_line)
        self.assertIn("start-of-book", info_line)

        # The hydrated locator must actually have been rejected: ABSEbook
        # never received the (invalid) hydrated CFI update.
        self.assertFalse(
            any(
                "Hydrated missing ABS ebook CFI" in line
                for line in cm.output
            ),
            "The hydration-succeeded debug path should not also have fired",
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
