"""CTC forced alignment via torchaudio MMS_FA (issue #426, Phase B).

Aligns an audiobook directly against the *known* ebook text and emits dense
per-word ``{char, ts}`` anchors — no transcription and no lexical n-gram matching.
The output slots straight into the same alignment map the lexical pipeline
produces, in the canonical ``full_text`` character space that
``EbookParser.get_locator_from_char_offset`` re-parses.

torch/torchaudio are heavy and ship only in the opt-in ``-ctc`` image, so every
import here is lazy: on the standard image ``ForcedAligner.is_available()`` is
False and callers fall back to the Whisper/lexical pipeline. Any failure inside
``align`` returns ``None`` for the same reason — a broken alignment must never be
worse than the existing path.

MMS_FA facts (verified against torchaudio 2.11): the token vocabulary is lowercase
``a``–``z`` plus apostrophe (blank ``-`` = index 0, star ``*`` = last index), at a
16 kHz sample rate. Words are normalized to that character set; anything else
(digits, punctuation) is dropped, and a word left empty is skipped — its char
offset simply does not appear as an anchor, which the dense remaining anchors and
linear interpolation absorb.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# MMS_FA accepts lowercase latin + apostrophe only.
_MMS_CHARS_RE = re.compile(r"[^a-z']")
# Emission is computed in windows of this many seconds to bound peak memory on the
# model forward pass for long audiobooks; the log-prob frames are concatenated.
_EMIT_WINDOW_SECONDS = 30


class ForcedAligner:
    """Lazily-loaded torchaudio MMS_FA forced aligner (see module docstring)."""

    def __init__(self):
        self._model = None
        self._dict: Optional[Dict[str, int]] = None
        self._device = None
        self._sample_rate = 16000
        # Temp decoded-audio files to unlink after each align (see _load_audio).
        self._tmp_audio_files: List[str] = []

    # -- availability -------------------------------------------------------- #

    @staticmethod
    def is_available() -> bool:
        """True when torch + torchaudio are importable (i.e. on the -ctc image)."""
        import importlib.util
        try:
            return bool(
                importlib.util.find_spec("torch")
                and importlib.util.find_spec("torchaudio")
            )
        except (ImportError, ValueError):
            return False

    def _resolve_device(self) -> str:
        pref = os.environ.get("CTC_DEVICE", "auto").strip().lower()
        import torch
        if pref == "cuda":
            return "cuda" if torch.cuda.is_available() else "cpu"
        if pref == "cpu":
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    def _load(self):
        """Load and cache the MMS_FA model + token dictionary."""
        if self._model is not None:
            return
        import torch  # noqa: F401
        import torchaudio

        bundle = torchaudio.pipelines.MMS_FA
        self._sample_rate = bundle.sample_rate
        self._device = self._resolve_device()
        model = os.environ.get("CTC_MODEL", "mms_fa").strip().lower()
        if model not in ("", "mms_fa"):
            logger.warning(
                f"⚠️ CTC: unknown CTC_MODEL '{model}', using mms_fa (the only supported bundle)"
            )
        logger.info(f"⚙️ CTC: loading MMS_FA forced aligner on {self._device}")
        self._model = bundle.get_model(with_star=False).to(self._device).eval()
        self._dict = bundle.get_dict()

    # -- pure helpers (unit-tested without torch) ---------------------------- #

    @staticmethod
    def _book_words(full_text: str) -> List[Tuple[str, int]]:
        """Return ``[(mms_word, char_offset)]`` for each whitespace token.

        ``char_offset`` indexes into ``full_text`` (the canonical coordinate space
        the locator resolver re-parses). Words that hold no MMS-alignable character
        are dropped, so every entry can be tokenized and aligned.
        """
        words: List[Tuple[str, int]] = []
        for match in re.finditer(r"\S+", full_text):
            mms = _MMS_CHARS_RE.sub("", match.group().lower())
            if mms:
                words.append((mms, match.start()))
        return words

    @staticmethod
    def _build_map(
        entries: List[Tuple[str, int]],
        word_start_times: List[float],
        full_text: str,
    ) -> List[Dict]:
        """Assemble the ``[{char, ts}]`` map from aligned word start times.

        ``entries`` and ``word_start_times`` are parallel and already in reading
        order, so the anchors are monotonic by construction. A ``(0, 0.0)`` head
        and a ``(len(full_text), last_ts)`` tail are added so interpolation covers
        the whole book.
        """
        if not entries or len(entries) != len(word_start_times):
            return []
        anchors: List[Dict] = []
        if entries[0][1] > 0:
            anchors.append({"char": 0, "ts": 0.0})
        for (_word, char), ts in zip(entries, word_start_times):
            anchors.append({"char": int(char), "ts": round(float(ts), 3)})
        last_ts = anchors[-1]["ts"]
        if anchors[-1]["char"] < len(full_text):
            anchors.append({"char": len(full_text), "ts": last_ts})
        return anchors

    # -- alignment ----------------------------------------------------------- #

    def _emissions(self, waveform):
        """Model forward in windows; concatenate log-prob frames [1, T, C]."""
        import torch

        window = int(_EMIT_WINDOW_SECONDS * self._sample_rate)
        total = waveform.size(1)
        chunks = []
        with torch.inference_mode():
            for start in range(0, total, window):
                piece = waveform[:, start : start + window].to(self._device)
                emission, _ = self._model(piece)
                # forced_align dispatches on the emission device.
                chunks.append(emission)
                if len(chunks) % 10 == 0 or start + window >= total:
                    logger.info(
                        "⚙️ CTC: emissions %.0f/%.0fs on %s",
                        min(start + window, total) / self._sample_rate,
                        total / self._sample_rate, self._device,
                    )
        return torch.cat(chunks, dim=1)

    def _load_audio(self, audio_paths):
        """Decode one or more parts to a mono float32 waveform at the model rate.

        Decoding goes through the ffmpeg CLI (already required by the image for audio
        normalization) rather than ``torchaudio.load``: it accepts every audiobook
        container (m4b/mp3/…), downmixes and resamples in one pass, and avoids
        torchaudio's torchcodec backend and its strict FFmpeg-version coupling.

        All parts are streamed sequentially into a single temp file on disk and the
        returned waveform is a memory-map of it, so RAM stays bounded to the windows
        ``_emissions`` actually touches. Decoding a multi-hour book straight into a RAM
        buffer (subprocess PIPE + numpy copy) peaked at several GB and OOM-killed the
        container (#426). The temp file is unlinked by ``align`` after use.
        """
        import subprocess
        import tempfile

        import numpy as np
        import torch

        if isinstance(audio_paths, (str, os.PathLike)):
            audio_paths = [audio_paths]

        tmp = tempfile.NamedTemporaryFile(suffix=".f32le", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            with open(tmp_path, "wb") as out:
                for path in audio_paths:
                    subprocess.run(
                        [
                            "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
                            "-f", "f32le", "-ac", "1", "-ar", str(self._sample_rate), "pipe:1",
                        ],
                        stdout=out, stderr=subprocess.PIPE, check=True,
                    )
            num_samples = os.path.getsize(tmp_path) // 4
            if num_samples == 0:
                raise ValueError("ffmpeg produced no audio samples")
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        self._tmp_audio_files.append(tmp_path)
        mm = np.memmap(tmp_path, dtype="<f4", mode="r", shape=(num_samples,))
        return torch.from_numpy(mm).unsqueeze(0)

    def _cleanup_tmp_audio(self):
        while self._tmp_audio_files:
            path = self._tmp_audio_files.pop()
            try:
                os.unlink(path)
            except OSError:
                pass

    def align(self, audio_paths, full_text: str,
              text_range: Optional[Tuple[int, int]] = None) -> Optional[List[Dict]]:
        """Force-align audio (one path or a list of parts) to ``full_text``.

        Returns a ``{char, ts}`` map, or ``None`` on any failure (missing deps,
        decode error, empty text) so the caller can fall back to the lexical pipeline.
        """
        if not self.is_available():
            return None
        start, end = text_range if text_range is not None else (0, len(full_text))
        if not 0 <= start < end <= len(full_text):
            return None
        entries = [(word, char + start) for word, char in self._book_words(full_text[start:end])]
        if not entries:
            logger.warning("⚠️ CTC: no alignable words in ebook text; skipping")
            return None
        try:
            import torch
            import torchaudio.functional as F

            self._load()

            targets: List[int] = []
            word_token_counts: List[int] = []
            kept: List[Tuple[str, int]] = []
            for word, char in entries:
                ids = [self._dict[c] for c in word if c in self._dict]
                if not ids:
                    continue
                targets.extend(ids)
                word_token_counts.append(len(ids))
                kept.append((word, char))
            if not targets:
                return None

            logger.info("⚙️ CTC: decoding audio at %s Hz", self._sample_rate)
            waveform = self._load_audio(audio_paths)
            logger.info(
                "⚙️ CTC: decoded %.0fs audio; computing emissions on %s",
                waveform.size(1) / self._sample_rate, self._device,
            )
            emission = self._emissions(waveform)  # [1, T, C], log-probs
            num_frames = emission.size(1)
            seconds_per_frame = waveform.size(1) / num_frames / self._sample_rate

            # forced_align allocates a work buffer that grows ~with frames x tokens.
            # An oversized single pass does not raise a catchable error — it aborts
            # the whole process (CPU: 32-bit back-pointer overflow; GPU: the CUDA
            # kernel exceeds device memory and aborts, taking the container down,
            # #426). Guard BOTH devices and fall back to lexical instead.
            cost = num_frames * (2 * len(targets) + 1)
            if emission.device.type == "cpu":
                if cost > 2**31 - 1:
                    logger.warning(
                        "⚠️ CTC: CPU alignment exceeds the safe back-pointer limit "
                        "(%s frames, %s tokens); falling back to lexical alignment",
                        num_frames, len(targets),
                    )
                    return None
            else:
                try:
                    free_bytes, _total = torch.cuda.mem_get_info(emission.device)
                except Exception:
                    free_bytes = 0
                # ~0.6 B per frame*token observed on a 12 GB card (Buy a Bullet ~2e10
                # fit; a 5.7 h book ~5e11 aborted); keep a safety margin.
                if not (free_bytes and cost * 0.6 < free_bytes * 0.7):
                    logger.warning(
                        "⚠️ CTC: alignment too large for a single GPU pass "
                        "(%s frames, %s tokens, %.1f GB free); falling back to lexical. "
                        "Long books need chunked CTC.",
                        num_frames, len(targets), free_bytes / 1e9,
                    )
                    return None

            targets_t = torch.tensor([targets], dtype=torch.int32, device=emission.device)
            logger.info(
                "⚙️ CTC: forced_align on %s (%s frames, %s tokens)",
                emission.device, num_frames, len(targets),
            )
            aligned, scores = F.forced_align(emission, targets_t, blank=0)
            # Span reduction is a Python loop; transfer once after the GPU align
            # instead of synchronizing CUDA for every token's mean score.
            spans = F.merge_tokens(aligned[0].cpu(), scores[0].cpu(), blank=0)
            if len(spans) != len(targets):
                logger.warning(
                    f"⚠️ CTC: token/span mismatch ({len(spans)} vs {len(targets)}); "
                    "skipping to fall back to lexical alignment"
                )
                return None

            word_start_times: List[float] = []
            cursor = 0
            for count in word_token_counts:
                start_frame = spans[cursor].start
                word_start_times.append(start_frame * seconds_per_frame)
                cursor += count

            # Keep canonical EPUB offsets, but do not map the end of narration to
            # the end of an unnarrated bonus excerpt.
            alignment_map = self._build_map(kept, word_start_times, full_text[:end])
            logger.info(
                f"🎯 CTC: forced-aligned {len(kept)} words -> {len(alignment_map)} anchors "
                f"({num_frames} frames, {seconds_per_frame * num_frames:.0f}s audio)"
            )
            return alignment_map or None

        except Exception as e:
            logger.error(f"❌ CTC forced alignment failed: {e}", exc_info=True)
            return None
        finally:
            self._cleanup_tmp_audio()
