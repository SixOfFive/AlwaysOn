"""End-to-end audio pipeline: mic → wake word → VAD-bounded stream → server → TTS reply.

State machine:

    IDLE       — wake detector running, audio not forwarded
    STREAMING  — wake fired; PCM frames forwarded to server, VAD watching for end-of-speech
    SPEAKING   — server replied with Say; TTS playing; wake suppressed

(Self-wake while speaking is suppressed to avoid the assistant hearing
itself. v1 doesn't support barge-in.)
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time

import websockets
from pydantic import ValidationError

from jarvis_client.audio import AudioCapture
from jarvis_client.tts import TTS
from jarvis_client.vad import EndOfUtteranceDetector
from jarvis_client.wake import WakeDetector, WAKEWORD
from jarvis_shared import (
    EndUtterance,
    ErrorMsg,
    Say,
    Thinking,
    Transcript,
    Wake,
    parse_control,
)

log = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    STREAMING = "streaming"
    SPEAKING = "speaking"


class Pipeline:
    def __init__(
        self,
        ws: websockets.ClientConnection,
        *,
        audio_device: int | str | None = None,
        wake_threshold: float = 0.5,
    ) -> None:
        self.ws = ws
        self.state = State.IDLE
        self.audio = AudioCapture(device=audio_device)
        self.wake = WakeDetector(threshold=wake_threshold)
        self.vad = EndOfUtteranceDetector()
        self.tts = TTS()

    async def run(self) -> None:
        chunks = await self.audio.start()
        log.info("listening for %r — speak when ready", WAKEWORD)

        # Server → client frames run in parallel.
        reader = asyncio.create_task(self._read_server())
        try:
            await self._capture_loop(chunks)
        finally:
            reader.cancel()
            self.audio.stop()

    async def _capture_loop(self, chunks: asyncio.Queue) -> None:
        while True:
            chunk = await chunks.get()

            if self.state == State.SPEAKING:
                # Drop frames while we're talking back, to avoid self-wake.
                continue

            if self.state == State.IDLE:
                score = self.wake.feed(chunk)
                if self.wake.fired(score):
                    log.info("WAKE fired (%.2f)", score)
                    self.wake.reset()
                    self.vad.reset()
                    await self.ws.send(Wake(
                        keyword=WAKEWORD,
                        confidence=score,
                        unix_millis=int(time.time() * 1000),
                    ).model_dump_json())
                    self.state = State.STREAMING

            elif self.state == State.STREAMING:
                # Forward raw PCM bytes to the server.
                await self.ws.send(chunk.tobytes())

                # Watch for end of utterance.
                if self.vad.feed(chunk):
                    log.info("end of utterance")
                    await self.ws.send(EndUtterance().model_dump_json())
                    self.state = State.SPEAKING  # await server reply

    async def _read_server(self) -> None:
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = parse_control(raw)
                except ValidationError as exc:
                    log.warning("bad frame from server: %s", exc)
                    continue

                if isinstance(msg, Transcript):
                    tag = "final" if msg.final else "interim"
                    log.info("transcript (%s): %r", tag, msg.text)

                elif isinstance(msg, Thinking):
                    log.info("thinking… %s", msg.note)

                elif isinstance(msg, Say):
                    await self.tts.say(msg.text)
                    # Back to listening.
                    self.wake.reset()
                    self.state = State.IDLE
                    log.info("listening for %r", WAKEWORD)

                elif isinstance(msg, ErrorMsg):
                    log.error("server error %s: %s", msg.code, msg.message)
                    self.state = State.IDLE
        except websockets.ConnectionClosed:
            log.info("server closed connection")
