"""Voice Activity Detection via Silero VAD.

Used after a wake fires, to detect when the user has finished speaking
(end of utterance). Silero expects exactly 512-sample chunks at 16 kHz,
which matches our capture chunk size.
"""

from __future__ import annotations

import logging

import numpy as np
import torch
from silero_vad import load_silero_vad

log = logging.getLogger(__name__)


class EndOfUtteranceDetector:
    """Tracks consecutive silence after a wake. Fires `is_done()` when
    we've seen `silence_ms` worth of non-speech frames in a row.

    Also enforces `max_utterance_ms` as a hard cap so we don't hang if VAD
    misbehaves.
    """

    def __init__(
        self,
        speech_threshold: float = 0.5,
        silence_ms: int = 700,
        max_utterance_ms: int = 10_000,
    ) -> None:
        log.info("loading silero-vad…")
        self.model = load_silero_vad()
        self.speech_threshold = speech_threshold
        self.silence_ms = silence_ms
        self.max_utterance_ms = max_utterance_ms
        self._chunk_ms = 32  # 512 samples @ 16 kHz
        self._silent_streak_ms = 0
        self._elapsed_ms = 0
        self._heard_speech = False
        log.info("silero-vad ready (end-of-utterance after %d ms silence)",
                 silence_ms)

    def reset(self) -> None:
        # Silero keeps RNN state across calls — clear it for a new utterance.
        self.model.reset_states()
        self._silent_streak_ms = 0
        self._elapsed_ms = 0
        self._heard_speech = False

    def feed(self, chunk_int16: np.ndarray) -> bool:
        """Feed one 512-sample chunk. Returns True if utterance is done."""
        self._elapsed_ms += self._chunk_ms

        # Silero wants float32 in [-1, 1].
        chunk_f = torch.from_numpy(chunk_int16.astype(np.float32) / 32768.0)
        with torch.no_grad():
            prob = float(self.model(chunk_f, 16_000).item())

        if prob >= self.speech_threshold:
            self._heard_speech = True
            self._silent_streak_ms = 0
        else:
            self._silent_streak_ms += self._chunk_ms

        return self.is_done()

    def is_done(self) -> bool:
        if self._elapsed_ms >= self.max_utterance_ms:
            return True
        return self._heard_speech and self._silent_streak_ms >= self.silence_ms
