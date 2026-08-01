import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from utils.transcription_providers import (
    LocalWhisperProvider,
    DeepgramProvider,
    WhisperCppServerProvider,
    get_transcription_provider,
)

class TestLocalWhisperProvider(unittest.TestCase):
    
    @patch.dict(os.environ, {}, clear=True)
    def test_default_init(self):
        """Test default initialization with no env vars."""
        provider = LocalWhisperProvider()
        self.assertEqual(provider.model_size, "base")
        self.assertEqual(provider.whisper_device, "auto")
        self.assertEqual(provider.whisper_compute_type, "auto")
        self.assertIn("LocalWhisper", provider.get_name())

    @staticmethod
    def _fake_cuda_env(libs_bundled: bool, device_count: int = 0):
        """Simulate CUDA libs and visible GPU (both required for CUDA to work in the container)"""
        mock_ct2 = MagicMock()
        mock_ct2.get_cuda_device_count.return_value = device_count
        return (
            patch("utils.transcription_providers.importlib.util.find_spec",
                  return_value=MagicMock() if libs_bundled else None),
            patch.dict(sys.modules, {'ctranslate2': mock_ct2}),
        )

    @patch("utils.transcription_providers.logger")
    def test_get_device_config_auto_gpu(self, mock_logger):
        """CUDA image on a host with a GPU: auto picks cuda."""
        provider = LocalWhisperProvider()
        find_spec, ct2 = self._fake_cuda_env(libs_bundled=True, device_count=1)

        with find_spec, ct2:
            device, compute_type = provider._get_device_config()

        self.assertEqual(device, "cuda")
        self.assertEqual(compute_type, "float16")  # Default for GPU in auto mode

    @patch("utils.transcription_providers.logger")
    def test_get_device_config_auto_cpu_no_libs(self, mock_logger):
        """CPU image on a GPU host: no bundled CUDA libs, so stay on CPU."""
        provider = LocalWhisperProvider()
        find_spec, ct2 = self._fake_cuda_env(libs_bundled=False, device_count=1)

        with find_spec, ct2:
            device, compute_type = provider._get_device_config()

        self.assertEqual(device, "cpu")
        self.assertEqual(compute_type, "int8")  # Default for CPU in auto mode

    @patch("utils.transcription_providers.logger")
    def test_get_device_config_auto_cpu_no_gpu(self, mock_logger):
        """CUDA image with no GPU passed through to the container: stay on CPU."""
        provider = LocalWhisperProvider()
        find_spec, ct2 = self._fake_cuda_env(libs_bundled=True, device_count=0)

        with find_spec, ct2:
            device, compute_type = provider._get_device_config()

        self.assertEqual(device, "cpu")
        self.assertEqual(compute_type, "int8")


    @patch("utils.transcription_providers.logger")
    def test_explicit_config(self, mock_logger):
        """Test that explicit environment variables override auto detection."""
        with patch.dict(os.environ, {
            "WHISPER_DEVICE": "cpu", 
            "WHISPER_COMPUTE_TYPE": "int8"
        }):
            provider = LocalWhisperProvider()
            device, compute_type = provider._get_device_config()
            
            self.assertEqual(device, "cpu")
            self.assertEqual(compute_type, "int8")

    @patch("faster_whisper.WhisperModel")
    @patch("utils.transcription_providers.logger")
    @patch.dict(os.environ, {"WHISPER_MODEL": "base", "WHISPER_DEVICE": "auto"}, clear=True)
    def test_model_initialization_gpu(self, mock_logger, mock_whisper_model):
        """Test that WhisperModel is initialized with correct GPU params."""
        provider = LocalWhisperProvider()
        
        # Force GPU config via mock
        with patch.object(provider, '_get_device_config', return_value=('cuda', 'float16')):
            provider._get_model()
            expected_download_root = str(Path(os.environ.get("DATA_DIR", "/data")) / "models")
            
            mock_whisper_model.assert_called_once_with(
                'base', 
                download_root=expected_download_root,
                device='cuda', 
                compute_type='float16'
            )

class TestDeepgramProvider(unittest.TestCase):
    
    def test_init_without_key(self):
        """Test initialization works but transcribe fails without key."""
        with patch.dict(os.environ, {}, clear=True):
            provider = DeepgramProvider()
            self.assertEqual(provider.api_key, "")
            
            with self.assertRaises(ValueError):
                provider.transcribe(Path("dummy.wav"))

    def test_init_with_key(self):
        """Test initialization with key."""
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test_key", "DEEPGRAM_MODEL": "nova-3"}):
            provider = DeepgramProvider()
            self.assertEqual(provider.api_key, "test_key")
            self.assertEqual(provider.model, "nova-3")
            self.assertIn("nova-3", provider.get_name())

    def test_transcribe(self):
        """Test transcribe calls Deepgram API correctly with new SDK."""
        # Create a mock for the deepgram module
        mock_deepgram = MagicMock()
        mock_client_cls = MagicMock()
        mock_deepgram.DeepgramClient = mock_client_cls
        
        # Patch sys.modules to include deepgram
        with patch.dict(sys.modules, {'deepgram': mock_deepgram}):
            with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test_key"}):
                provider = DeepgramProvider()
                
                # Mock the client chain: client.listen.v1.media.transcribe_file
                mock_client = mock_client_cls.return_value
                mock_transcribe = mock_client.listen.v1.media.transcribe_file
                
                # Mock response structure
                mock_response = MagicMock()
                # Setup utterances structure
                mock_utterance = MagicMock()
                mock_utterance.start = 0.5
                mock_utterance.end = 2.5
                mock_utterance.transcript = "Hello world"
                
                mock_response.results.utterances = [mock_utterance]
                mock_transcribe.return_value = mock_response
                
                # Create a dummy file to read
                with patch("builtins.open", new_callable=unittest.mock.mock_open, read_data=b"audio_data"):
                    segments = provider.transcribe(Path("test.mp3"))
                
                # Verify client init
                mock_client_cls.assert_called_once_with(api_key="test_key")
                
                # Verify transcribe call args - ensure NO timeout and correct model
                mock_transcribe.assert_called_once()
                _, kwargs = mock_transcribe.call_args
                self.assertEqual(kwargs['model'], 'nova-2')
                self.assertEqual(kwargs['smart_format'], True)
                self.assertNotIn('timeout', kwargs) # IMPORTANT: timeout should NOT be passed
                
                # Verify result parsing
                self.assertEqual(len(segments), 1)
                self.assertEqual(segments[0]['text'], "Hello world")
                self.assertEqual(segments[0]['start'], 0.5)
                self.assertEqual(segments[0]['end'], 2.5)

class TestWhisperCppServerProvider(unittest.TestCase):

    def test_init_without_url_raises(self):
        """Missing WHISPER_CPP_URL must fail loudly."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                WhisperCppServerProvider()

    def test_defaults(self):
        """Raw upload is off by default and does not advertise supports_raw_audio."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()
            self.assertFalse(provider.send_original)
            self.assertFalse(provider.supports_raw_audio)
            self.assertEqual(provider.timeout, 600)

    def test_send_original_enables_raw_audio(self):
        """WHISPER_CPP_SEND_ORIGINAL=true makes the pipeline skip WAV normalization."""
        with patch.dict(os.environ, {
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
            "WHISPER_CPP_SEND_ORIGINAL": "true",
        }, clear=True):
            provider = WhisperCppServerProvider()
            self.assertTrue(provider.send_original)
            self.assertTrue(provider.supports_raw_audio)

    def test_transcribe_local_file_parses_segments(self):
        """Local file upload posts verbose_json and parses segment timestamps."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "segments": [{"start": 1.0, "end": 2.5, "text": " hello world "}]
        }
        with patch("requests.post", return_value=mock_resp) as mock_post, \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(segments, [{"start": 1.0, "end": 2.5, "text": "hello world"}])
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"]["response_format"], "verbose_json")
        self.assertEqual(kwargs["timeout"], 600)

    def test_transcribe_url_source_downloads_then_uploads(self):
        """A stream URL source is buffered to a temp file and uploaded."""
        with patch.dict(os.environ, {
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
            "WHISPER_CPP_SEND_ORIGINAL": "true",
        }, clear=True):
            provider = WhisperCppServerProvider()

        mock_get_resp = MagicMock()
        mock_get_resp.__enter__ = MagicMock(return_value=mock_get_resp)
        mock_get_resp.__exit__ = MagicMock(return_value=False)
        mock_get_resp.iter_content.return_value = [b"audio-bytes"]

        mock_post_resp = MagicMock()
        mock_post_resp.json.return_value = {
            "segments": [{"start": 0.0, "end": 3.0, "text": "streamed"}]
        }

        with patch("requests.get", return_value=mock_get_resp) as mock_get, \
             patch("requests.post", return_value=mock_post_resp) as mock_post:
            segments = provider.transcribe("http://abs/stream/part.m4b?token=abc")

        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args[0][0], "http://abs/stream/part.m4b?token=abc")
        mock_post.assert_called_once()
        # Upload uses the source filename (query string stripped)
        upload_name = mock_post.call_args[1]["files"]["file"][0]
        self.assertEqual(upload_name, "part.m4b")
        self.assertEqual(segments, [{"start": 0.0, "end": 3.0, "text": "streamed"}])

    def test_text_only_response_warns_and_degrades(self):
        """Servers ignoring verbose_json still return a usable (untimed) segment."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "plain transcript"}
        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(segments, [{"start": 0.0, "end": 0.0, "text": "plain transcript"}])

    def test_chunked_wav_upload_offsets_timestamps(self):
        """WHISPER_CPP_CHUNK_MINUTES splits WAVs and offsets returned timestamps."""
        import io
        import tempfile
        import wave

        with patch.dict(os.environ, {
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
            "WHISPER_CPP_CHUNK_MINUTES": "1",
        }, clear=True):
            provider = WhisperCppServerProvider()

        # 90 seconds of silence at 16kHz mono -> two chunks (60s + 30s)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            with wave.open(tmp, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 16000 * 90)
            wav_path = Path(tmp.name)

        durations = []

        def fake_post(url, files=None, data=None, timeout=None):
            buf = files["file"][1]
            with wave.open(io.BytesIO(buf.read()), "rb") as wf:
                dur = wf.getnframes() / wf.getframerate()
            durations.append(dur)
            resp = MagicMock()
            resp.json.return_value = {
                "segments": [{"start": 0.0, "end": dur, "text": f"part {len(durations)}"}]
            }
            return resp

        try:
            with patch("requests.post", side_effect=fake_post):
                segments = provider.transcribe(wav_path)
        finally:
            wav_path.unlink()

        self.assertEqual(durations, [60.0, 30.0])
        self.assertEqual(segments, [
            {"start": 0.0, "end": 60.0, "text": "part 1"},
            {"start": 60.0, "end": 90.0, "text": "part 2"},
        ])

    def test_factory_returns_whispercpp(self):
        """Factory selects WhisperCppServerProvider when configured."""
        with patch.dict(os.environ, {
            "TRANSCRIPTION_PROVIDER": "whispercpp",
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
        }, clear=True):
            provider = get_transcription_provider()
            self.assertIsInstance(provider, WhisperCppServerProvider)


if __name__ == '__main__':
    unittest.main()
