# Transcription Providers
"""
Abstract interface and implementations for transcription services.
Supports local Whisper and cloud providers like Deepgram.
"""

import importlib.util
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TranscriptionProvider(ABC):
    """Abstract base class for transcription providers."""
    
    @abstractmethod
    def transcribe(self, audio_path: Path, progress_callback=None) -> list[dict]:
        """
        Transcribe an audio file and return segments.
        
        Args:
            audio_path: Path to the audio file
            progress_callback: Optional callback for progress updates (0.0 to 1.0)
        
        Returns:
            List of dicts with 'start', 'end', 'text' keys
        """
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Return the provider name for logging."""
        pass


class LocalWhisperProvider(TranscriptionProvider):
    """Local Whisper transcription using faster-whisper."""
    
    def __init__(self):
        self.model_size = os.environ.get("WHISPER_MODEL", "base")
        self.whisper_device = os.environ.get("WHISPER_DEVICE", "auto").lower()
        self.whisper_compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "auto").lower()
        self._model = None
    
    def get_name(self) -> str:
        return f"LocalWhisper ({self.model_size})"
    
    def _detect_cuda(self) -> tuple[bool, str]:
        """
        Check whether CTranslate2 can actually run on CUDA here.
        This is dependent on the image that is used (only the image with the -cuda suffix has CUDA libraries)
        and whether the GPU is visible in the container (via deploy block in the compose file)
        """
        try:
            cudnn_spec = importlib.util.find_spec("nvidia.cudnn")
        except (ImportError, ValueError):
            # find_spec only returns None when the parent "nvidia" package exists;
            # on the non-CUDA image no nvidia-* wheels are installed at all, so it
            # raises ModuleNotFoundError instead (#355).
            cudnn_spec = None
        if cudnn_spec is None:
            return False, "CUDA libraries not bundled (use the -cuda image tag)"

        try:
            import ctranslate2 # transitive dependency by faster-whisper
            device_count = ctranslate2.get_cuda_device_count()
        except Exception as e:
            return False, f"CUDA probe failed: {e}"

        if device_count < 1:
            return False, "no GPU visible to the container"

        return True, f"{device_count} CUDA device(s) available"

    def _get_device_config(self) -> tuple[str, str]:
        """Determine device and compute type with auto-detection."""
        device = self.whisper_device
        compute_type = self.whisper_compute_type
        
        if device == 'auto':
            cuda_ok, reason = self._detect_cuda()
            if cuda_ok:
                device = 'cuda'
                logger.info(f"🎮 CUDA enabled: {reason}")
            else:
                device = 'cpu'
                logger.info(f"💻 Using CPU: {reason}")
        
        if compute_type == 'auto':
            compute_type = 'float16' if device == 'cuda' else 'int8'
        
        return device, compute_type
    
    def _get_model(self):
        """Lazy-load the Whisper model."""
        if self._model is None:
            from faster_whisper import WhisperModel
            device, compute_type = self._get_device_config()
            logger.info(f"⚙️ Loading Whisper: model={self.model_size}, device={device}, compute_type={compute_type}")
            
            model_kwargs = {'device': device, 'compute_type': compute_type}
            if device == 'cpu':
                model_kwargs['cpu_threads'] = 4
            
            self._model = WhisperModel(
                self.model_size,
                download_root=str(Path(os.environ.get("DATA_DIR", "/data")) / "models"),
                **model_kwargs,
            )
        return self._model
    
    def transcribe(self, audio_path: Path, progress_callback=None) -> list[dict]:
        """Transcribe using local Whisper model."""
        model = self._get_model()
        segments_out = []
        
        logger.info(f"🧠 Transcribing with {self.get_name()}: {audio_path.name}")
        segments, info = model.transcribe(str(audio_path), beam_size=1, best_of=1)
        
        for segment in segments:
            segments_out.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip()
            })
        
        logger.info(f"✅ Transcription complete: {len(segments_out)} segments")
        return segments_out


class DeepgramProvider(TranscriptionProvider):
    """Cloud transcription using Deepgram API."""
    
    def __init__(self):
        self.api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        self.model = os.environ.get("DEEPGRAM_MODEL", "nova-2")
    
    def get_name(self) -> str:
        return f"Deepgram ({self.model})"
    
    def transcribe(self, audio_path: Path, progress_callback=None) -> list[dict]:
        """Transcribe using Deepgram API."""
        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY not configured")
        
        # Import from the current SDK location.
        from deepgram import DeepgramClient
        
        logger.info(f"☁️ Transcribing with {self.get_name()}: {audio_path.name}")
        
        # Initialize the client with the API key.
        client = DeepgramClient(api_key=self.api_key)
        
        with open(audio_path, "rb") as f:
            buffer_data = f.read()
        
        # Use the API's supported transcription arguments.
        response = client.listen.v1.media.transcribe_file(
            request=buffer_data,
            model=self.model,
            smart_format=True,
            utterances=True,
            punctuate=True,
            request_options={
               "timeout_in_seconds": 600  # Set to 10 minutes (default is 60s)
            }
        )
        
        segments_out = []
        
        # Parse Deepgram response
        if hasattr(response, 'results') and response.results:
            # Try utterances first (preferred for sentence-level segments)
            if hasattr(response.results, 'utterances') and response.results.utterances:
                for utterance in response.results.utterances:
                    segments_out.append({
                        "start": utterance.start,
                        "end": utterance.end,
                        "text": utterance.transcript.strip()
                    })
            # Fallback to channels/words
            elif hasattr(response.results, 'channels') and response.results.channels:
                for channel in response.results.channels:
                    for alternative in channel.alternatives:
                        if hasattr(alternative, 'words') and alternative.words:
                            current_segment = {"start": 0, "end": 0, "text": ""}
                            for word in alternative.words:
                                if not current_segment["text"]:
                                    current_segment["start"] = word.start
                                current_segment["end"] = word.end
                                word_text = getattr(word, 'punctuated_word', word.word) or word.word
                                current_segment["text"] += word_text + " "
                                
                                if word_text and word_text[-1] in '.!?':
                                    current_segment["text"] = current_segment["text"].strip()
                                    segments_out.append(current_segment)
                                    current_segment = {"start": 0, "end": 0, "text": ""}
                            
                            if current_segment["text"]:
                                current_segment["text"] = current_segment["text"].strip()
                                segments_out.append(current_segment)
                        elif hasattr(alternative, 'transcript') and alternative.transcript:
                            segments_out.append({
                                "start": 0,
                                "end": 0,
                                "text": alternative.transcript.strip()
                            })
        
        logger.info(f"✅ Deepgram transcription complete: {len(segments_out)} segments")
        return segments_out

class WhisperCppServerProvider(TranscriptionProvider):
    """Transcription via an OpenAI-compatible HTTP server.

    Works with whisper.cpp's whisper-server, speaches, and parakeet ASR
    servers exposing POST <url> with multipart file upload and
    response_format=verbose_json (segments with start/end timestamps).

    When WHISPER_CPP_SEND_ORIGINAL is true, supports_raw_audio=True tells the
    transcription pipeline to skip the local WAV-conversion and chunk-splitting
    steps: original audio files (mp3/m4b) are uploaded as-is and the server does
    its own decoding and long-audio chunking (e.g. parakeet's -long-audio mode).
    Only enable this for servers that accept non-WAV input and long files.
    """

    def __init__(self):
        url = os.environ.get("WHISPER_CPP_URL", "").strip()
        if not url:
            raise ValueError(
                "WHISPER_CPP_URL is not configured. Set it in Settings → Advanced Options."
            )
        self.server_url = url
        self.model = os.environ.get("WHISPER_MODEL", "small")
        self.timeout = int(os.environ.get("WHISPER_CPP_TIMEOUT", "600"))
        self.send_original = os.environ.get(
            "WHISPER_CPP_SEND_ORIGINAL", "false"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.supports_raw_audio = self.send_original
        self.chunk_minutes = int(os.environ.get("WHISPER_CPP_CHUNK_MINUTES", "0"))

    def get_name(self) -> str:
        return f"Whisper.cpp (server) - {self.model}"

    def transcribe(self, audio_source, progress_callback=None) -> list[dict]:
        import requests

        source_str = str(audio_source)
        label = source_str.split("/")[-1].split("?")[0] or "audio"
        logger.info(f"🌐 Transcribing with {self.get_name()}: {label}")

        if source_str.startswith(("http://", "https://")):
            # Raw mode with a stream URL: buffer the source to a temp file so the
            # multipart upload has a real file with a known size.
            import tempfile

            suffix = Path(label).suffix or ".mp3"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp_path = Path(tmp.name)
            try:
                with requests.get(source_str, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        tmp.write(chunk)
                tmp.close()
                return self._upload(tmp_path, label)
            finally:
                tmp.close()
                tmp_path.unlink(missing_ok=True)

        return self._upload(Path(source_str), label)

    def _upload(self, path: Path, filename: str) -> list[dict]:
        import mimetypes

        if self.chunk_minutes > 0 and path.suffix.lower() == ".wav":
            segments_out = self._transcribe_wav_chunked(path)
        else:
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                segments_out = self._post(f, filename, content_type)

        logger.info(f"✅ whisper.cpp transcription complete: {len(segments_out)} segments")
        return segments_out

    def _transcribe_wav_chunked(self, path: Path) -> list[dict]:
        """Split a WAV into chunk_minutes-sized sub-uploads and offset timestamps.

        For servers that return a single merged segment per request (e.g. the
        achetronic/parakeet ASR server), timestamp resolution equals the upload
        length. Slicing the WAV into small requests restores enough granularity
        for alignment while keeping each server-side encoder pass small.
        """
        import io
        import wave

        segments_out = []
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            width = wf.getsampwidth()
            channels = wf.getnchannels()
            frames_per_chunk = rate * 60 * self.chunk_minutes

            idx = 0
            while True:
                frames = wf.readframes(frames_per_chunk)
                if not frames:
                    break
                offset = (idx * frames_per_chunk) / rate

                buf = io.BytesIO()
                with wave.open(buf, "wb") as out:
                    out.setnchannels(channels)
                    out.setsampwidth(width)
                    out.setframerate(rate)
                    out.writeframes(frames)
                buf.seek(0)

                chunk_segs = self._post(buf, f"{path.stem}_c{idx:04d}.wav", "audio/wav")
                for seg in chunk_segs:
                    seg["start"] += offset
                    seg["end"] += offset
                segments_out.extend(chunk_segs)
                idx += 1

        return segments_out

    def _post(self, fileobj, filename: str, content_type: str) -> list[dict]:
        import requests

        files = {
            "file": (filename, fileobj, content_type)
        }
        data = {"model": self.model, "response_format": "verbose_json"}

        response = requests.post(
            self.server_url,
            files=files,
            data=data,
            timeout=self.timeout
        )

        if not response.ok:
            # Surface the server's error body — whisper.cpp / llama-swap explains
            # *why* here (e.g. unknown model id, failed to decode audio). Without
            # this the caller only sees a bare "HTTP 400" and cannot diagnose it.
            body = (response.text or "").strip()
            if len(body) > 500:
                body = body[:500] + "…"
            logger.error(
                f"❌ whisper.cpp server returned HTTP {response.status_code} from "
                f"{self.server_url} (model={self.model}). Response: {body or '<empty body>'}"
            )
            response.raise_for_status()

        result = response.json()

        segments_out = []
        for seg in result.get("segments", []):
            segments_out.append({
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "text": seg["text"].strip()
            })

        if not segments_out and result.get("text"):
            logger.warning(
                "⚠️ Server returned plain text without timestamped segments — "
                "alignment will be degraded. Ensure the server supports verbose_json."
            )
            segments_out.append({"start": 0.0, "end": 0.0, "text": str(result["text"]).strip()})

        return segments_out


def get_transcription_provider() -> TranscriptionProvider:
    """Factory function to get the configured transcription provider."""
    provider_name = os.environ.get("TRANSCRIPTION_PROVIDER", "local").lower()
    
    if provider_name == "deepgram":
        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not api_key:
            logger.warning("⚠️ Deepgram selected but no API key configured, falling back to local")
            return LocalWhisperProvider()
        return DeepgramProvider()
    elif provider_name == "whispercpp":
        url = os.environ.get("WHISPER_CPP_URL", "").strip()
        if not url:
            logger.warning("⚠️ Whisper.cpp selected but no URL configured, falling back to local")
            return LocalWhisperProvider()
        return WhisperCppServerProvider()
    else:
        if provider_name not in ("local", ""):
            logger.warning(f"⚠️ Unknown transcription provider '{provider_name}', falling back to local")
        return LocalWhisperProvider()
