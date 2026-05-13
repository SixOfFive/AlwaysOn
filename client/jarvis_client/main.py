"""Client entry: dial server, handshake, hand off to pipeline (or run
the synthetic-utterance smoke test in --synthetic mode)."""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import websockets
from pydantic import ValidationError

from jarvis_client.pipeline import Pipeline
from jarvis_shared import (
    EndUtterance,
    ErrorMsg,
    Hello,
    Say,
    Thinking,
    Transcript,
    Wake,
    Welcome,
    parse_control,
)

log = logging.getLogger(__name__)


async def run(
    *,
    server_url: str,
    client_id: str,
    mode: str,
    audio_device: int | str | None,
    wake_threshold: float,
) -> None:
    log.info("dialing %s as %s (mode=%s)", server_url, client_id, mode)
    async with websockets.connect(server_url) as ws:
        await _handshake(ws, client_id)

        if mode == "synthetic":
            await _synthetic_loop(ws)
        else:
            pipeline = Pipeline(
                ws,
                audio_device=audio_device,
                wake_threshold=wake_threshold,
            )
            await pipeline.run()


async def _handshake(ws: websockets.ClientConnection, client_id: str) -> None:
    hello = Hello(client_id=client_id, hostname=socket.gethostname())
    await ws.send(hello.model_dump_json())

    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    try:
        msg = parse_control(raw)
    except ValidationError as exc:
        raise RuntimeError(f"server sent unparseable welcome: {exc}") from exc

    if not isinstance(msg, Welcome):
        raise RuntimeError(f"expected welcome, got {msg.type}")
    log.info("server welcomed: session=%s", msg.session_id)


# --- synthetic mode: keeps the smoke test alive after the audio pipeline lands ---

async def _synthetic_loop(ws: websockets.ClientConnection) -> None:
    asyncio.create_task(_synthetic_utterance(ws))
    try:
        async for raw in ws:
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
            elif isinstance(msg, Say):
                log.info("SAY: %s", msg.text)
            elif isinstance(msg, Thinking):
                log.info("thinking… %s", msg.note)
            elif isinstance(msg, ErrorMsg):
                log.error("server error %s: %s", msg.code, msg.message)
    except websockets.ConnectionClosed:
        log.info("server closed connection")


async def _synthetic_utterance(ws: websockets.ClientConnection) -> None:
    await asyncio.sleep(0.5)
    await ws.send(Wake(
        keyword="hey-jarvis",
        confidence=0.0,
        unix_millis=int(time.time() * 1000),
    ).model_dump_json())
    silence = bytes(3200)  # 100 ms s16le @ 16 kHz
    for _ in range(5):
        await ws.send(silence)
    await ws.send(EndUtterance().model_dump_json())
