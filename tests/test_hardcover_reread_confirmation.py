#!/usr/bin/env python3
"""
Regression coverage for two-cycle Hardcover reread confirmation.

A single low percentage cannot tell a genuine reread apart from a stale reader
re-reporting an old position — a KOReader closed at 4%, left behind while the
audiobook was finished elsewhere, reports that same 4% the next time it is
opened. Creating a Hardcover read from that one sample invents history.

So a completed read is never rewritten, and a new read is only inserted once the
position has ADVANCED past a recorded candidate anchor on a LATER sync cycle.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database_service import DatabaseService
from src.db.models import Book, HardcoverDetails, State
from src.sync_clients.hardcover_sync_client import (
    REREAD_CANDIDATE_FIRST_SEEN_KEY,
    REREAD_CANDIDATE_PCT_KEY,
    HardcoverSyncClient,
)
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.utils.progress_metadata import state_metadata_kwargs

ABS_ID = 'reread-book'
HARDCOVER_CLIENT_NAME = 'hardcover'


class TestHardcoverRereadConfirmation(unittest.TestCase):
    """The reread must be confirmed across two cycles before any read is created."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.database_service = DatabaseService(str(Path(self.temp_dir) / 'test_reread.db'))

        self.mock_hardcover_client = Mock()
        self.mock_hardcover_client.is_configured.return_value = True
        self.mock_hardcover_client.update_progress.return_value = True

        self.sync_client = HardcoverSyncClient(
            hardcover_client=self.mock_hardcover_client,
            ebook_parser=Mock(),
            abs_client=Mock(),
            database_service=self.database_service,
        )

        self.book = Book(
            abs_id=ABS_ID,
            abs_title='A Book Worth Rereading',
            ebook_filename='reread.epub',
            status='active',
            duration=7200.0,
        )
        self.database_service.save_book(self.book)

        # Already finished on Hardcover, and already at status 3 so no status
        # transition fires and _handle_status_transition yields no active read.
        self.mock_hardcover_client.get_user_book.return_value = {
            'id': 'user-book-1',
            'status_id': 3,
        }
        self.completed_read = {
            'id': 'completed-read-1',
            'started_at': '2026-07-01',
            'finished_at': '2026-07-05',
        }
        self.mock_hardcover_client.get_latest_read.return_value = self.completed_read

        self._save_details(pages=200)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ---------------------------------------------------------------- helpers

    def _save_details(self, pages=None, audio_seconds=None):
        self.database_service.save_hardcover_details(HardcoverDetails(
            abs_id=ABS_ID,
            hardcover_book_id='book-1',
            hardcover_edition_id='edition-1',
            hardcover_pages=pages if pages is not None else -1,
            hardcover_audio_seconds=audio_seconds,
            matched_by='test',
        ))

    def _cycle(self, percentage):
        """Run one sync cycle and persist its result the way sync_manager does.

        Going through the real ``state_metadata_kwargs`` means these tests
        exercise the actual locator_json round-trip the candidate relies on,
        rather than a hand-rolled stand-in for it.
        """
        result = self.sync_client.update_progress(
            self.book,
            UpdateProgressRequest(locator_result=LocatorResult(percentage=percentage)),
        )
        if result.success and result.updated_state:
            self.database_service.save_state(State(
                abs_id=ABS_ID,
                client_name=HARDCOVER_CLIENT_NAME,
                last_updated=1_787_000_000.0,
                percentage=result.updated_state.get('pct'),
                **state_metadata_kwargs(result.updated_state),
            ))
        return result

    def _allow_new_read_flags(self):
        return [
            call.kwargs.get('allow_new_read')
            for call in self.mock_hardcover_client.update_progress.call_args_list
        ]

    def _stored_candidate(self):
        return self.sync_client._read_reread_candidate(self.book)

    # ------------------------------------------------------------------ tests

    def test_stale_low_position_never_creates_a_read_however_often_it_repeats(self):
        """The reported failure: a stale 4% must never become a Hardcover read."""
        for _ in range(3):
            self._cycle(0.04)

        self.assertEqual(self._allow_new_read_flags(), [False, False, False])

    def test_position_advancing_past_the_margin_confirms_the_reread(self):
        self._cycle(0.04)
        self._cycle(0.09)

        self.assertEqual(self._allow_new_read_flags(), [False, True])

    def test_advance_within_the_margin_does_not_confirm_the_reread(self):
        self._cycle(0.04)
        self._cycle(0.05)

        self.assertEqual(self._allow_new_read_flags(), [False, False])

    def test_candidate_clears_when_the_position_returns_to_completion(self):
        self._cycle(0.04)
        self.assertIsNotNone(self._stored_candidate()[0])

        # The bridge corrects the stale reader back to the finished position.
        self._cycle(1.0)
        self.assertEqual(self._stored_candidate(), (None, None))

        # So the next low reading is a first sighting again, not a confirmation.
        self._cycle(0.04)
        self.assertEqual(self._allow_new_read_flags(), [False, False, False])

    def test_preserving_a_completed_read_leaves_the_state_percentage_untouched(self):
        """The state row must not claim a position that never reached Hardcover."""
        self.database_service.save_state(State(
            abs_id=ABS_ID,
            client_name=HARDCOVER_CLIENT_NAME,
            last_updated=1_786_000_000.0,
            percentage=1.0,
        ))

        result = self._cycle(0.04)

        self.assertTrue(result.success)
        self.assertEqual(result.updated_state['pct'], 1.0)
        self.assertNotIn('pages', result.updated_state)
        self.assertEqual(result.updated_state[REREAD_CANDIDATE_PCT_KEY], 0.04)
        self.assertIn(REREAD_CANDIDATE_FIRST_SEEN_KEY, result.updated_state)

    def test_audiobook_path_uses_the_same_two_cycle_confirmation(self):
        self._save_details(pages=-1, audio_seconds=36000)

        self._cycle(0.04)
        self._cycle(0.09)

        self.assertEqual(self._allow_new_read_flags(), [False, True])

    def test_an_open_read_is_updated_normally_and_carries_no_candidate(self):
        self.mock_hardcover_client.get_latest_read.return_value = {
            'id': 'open-read-1',
            'started_at': '2026-08-01',
            'finished_at': None,
        }

        result = self._cycle(0.25)

        self.assertEqual(self._allow_new_read_flags(), [False])
        self.assertEqual(result.updated_state['pct'], 0.25)
        self.assertEqual(result.updated_state['pages'], 50)
        self.assertNotIn(REREAD_CANDIDATE_PCT_KEY, result.updated_state)
        self.assertEqual(self._stored_candidate(), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
