"""Wake-word detection via openWakeWord.

Uses the pretrained "hey_jarvis" model. First call downloads it from
Hugging Face into the openwakeword cache (~tens of MB).

openWakeWord's `predict()` accepts any chunk size and buffers internally,
so we can feed our 32 ms (512-sample) chunks directly.
"""

from __future__ import annotations

import logging

import numpy as np
from openwakeword.model import Model
from openwakeword.utils import download_models

log = logging.getLogger(__name__)

WAKEWORD = "hey_jarvis"


class WakeDetector:
    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = threshold
        log.info("loading openwakeword model %r…", WAKEWORD)
        # Ensure the pretrained set is on disk before instantiating the model.
        download_models([WAKEWORD])
        self.model = Model(
            wakeword_models=[WAKEWORD],
            inference_framework="onnx",
        )
        log.info("wake-word model ready (threshold=%.2f)", threshold)

    def reset(self) -> None:
        """Clear internal buffer — call after a wake fires so the next
        utterance doesn't re-trigger on the tail of the same speech."""
        self.model.reset()

    def feed(self, chunk_int16: np.ndarray) -> float:
        """Feed one audio chunk; return current confidence for the wake word."""
        preds = self.model.predict(chunk_int16)
        return float(preds.get(WAKEWORD, 0.0))

    def fired(self, score: float) -> bool:
        return score >= self.threshold
