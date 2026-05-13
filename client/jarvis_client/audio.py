"""Mic capture via sounddevice.

sounddevice runs its callback on PortAudio's thread; this module bridges
that to asyncio with a thread-safe asyncio.Queue. Chunks are int16 mono
at 16 kHz, 512 samples each (32 ms) — chosen to match Silero VAD's
expected window so we don't have to re-buffer downstream.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHUNK_SAMPLES = 512  # 32 ms — matches silero-vad


class AudioCapture:
    def __init__(self, device: int | str | None = None) -> None:
        self.device = device
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._stream: sd.InputStream | None = None

    async def start(self) -> asyncio.Queue[np.ndarray]:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=200)  # ~6 s of audio if backed up

        def callback(indata, _frames, _time, status):
            if status:
                log.warning("audio status: %s", status)
            # indata is shape (frames, channels) int16; we asked for mono.
            chunk = indata[:, 0].copy()
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
            except RuntimeError:
                pass  # loop closing

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_SAMPLES,
            callback=callback,
            device=self.device,
        )
        self._stream.start()
        log.info("mic capture started (device=%s, %d Hz, %d-sample chunks)",
                 self.device, SAMPLE_RATE, CHUNK_SAMPLES)
        return self._queue

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("mic capture stopped")


def list_devices() -> str:
    """Human-readable device list for `--list-audio-devices`."""
    return str(sd.query_devices())
