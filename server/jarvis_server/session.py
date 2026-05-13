"""Per-client WebSocket session.

State machine: HELLO -> idle -> (wake -> audio frames -> end_utterance ->
STT -> router -> say)*

Audio frames between Wake and EndUtterance are raw PCM (s16le, 16 kHz,
mono). They get accumulated into a single bytes buffer that's handed to
faster-whisper at end-of-utterance.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from jarvis_server.active_session import ActiveSession
from jarvis_server.router import Router
from jarvis_server.stt import STT
from jarvis_shared import (
    PROTOCOL_VERSION,
    Command,
    EndUtterance,
    ErrorMsg,
    Hello,
    Pong,
    Say,
    Thinking,
    Transcript,
    Wake,
    Welcome,
    parse_control,
)

log = logging.getLogger(__name__)


class Session:
    def __init__(self, ws: WebSocket, *, stt: STT, router: Router) -> None:
        self.ws = ws
        self.stt = stt
        self.router = router
        self.client_id: str | None = None
        self.hostname: str | None = None
        self._audio = bytearray()
        self._in_utterance = False

    async def run(self) -> None:
        try:
            await self._handshake()
            ActiveSession.set(self)
            await self._main_loop()
        except WebSocketDisconnect:
            log.info("client disconnected: %s", self.client_id)
        finally:
            ActiveSession.clear(self)

    async def _handshake(self) -> None:
        raw = await asyncio.wait_for(self.ws.receive_text(), timeout=5.0)
        try:
            msg = parse_control(raw)
        except ValidationError as exc:
            await self._send(ErrorMsg(code="bad_hello", message=str(exc)))
            await self.ws.close()
            raise

        if not isinstance(msg, Hello):
            await self._send(ErrorMsg(code="expected_hello", message="first frame must be hello"))
            await self.ws.close()
            raise RuntimeError("first frame was not hello")

        if msg.version != PROTOCOL_VERSION:
            await self._send(ErrorMsg(
                code="version_mismatch",
                message=f"server speaks protocol v{PROTOCOL_VERSION}",
            ))
            await self.ws.close()
            raise RuntimeError(f"client speaks v{msg.version}")

        self.client_id = msg.client_id
        self.hostname = msg.hostname
        log.info("client connected: id=%s host=%s", self.client_id, self.hostname)

        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")[:-3]
        await self._send(Welcome(session_id=session_id))

    async def _main_loop(self) -> None:
        while True:
            event = await self.ws.receive()
            if event["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()

            if (data := event.get("text")) is not None:
                await self._on_control(data)
            elif (data := event.get("bytes")) is not None and self._in_utterance:
                self._audio.extend(data)

    async def _on_control(self, raw: str) -> None:
        try:
            msg = parse_control(raw)
        except ValidationError as exc:
            log.warning("bad control frame: %s", exc)
            await self._send(ErrorMsg(code="bad_frame", message=str(exc)))
            return

        if isinstance(msg, Wake):
            self._audio.clear()
            self._in_utterance = True
            log.info("wake: keyword=%r conf=%.2f", msg.keyword, msg.confidence)

        elif isinstance(msg, EndUtterance):
            self._in_utterance = False
            buf = bytes(self._audio)
            self._audio.clear()
            await self._on_utterance(buf)

        elif isinstance(msg, Command):
            # Client transcribed locally; just route the text.
            log.info("command: %r", msg.text)
            await self._on_text(msg.text)

        elif msg.type == "ping":
            await self._send(Pong())

        elif msg.type == "cancel":
            self._in_utterance = False
            self._audio.clear()
            log.info("cancel")

        else:
            log.warning("unexpected control frame from client: %s", msg.type)

    async def _on_utterance(self, pcm: bytes) -> None:
        if len(pcm) < 4_000:  # < 0.125s — discard tap/noise
            log.info("utterance too short (%d bytes), discarding", len(pcm))
            await self._send(Say(text=""))
            return

        await self._send(Thinking(note="transcribing"))
        text = await self.stt.transcribe(pcm)
        await self._send(Transcript(text=text, final=True))
        await self._on_text(text)

    async def _on_text(self, text: str) -> None:
        if not text.strip():
            await self._send(Say(text="I didn't catch that."))
            return

        await self._send(Thinking(note="routing"))
        try:
            reply = await self.router.handle(text)
        except Exception as exc:  # noqa: BLE001
            log.exception("router crashed")
            reply = f"Something went wrong: {exc}"

        await self._send(Say(text=reply))

    async def _send(self, msg: object) -> None:
        await self.ws.send_text(msg.model_dump_json())  # type: ignore[attr-defined]
