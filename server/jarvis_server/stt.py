"""Speech-to-text via faster-whisper.

CPU-only by default so the 4070 stays free for LLM inference. The model
loads once at server startup. Transcription happens off the asyncio
event loop in a thread, since it's CPU-bound and blocking.

Input PCM: signed 16-bit little-endian, 16 kHz, mono — matches what the
client streams.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger(__name__)


class STT:
    def __init__(
        self,
        model_name: str = "small.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
    ) -> None:
        log.info("loading faster-whisper %r on %s (%s)…",
                 model_name, device, compute_type)
        t0 = time.monotonic()
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self.language = language
        log.info("STT ready in %.1fs", time.monotonic() - t0)

    def _transcribe_blocking(self, pcm_s16le: bytes) -> str:
        if len(pcm_s16le) < 2:
            return ""
        audio = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            vad_filter=False,  # client-side VAD already trimmed silence
            beam_size=1,       # fast path; command-style input is short
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, pcm_s16le: bytes) -> str:
        t0 = time.monotonic()
        text = await asyncio.to_thread(self._transcribe_blocking, pcm_s16le)
        log.info("STT: %.0f ms, %d bytes -> %r",
                 (time.monotonic() - t0) * 1000, len(pcm_s16le), text)
        return text
