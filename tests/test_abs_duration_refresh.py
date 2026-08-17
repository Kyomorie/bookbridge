"""Tests for refreshing a stale Book.duration from ABS.

Book.duration is stamped at match time and divides every ABS seconds->percentage
conversion, so a re-encoded or re-chaptered audiobook silently skews every ABS
position forever. These pin the correction and, just as importantly, the guards
that stop a bad reading from zeroing the divisor.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.sync_clients.abs_sync_client import ABSSyncClient


def _book(duration=1000.0):
    return SimpleNamespace(abs_id="abs-1", abs_title="Test Book", duration=duration)


class TestCorrectedDuration(unittest.TestCase):
    def test_material_change_is_adopted(self):
        self.assertEqual(ABSSyncClient._corrected_duration(_book(1000.0), 2000.0), 2000.0)

    def test_small_drift_is_ignored_as_noise(self):
        # 0.2% differences are metadata rounding, not a re-encode.
        self.assertIsNone(ABSSyncClient._corrected_duration(_book(1000.0), 1002.0))

    def test_missing_or_zero_never_zeroes_the_divisor(self):
        for bad in (None, 0, 0.0, -5, "", "abc", [], {}):
            self.assertIsNone(
                ABSSyncClient._corrected_duration(_book(1000.0), bad),
                f"{bad!r} must not be adopted",
            )

    def test_unset_duration_is_populated(self):
        self.assertEqual(ABSSyncClient._corrected_duration(_book(0), 1500.0), 1500.0)
        self.assertEqual(ABSSyncClient._corrected_duration(_book(None), 1500.0), 1500.0)

    def test_numeric_string_is_accepted(self):
        self.assertEqual(ABSSyncClient._corrected_duration(_book(1000.0), "2000"), 2000.0)


class TestServiceStateAppliesCorrection(unittest.TestCase):
    def setUp(self):
        self.abs_client = MagicMock()
        self.abs_client.is_configured.return_value = True
        self.client = ABSSyncClient(
            abs_client=self.abs_client,
            transcriber=MagicMock(),
            ebook_parser=MagicMock(),
            alignment_service=None,
        )

    def test_percentage_uses_the_corrected_duration_this_cycle(self):
        book = _book(1000.0)
        # Book says 1000s, ABS says 2000s; 500s in is 25%, not 50%.
        self.abs_client.get_progress.return_value = {
            "currentTime": 500.0,
            "duration": 2000.0,
            "lastUpdate": None,
            "isFinished": False,
        }

        state = self.client.get_service_state(book, prev_state=None)

        self.assertAlmostEqual(state.current["pct"], 0.25)
        self.assertEqual(state.current["service_duration"], 2000.0)
        self.assertEqual(book.duration, 1000.0, "book.duration must not be mutated; correction is local to this cycle")

    def test_no_correction_key_when_duration_agrees(self):
        book = _book(1000.0)
        self.abs_client.get_progress.return_value = {
            "currentTime": 500.0,
            "duration": 1000.0,
            "lastUpdate": None,
            "isFinished": False,
        }

        state = self.client.get_service_state(book, prev_state=None)

        self.assertNotIn("service_duration", state.current)
        self.assertAlmostEqual(state.current["pct"], 0.5)
        self.assertEqual(book.duration, 1000.0)

    def test_missing_duration_leaves_stored_value_intact(self):
        book = _book(1000.0)
        self.abs_client.get_progress.return_value = {
            "currentTime": 500.0,
            "lastUpdate": None,
            "isFinished": False,
        }

        state = self.client.get_service_state(book, prev_state=None)

        self.assertNotIn("service_duration", state.current)
        self.assertEqual(book.duration, 1000.0)
        self.assertAlmostEqual(state.current["pct"], 0.5)

    def test_bulk_context_path_also_corrects(self):
        book = _book(1000.0)
        bulk = {"abs-1": {"currentTime": 500.0, "duration": 2000.0, "isFinished": False}}

        state = self.client.get_service_state(book, prev_state=None, bulk_context=bulk)

        self.assertEqual(state.current["service_duration"], 2000.0)
        self.assertAlmostEqual(state.current["pct"], 0.25)
        self.assertEqual(book.duration, 1000.0, "book.duration must not be mutated in bulk context path")

    def test_duration_correction_does_not_mutate_shared_book(self):
        """
        Regression test: when ABS reports a materially different duration,
        get_service_state must NOT mutate the passed book object's duration attribute,
        but must still report the correction via current['service_duration'] and
        compute the percentage using the CORRECTED duration.
        """
        book = _book(1000.0)  # Stored duration
        self.abs_client.get_progress.return_value = {
            "currentTime": 500.0,  # 500s into a 2000s book = 25%
            "duration": 2000.0,    # ABS reports 2000s (material difference)
            "lastUpdate": None,
            "isFinished": False,
        }

        state = self.client.get_service_state(book, prev_state=None)

        # 1. book.duration is NOT mutated (still equals original stored value)
        self.assertEqual(book.duration, 1000.0, "book.duration must not be mutated by get_service_state")

        # 2. Correction is still reported via current['service_duration']
        self.assertEqual(state.current["service_duration"], 2000.0, "service_duration must report the corrected value")

        # 3. Percentage is computed using the CORRECTED duration (500/2000 = 0.25), not the stale one (500/1000 = 0.5)
        self.assertAlmostEqual(state.current["pct"], 0.25, places=4, msg="Percentage must use corrected duration, not stale book.duration")


if __name__ == "__main__":
    unittest.main()
