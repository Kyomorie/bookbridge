"""Regression tests for issue #362 — long audiobooks silently truncated.

The reporter's 28h audiobook (101,665.44s) was transcribed as if it were 21.3h
(76,726.9s). Nothing noticed: the transcript aligned cleanly against the ebook, so
every position derived from that map was wrong by the coverage shortfall while the
job reported success.

Three guards are covered here:
  * a streamed download that stops short of its Content-Length is rejected,
  * audio shorter than the runtime the library reports is rejected BEFORE Whisper
    runs (a bad download must not cost hours),
  * a completed `_progress.json` holding a short transcript is discarded rather
    than replayed forever — `_prune_audio_cache` keeps that file deliberately, so
    without this an already-affected book never heals.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.utils.transcriber import AudioTranscriber

# The reporter's actual numbers (issue #362).
REPORTED_AUDIO_DURATION = 101665.44
REPORTED_TRANSCRIPT_EXTENT = 76726.928


def _segments_to(end_ts):
    return [{"start": 0.0, "end": end_ts, "text": "hello there"}]


class TranscriptCoverageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.transcriber = AudioTranscriber(self.tmp, MagicMock(), MagicMock())
        self._saved_env = os.environ.get("TRANSCRIPT_MIN_COVERAGE")
        os.environ.pop("TRANSCRIPT_MIN_COVERAGE", None)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("TRANSCRIPT_MIN_COVERAGE", None)
        else:
            os.environ["TRANSCRIPT_MIN_COVERAGE"] = self._saved_env
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCoverageThresholdSetting(TranscriptCoverageTestCase):
    def test_defaults_to_smil_threshold(self):
        self.assertEqual(self.transcriber.min_coverage, 0.85)

    def test_reads_setting_per_access(self):
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "0.5"
        self.assertEqual(self.transcriber.min_coverage, 0.5)
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "0.99"
        self.assertEqual(self.transcriber.min_coverage, 0.99)

    def test_invalid_value_falls_back_to_default(self):
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "not-a-number"
        self.assertEqual(self.transcriber.min_coverage, 0.85)

    def test_clamped_to_unit_interval(self):
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "5"
        self.assertEqual(self.transcriber.min_coverage, 1.0)
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "-1"
        self.assertEqual(self.transcriber.min_coverage, 0.0)


class TestCoverageCheck(TranscriptCoverageTestCase):
    def test_rejects_the_reported_shortfall(self):
        with self.assertRaises(ValueError) as ctx:
            self.transcriber._check_audio_coverage(
                REPORTED_TRANSCRIPT_EXTENT, REPORTED_AUDIO_DURATION
            )
        message = str(ctx.exception)
        self.assertIn("Coverage too low", message)
        self.assertIn("75.5%", message)
        self.assertIn("101665", message)
        self.assertIn("76727", message)

    def test_accepts_full_coverage(self):
        self.transcriber._check_audio_coverage(
            REPORTED_AUDIO_DURATION, REPORTED_AUDIO_DURATION
        )

    def test_accepts_small_shortfall_within_threshold(self):
        self.transcriber._check_audio_coverage(
            REPORTED_AUDIO_DURATION * 0.9, REPORTED_AUDIO_DURATION
        )

    def test_zero_threshold_disables_the_guard(self):
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "0"
        self.transcriber._check_audio_coverage(
            REPORTED_TRANSCRIPT_EXTENT, REPORTED_AUDIO_DURATION
        )

    def test_unknown_expected_duration_is_not_an_error(self):
        """Audio-only or legacy mappings may have no recorded runtime."""
        self.transcriber._check_audio_coverage(REPORTED_TRANSCRIPT_EXTENT, None)
        self.transcriber._check_audio_coverage(REPORTED_TRANSCRIPT_EXTENT, 0)

    def test_missing_actual_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.transcriber._check_audio_coverage(0.0, REPORTED_AUDIO_DURATION)

    def test_transcript_extent_reads_last_segment(self):
        self.assertEqual(
            self.transcriber._transcript_extent(_segments_to(REPORTED_TRANSCRIPT_EXTENT)),
            REPORTED_TRANSCRIPT_EXTENT,
        )
        self.assertEqual(self.transcriber._transcript_extent([]), 0.0)
        self.assertEqual(self.transcriber._transcript_extent(None), 0.0)


class TestDownloadIntegrity(TranscriptCoverageTestCase):
    def _response(self, content_length):
        response = MagicMock()
        response.headers = {"Content-Length": str(content_length)} if content_length else {}
        return response

    def test_truncated_download_raises(self):
        target = self.tmp / "part_000.m4b"
        target.write_bytes(b"x" * 500)
        with self.assertRaises(ValueError) as ctx:
            self.transcriber._verify_download_size(self._response(1000), target)
        self.assertIn("Truncated download", str(ctx.exception))

    def test_complete_download_passes(self):
        target = self.tmp / "part_000.m4b"
        target.write_bytes(b"x" * 1000)
        self.transcriber._verify_download_size(self._response(1000), target)

    def test_missing_content_length_is_not_an_error(self):
        target = self.tmp / "part_000.m4b"
        target.write_bytes(b"x" * 10)
        self.transcriber._verify_download_size(self._response(None), target)


class TestProcessAudioGuards(TranscriptCoverageTestCase):
    """Drives the real process_audio() entry point, not just the predicate."""

    def _write_completed_cache(self, abs_id, transcript_end):
        cache_dir = self.tmp / "audio_cache" / abs_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        progress_file = cache_dir / "_progress.json"
        progress_file.write_text(json.dumps({
            "chunks_completed": 29,
            "cumulative_duration": transcript_end,
            "transcript": _segments_to(transcript_end),
            "done": True,
        }))
        return progress_file

    def test_short_cached_transcript_is_discarded_and_not_returned(self):
        """The affected install must heal instead of replaying the bad cache."""
        abs_id = "dungeon-crawler-carl"
        progress_file = self._write_completed_cache(abs_id, REPORTED_TRANSCRIPT_EXTENT)

        with patch("src.utils.transcriber.get_transcription_provider") as provider_factory:
            provider = MagicMock()
            provider.supports_raw_audio = False
            provider_factory.return_value = provider
            with self.assertRaises(Exception):
                self.transcriber.process_audio(
                    abs_id, [], expected_duration=REPORTED_AUDIO_DURATION
                )

        self.assertFalse(
            progress_file.exists(),
            "a below-coverage cached transcript must be unlinked so the retry re-downloads",
        )

    def test_good_cached_transcript_is_still_reused(self):
        abs_id = "healthy-book"
        self._write_completed_cache(abs_id, REPORTED_AUDIO_DURATION)

        result = self.transcriber.process_audio(
            abs_id, [], expected_duration=REPORTED_AUDIO_DURATION
        )
        self.assertEqual(len(result), 1)

    def test_cached_transcript_reused_when_no_expected_duration(self):
        """Unchanged behaviour for callers that cannot supply a runtime."""
        abs_id = "legacy-book"
        self._write_completed_cache(abs_id, REPORTED_TRANSCRIPT_EXTENT)

        result = self.transcriber.process_audio(abs_id, [])
        self.assertEqual(len(result), 1)

    def test_short_audio_rejected_before_any_transcription(self):
        """Fail fast: a truncated download must not cost hours of Whisper."""
        abs_id = "short-audio-book"
        cache_dir = self.tmp / "audio_cache" / abs_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        chunk = cache_dir / "part_000_split_001.wav"
        chunk.write_bytes(b"RIFF")

        self.transcriber.get_audio_duration = MagicMock(return_value=REPORTED_TRANSCRIPT_EXTENT)

        with patch("src.utils.transcriber.get_transcription_provider") as provider_factory:
            provider = MagicMock()
            provider.supports_raw_audio = False
            provider.get_name.return_value = "test"
            provider_factory.return_value = provider

            with self.assertRaises(ValueError) as ctx:
                self.transcriber.process_audio(
                    abs_id,
                    [{"stream_url": "http://example.com/1.m4b", "ext": "m4b"}],
                    expected_duration=REPORTED_AUDIO_DURATION,
                )

            self.assertIn("Coverage too low", str(ctx.exception))
            provider.transcribe.assert_not_called()

        self.assertFalse(
            cache_dir.exists(),
            "the short audio cache must be cleared so the retry re-downloads",
        )


if __name__ == "__main__":
    unittest.main()
