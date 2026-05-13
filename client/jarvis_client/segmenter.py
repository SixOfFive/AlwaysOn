"""Speech segmenter: VAD-driven chunks of speech, bounded by silence.

Silero VAD is trained on speech specifically, so non-speech sound
(keyboard taps, fan, music transients) does not trigger it the way an
energy-only VAD would.

The segmenter is fed 512-sample chunks at 16 kHz (matches AudioCapture).
It emits complete PCM segments via an asyncio.Queue: each segment is
all the audio from speech-start to speech-end with a configurable
amount of pre-roll padding so we don't clip the first phoneme.
"""

from __future__ import annotations

import asyncio
import collections
import logging

import numpy as np
import torch
from silero_vad import VADIterator, load_silero_vad

log = logging.getLogger(__name__)

CHUNK_SAMPLES = 512        # 32 ms @ 16 kHz
CHUNK_MS = 32
SAMPLE_RATE = 16_000


class SpeechSegmenter:
    """Streaming VAD that yields (PCM bytes, duration_ms) per utterance."""

    def __init__(
        self,
        speech_threshold: float = 0.5,
        min_silence_ms: int = 700,
        speech_pad_ms: int = 200,
        max_segment_ms: int = 20_000,
    ) -> None:
        log.info("loading silero-vad…")
        self.model = load_silero_vad()
        self.vad = VADIterator(
            self.model,
            threshold=speech_threshold,
            sampling_rate=SAMPLE_RATE,
            min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=speech_pad_ms,
        )
        self.max_segment_ms = max_segment_ms

        # Rolling pre-roll buffer so the start of speech isn't clipped
        # by the time VAD has confidently fired.
        pad_chunks = max(1, speech_pad_ms // CHUNK_MS)
        self._preroll: collections.deque[np.ndarray] = collections.deque(maxlen=pad_chunks)

        self._buffer: bytearray = bytearray()
        self._in_speech = False
        self._segment_ms = 0
        log.info("segmenter ready (silence>=%dms ends a segment)", min_silence_ms)

    def feed(self, chunk_int16: np.ndarray) -> bytes | None:
        """Push one 512-sample int16 chunk. Returns a finished segment as
        PCM bytes when speech ends, else None."""
        # VADIterator wants float32 in [-1, 1].
        chunk_f = torch.from_numpy(chunk_int16.astype(np.float32) / 32768.0)
        event = self.vad(chunk_f, return_seconds=False)

        if not self._in_speech:
            self._preroll.append(chunk_int16)

        if event and "start" in event:
            self._in_speech = True
            self._buffer.clear()
            # Prepend pre-roll so we don't lose the first 100-200 ms.
            for c in self._preroll:
                self._buffer.extend(c.tobytes())
            self._buffer.extend(chunk_int16.tobytes())
            self._segment_ms = len(self._preroll) * CHUNK_MS + CHUNK_MS
            return None

        if self._in_speech:
            self._buffer.extend(chunk_int16.tobytes())
            self._segment_ms += CHUNK_MS

        # Hard cap: emit whatever we have if the user is monologuing.
        if self._in_speech and self._segment_ms >= self.max_segment_ms:
            return self._finish()

        if event and "end" in event:
            return self._finish()

        return None

    def _finish(self) -> bytes:
        segment = bytes(self._buffer)
        self._buffer.clear()
        self._in_speech = False
        self._segment_ms = 0
        self._preroll.clear()
        self.vad.reset_states()
        return segment


async def segments_from(
    chunks: asyncio.Queue[np.ndarray],
    segmenter: SpeechSegmenter,
) -> asyncio.Queue[bytes]:
    """Drain the raw-audio queue into a queue of finished speech segments.
    Returns the new queue; spawns a background task that runs forever."""
    out: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)

    async def pump() -> None:
        while True:
            chunk = await chunks.get()
            segment = segmenter.feed(chunk)
            if segment is not None:
                try:
                    out.put_nowait(segment)
                except asyncio.QueueFull:
                    log.warning("segment queue full — dropping utterance")

    asyncio.create_task(pump())
    return out
