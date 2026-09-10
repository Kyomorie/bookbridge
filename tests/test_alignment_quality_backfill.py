"""Issue #426 phase 4: the alignment-health query is widened by score, and
unscored legacy maps are backfilled lazily.

The load-bearing test in this file is
`test_low_score_lexical_map_is_included_and_high_score_lexical_map_is_excluded`:
before this phase, `get_books_needing_llm_realign()` looked only at
`align_method` (NULL / linear / storyteller_linear), so a badly broken
'lexical' map (Immortal Mana, Starfish, Bestial, Four Past Midnight — all
measured live, issue #426) was reported healthy.
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from src.db.database_service import DatabaseService
from src.db.models import Book, BookAlignment
from src.services.map_quality import ALIGNMENT_QUALITY_REALIGN_THRESHOLD, score_map


def _dense_map(length: int, step: int) -> List[Dict]:
    """An evenly-paced, densely-anchored synthetic map: scores near 1.0."""
    return [{"char": c, "ts": round(c * 0.01, 3)} for c in range(0, length + 1, step)]


def _broken_map(length: int) -> List[Dict]:
    """A sparse, two-point map: scores far below the realign threshold."""
    return [{"char": 0, "ts": 0.0}, {"char": length, "ts": float(length)}]


class _AlignmentQualityTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = DatabaseService(str(Path(self.temp_dir) / "quality_backfill.db"))

    def tearDown(self):
        self.db.db_manager.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _save_scored(self, abs_id: str, alignment_map, align_method: str,
                      total_chars: int, quality=None) -> None:
        """Write a BookAlignment row directly (mirrors
        AlignmentService._save_alignment's upsert shape) with an optional
        pre-computed quality score."""
        with self.db.get_session() as session:
            session.add(BookAlignment(
                abs_id=abs_id,
                alignment_map_json=json.dumps(alignment_map),
                align_method=align_method,
                total_chars=total_chars,
                quality_score=quality.score if quality is not None else None,
                quality_detail=None,
            ))


class TestGetBooksNeedingLlmRealignQualityThreshold(_AlignmentQualityTestBase):

    def test_low_score_lexical_map_is_included_and_high_score_lexical_map_is_excluded(self):
        good = _dense_map(1000, 10)
        good_quality = score_map(good)
        self.assertGreaterEqual(good_quality.score, ALIGNMENT_QUALITY_REALIGN_THRESHOLD)

        bad = _broken_map(1000)
        bad_quality = score_map(bad)
        self.assertLess(bad_quality.score, ALIGNMENT_QUALITY_REALIGN_THRESHOLD)

        self._save_scored("good-book", good, "lexical", 1000, quality=good_quality)
        self._save_scored("bad-book", bad, "lexical", 1000, quality=bad_quality)

        targets = self.db.get_books_needing_llm_realign()

        self.assertIn("bad-book", targets)
        self.assertNotIn("good-book", targets)

    def test_unscored_lexical_map_is_not_flagged_by_score_alone(self):
        """A NULL quality_score (map stored before scoring existed) must not, on
        its own, mark a 'lexical' map for realign -- only a recorded low score
        does. `align_method` NULL/linear/storyteller_linear still flags it."""
        unscored = _dense_map(1000, 10)
        self._save_scored("unscored-lexical", unscored, "lexical", 1000, quality=None)

        targets = self.db.get_books_needing_llm_realign()

        self.assertNotIn("unscored-lexical", targets)

    def test_null_align_method_still_flagged_regardless_of_score(self):
        good = _dense_map(1000, 10)
        good_quality = score_map(good)
        self._save_scored("pre-llm", good, None, 1000, quality=good_quality)

        self.assertIn("pre-llm", self.db.get_books_needing_llm_realign())

    def test_linear_method_still_flagged_regardless_of_score(self):
        good = _dense_map(1000, 10)
        good_quality = score_map(good)
        self._save_scored("linear-book", good, "linear", 1000, quality=good_quality)

        self.assertIn("linear-book", self.db.get_books_needing_llm_realign())


class TestGetAlignmentProvenanceQualityScore(_AlignmentQualityTestBase):

    def test_low_score_lexical_row_is_reported_with_its_score(self):
        bad = _broken_map(1000)
        bad_quality = score_map(bad)
        self.db.save_book(Book(abs_id="bad-book", abs_title="Bad Book"))
        self._save_scored("bad-book", bad, "lexical", 1000, quality=bad_quality)

        provenance = self.db.get_alignment_provenance()

        row = next(b for b in provenance["books"] if b["abs_id"] == "bad-book")
        self.assertEqual(row["quality_score"], bad_quality.score)
        self.assertTrue(row["needs_realign"])

    def test_high_score_lexical_row_is_not_reported(self):
        good = _dense_map(1000, 10)
        good_quality = score_map(good)
        self.db.save_book(Book(abs_id="good-book", abs_title="Good Book"))
        self._save_scored("good-book", good, "lexical", 1000, quality=good_quality)

        provenance = self.db.get_alignment_provenance()

        self.assertNotIn("good-book", {b["abs_id"] for b in provenance["books"]})

    def test_never_selects_the_alignment_map_blob(self):
        """Guards the existing contract this docstring promises: the report must
        never pull the (potentially 10-15MB) alignment_map_json column."""
        bad = _broken_map(1000)
        self._save_scored("bad-book", bad, "lexical", 1000, quality=score_map(bad))

        provenance = self.db.get_alignment_provenance()

        row = next(b for b in provenance["books"] if b["abs_id"] == "bad-book")
        self.assertNotIn("alignment_map_json", row)


class TestBackfillAlignmentQuality(_AlignmentQualityTestBase):

    def test_scores_only_unscored_maps(self):
        already_scored = _dense_map(1000, 10)
        already_quality = score_map(already_scored)
        self._save_scored("scored-book", already_scored, "lexical", 1000, quality=already_quality)

        unscored = _broken_map(1000)
        self._save_scored("unscored-book", unscored, "lexical", 1000, quality=None)

        scored_count = self.db.backfill_alignment_quality()

        self.assertEqual(scored_count, 1)
        with self.db.get_session() as session:
            row = session.query(BookAlignment).filter_by(abs_id="unscored-book").first()
            self.assertIsNotNone(row.quality_score)
            self.assertIsNotNone(row.quality_detail)
            unchanged = session.query(BookAlignment).filter_by(abs_id="scored-book").first()
            self.assertEqual(unchanged.quality_score, already_quality.score)

    def test_respects_limit(self):
        for i in range(5):
            self._save_scored(f"book-{i}", _broken_map(100), "lexical", 100, quality=None)

        scored_count = self.db.backfill_alignment_quality(limit=2)

        self.assertEqual(scored_count, 2)
        with self.db.get_session() as session:
            remaining_unscored = (
                session.query(BookAlignment)
                .filter(BookAlignment.quality_score.is_(None))
                .count()
            )
            self.assertEqual(remaining_unscored, 3)

    def test_is_idempotent_on_a_second_call(self):
        for i in range(3):
            self._save_scored(f"book-{i}", _broken_map(100), "lexical", 100, quality=None)

        first_pass = self.db.backfill_alignment_quality(limit=25)
        second_pass = self.db.backfill_alignment_quality(limit=25)

        self.assertEqual(first_pass, 3)
        self.assertEqual(second_pass, 0)


if __name__ == "__main__":
    unittest.main()
