"""Finding 2 (P1) regression coverage: a normal sync cycle must never falsely
complete an in-flight read-along generation job.

Mechanism confirmed by reading the code: `SyncManager._promote_alignment_backed_book`
(called from the main per-book sync loop on every active book, every cycle --
not just on crash recovery) looked up "the latest `Job` row for this abs_id"
with no notion of what kind of job that was, and if it looked unfinished
(progress < 1.0, a retry_count, or a last_error) stamped it
progress=1.0/last_error=None. Phase 6a's read-along EPUB generation
(`web_server.generate_readalong_epub`) creates a `Job` row in the very same
table, so an ordinary sync cycle that ran while generation was still in
flight (or had genuinely failed) would silently mark that job "done" without
its worker ever finishing -- and erase a recorded failure.

The fix adds `Job.kind` (migration `3f6c8a1d9e42_add_job_kind.py`) and scopes
`_promote_alignment_backed_book`'s lookup/update to `kind=JOB_KIND_ALIGNMENT`
via `DatabaseService.get_latest_job`/`update_latest_job`'s new optional
`kind` filter. Read-along jobs are `kind=JOB_KIND_READALONG` throughout
(`generate_readalong_epub`, `_readalong_epub_worker`, `readalong_epub_status`)
so they are never the "latest alignment job" regardless of timestamp.

Uses a real `DatabaseService` (temp SQLite, full Alembic migration chain)
rather than a MagicMock so the SQL-level kind filtering in
`get_latest_job`/`update_latest_job` is exercised for real, not merely
asserted via call arguments a stub could ignore.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from src.db.database_service import DatabaseService
from src.db.models import Book, Job, JOB_KIND_ALIGNMENT, JOB_KIND_READALONG
from src.sync_manager import SyncManager


class _StubAlignmentService:
    """Stands in for `AlignmentService`: `_get_alignment` is the only method
    `_promote_alignment_backed_book` calls on it."""

    def __init__(self, has_alignment: bool = True):
        self.has_alignment = has_alignment

    def _get_alignment(self, abs_id: str):
        return {"ok": True} if self.has_alignment else None


class TestJobKindIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "job_kind.db")
        self.db = DatabaseService(self.db_path)
        self.manager = SyncManager(
            database_service=self.db,
            alignment_service=_StubAlignmentService(has_alignment=True),
            sync_clients={},
            epub_cache_dir=Path(self.temp_dir) / "epub_cache",
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / "books",
        )
        self.abs_id = "book-overlap"
        self.db.save_book(Book(abs_id=self.abs_id, abs_title="Overlap Book", status="active"))

    def tearDown(self) -> None:
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_normal_sync_does_not_complete_in_flight_readalong_job(self) -> None:
        """A read-along job mid-generation (progress 0.0, no error yet) must
        survive a normal sync cycle untouched."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG)
        )

        promoted = self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))

        self.assertTrue(promoted)
        readalong_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_READALONG)
        self.assertEqual(readalong_job.progress, 0.0)
        self.assertIsNone(readalong_job.last_error)

    def test_normal_sync_still_completes_the_alignment_repair_job(self) -> None:
        """The actual job this method exists to repair -- alignment
        build/retry tracking -- must still get completed."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=2, progress=0.4,
                last_error="transient failure", kind=JOB_KIND_ALIGNMENT)
        )

        promoted = self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))

        self.assertTrue(promoted)
        alignment_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_ALIGNMENT)
        self.assertEqual(alignment_job.progress, 1.0)
        self.assertEqual(alignment_job.retry_count, 0)
        self.assertIsNone(alignment_job.last_error)

    def test_readalong_failure_survives_a_subsequent_sync(self) -> None:
        """A generation failure recorded on the read-along job must still be
        readable after another normal sync cycle runs on the same book."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.0,
                last_error="BookOrbit audio sync is not available", kind=JOB_KIND_READALONG)
        )

        # Two subsequent normal sync cycles touching the same book.
        self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))
        self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))

        readalong_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_READALONG)
        self.assertEqual(readalong_job.last_error, "BookOrbit audio sync is not available")
        self.assertEqual(readalong_job.progress, 0.0)

    def test_overlapping_jobs_resolve_to_the_right_row_each_time(self) -> None:
        """An alignment-repair job and a read-along job on the same book, the
        read-along one strictly newer by timestamp (as it would be in
        practice -- created after the alignment work that made generation
        eligible). The newer row must never be mistaken for "the latest job"
        just because it has the later `last_attempt`."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=1, progress=0.5,
                last_error="retry pending", kind=JOB_KIND_ALIGNMENT)
        )
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=2000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG)
        )

        promoted = self.manager._promote_alignment_backed_book(self.db.get_book(self.abs_id))

        self.assertTrue(promoted)
        alignment_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_ALIGNMENT)
        readalong_job = self.db.get_latest_job(self.abs_id, kind=JOB_KIND_READALONG)
        # The older, correct-kind job was completed...
        self.assertEqual(alignment_job.progress, 1.0)
        self.assertIsNone(alignment_job.last_error)
        # ...while the newer read-along job -- literally "the latest job" for
        # this abs_id by timestamp -- was never touched.
        self.assertEqual(readalong_job.progress, 0.0)
        self.assertIsNone(readalong_job.last_error)

    def test_get_and_update_latest_job_default_to_unfiltered_for_compatibility(self) -> None:
        """Existing callers that never pass `kind` (Forge & Match's
        `_record_forge_match_job`/`_update_forge_match_job`, the dashboard's
        processing/forging job-progress lookup) must keep seeing "the newest
        job regardless of kind" -- the historical behavior."""
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=1000.0, retry_count=0, progress=0.2,
                last_error=None, kind=JOB_KIND_ALIGNMENT)
        )
        self.db.save_job(
            Job(abs_id=self.abs_id, last_attempt=2000.0, retry_count=0, progress=0.0,
                last_error=None, kind=JOB_KIND_READALONG)
        )

        latest = self.db.get_latest_job(self.abs_id)
        self.assertEqual(latest.kind, JOB_KIND_READALONG)

        updated = self.db.update_latest_job(self.abs_id, last_error="unfiltered update")
        self.assertEqual(updated.kind, JOB_KIND_READALONG)
        self.assertEqual(updated.last_error, "unfiltered update")


if __name__ == "__main__":
    unittest.main()
