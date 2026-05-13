"""Text-to-speech playback via pyttsx3 (Windows SAPI).

pyttsx3 is blocking, so we run it in a worker thread. Quality is fine for
a v1; we'll swap in Piper or Coqui later for a more natural voice.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pyttsx3

log = logging.getLogger(__name__)


class TTS:
    def __init__(self, rate: int | None = None, voice: str | None = None) -> None:
        self._lock = threading.Lock()
        self._engine: pyttsx3.Engine | None = None
        self._rate = rate
        self._voice = voice

    def _ensure_engine(self) -> pyttsx3.Engine:
        # SAPI engines are not safe to share across threads, and pyttsx3
        # gets unhappy if reused after stop(). Instantiate fresh per call
        # — cheap on Windows.
        engine = pyttsx3.init()
        if self._rate is not None:
            engine.setProperty("rate", self._rate)
        if self._voice is not None:
            engine.setProperty("voice", self._voice)
        return engine

    def _say_blocking(self, text: str) -> None:
        with self._lock:
            engine = self._ensure_engine()
            engine.say(text)
            engine.runAndWait()
            engine.stop()

    async def say(self, text: str) -> None:
        log.info("TTS: %s", text)
        await asyncio.to_thread(self._say_blocking, text)
