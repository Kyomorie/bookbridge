import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import sys

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from utils import transcriber as transcriber_module
from utils.transcriber import AudioTranscriber


class TestSplitDuration(unittest.TestCase):
    """AUDIO_SPLIT_DURATION_MINUTES is a GUI setting, so it must be read per
    access — the transcriber is a DI singleton that outlives a settings save."""

    def setUp(self):
        self.transcriber = AudioTranscriber(
            Path("/tmp/mock_data"), MagicMock(), MagicMock()
        )
        self._saved = os.environ.pop("AUDIO_SPLIT_DURATION_MINUTES", None)

    def tearDown(self):
        os.environ.pop("AUDIO_SPLIT_DURATION_MINUTES", None)
        if self._saved is not None:
            os.environ["AUDIO_SPLIT_DURATION_MINUTES"] = self._saved

    def test_defaults_to_45_minutes(self):
        self.assertEqual(self.transcriber.split_duration, 45 * 60)

    def test_reflects_setting_changed_after_construction(self):
        os.environ["AUDIO_SPLIT_DURATION_MINUTES"] = "15"
        self.assertEqual(self.transcriber.split_duration, 15 * 60)

    def test_invalid_value_falls_back_to_default(self):
        os.environ["AUDIO_SPLIT_DURATION_MINUTES"] = "not-a-number"
        self.assertEqual(self.transcriber.split_duration, 45 * 60)

    def test_zero_is_clamped_to_one_minute(self):
        """A 0-second chunk length would split forever."""
        os.environ["AUDIO_SPLIT_DURATION_MINUTES"] = "0"
        self.assertEqual(self.transcriber.split_duration, 60)


class TestRawSourceFailureLogging(unittest.TestCase):
    """Raw-audio providers receive stream-URL strings, not Paths. A transcription
    failure must surface the provider's error, not an AttributeError from the
    logging in the except branch."""

    def setUp(self):
        self.data_dir = Path("/tmp/mock_data_raw")
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.transcriber = AudioTranscriber(self.data_dir, MagicMock(), MagicMock())
        self.transcriber.get_audio_duration = MagicMock(return_value=100.0)

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_failure_on_stream_url_reports_provider_error(self):
        abs_id = "raw-book"
        audio_urls = [{'stream_url': 'http://example.com/book.m4b', 'ext': 'm4b'}]

        with patch.object(transcriber_module, "get_transcription_provider") as get_provider:
            provider = MagicMock()
            provider.supports_raw_audio = True
            provider.get_name.return_value = "raw-provider"
            provider.transcribe.side_effect = RuntimeError("server returned 500")
            get_provider.return_value = provider

            with self.assertRaises(RuntimeError) as ctx:
                self.transcriber.process_audio(abs_id, audio_urls)

        self.assertIn("server returned 500", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
