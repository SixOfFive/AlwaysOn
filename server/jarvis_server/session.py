"""Per-client WebSocket session.

State machine: HELLO → idle → (wake → audio frames → end_utterance → reply)*

Audio frames between Wake and EndUtterance are PCM (s16le, 16kHz, mono).
Today they're just counted; STT plumbing lands next.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from jarvis_shared import (
    PROTOCOL_VERSION,
    EndUtterance,
    ErrorMsg,
    Hello,
    Pong,
    Say,
    Transcript,
    Wake,
    Welcome,
    parse_control,
)

log = logging.getLogger(__name__)


class Session:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.client_id: str | None = None
        self.hostname: str | None = None
        self.audio_frames = 0
        self.audio_bytes = 0

    async def run(self) -> None:
        try:
            await self._handshake()
            await self._main_loop()
        except WebSocketDisconnect:
            log.info("client disconnected: %s", self.client_id)

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
            elif (data := event.get("bytes")) is not None:
                # PCM frame during an active utterance.
                self.audio_frames += 1
                self.audio_bytes += len(data)

    async def _on_control(self, raw: str) -> None:
        try:
            msg = parse_control(raw)
        except ValidationError as exc:
            log.warning("bad control frame: %s", exc)
            await self._send(ErrorMsg(code="bad_frame", message=str(exc)))
            return

        if isinstance(msg, Wake):
            self.audio_frames = 0
            self.audio_bytes = 0
            log.info("wake: keyword=%r conf=%.2f", msg.keyword, msg.confidence)

        elif isinstance(msg, EndUtterance):
            log.info(
                "end_utterance: %d frames / %d bytes buffered",
                self.audio_frames, self.audio_bytes,
            )
            # Stub: until STT is wired up, echo a placeholder.
            await self._send(Transcript(text="(stt not wired yet)", final=True))
            await self._send(Say(text="I heard you, but my ears are not connected yet."))

        elif msg.type == "ping":
            await self._send(Pong())

        elif msg.type == "cancel":
            log.info("cancel")

        else:
            log.warning("unexpected control frame from client: %s", msg.type)

    async def _send(self, msg: object) -> None:
        # Pydantic model → JSON string. model_dump_json keeps it tight.
        await self.ws.send_text(msg.model_dump_json())  # type: ignore[attr-defined]
