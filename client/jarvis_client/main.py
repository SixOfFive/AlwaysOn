"""Client connection logic: dial, handshake, dispatch inbound server frames.

Today the only outbound action is an optional synthetic utterance — wake
+ a few silence frames + end_utterance — so we can exercise the wire
without the audio pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import websockets
from pydantic import ValidationError

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


async def run(*, server_url: str, client_id: str, synthetic: bool) -> None:
    log.info("dialing %s as %s", server_url, client_id)
    async with websockets.connect(server_url) as ws:
        await _handshake(ws, client_id)

        recv_task = asyncio.create_task(_read_loop(ws))
        if synthetic:
            asyncio.create_task(_synthetic_utterance(ws))

        try:
            await recv_task
        except websockets.ConnectionClosed:
            log.info("server closed connection")


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


async def _read_loop(ws: websockets.ClientConnection) -> None:
    async for raw in ws:
        if isinstance(raw, bytes):
            # Server doesn't push binary today.
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
        else:
            log.warning("unexpected frame from server: %s", msg.type)


async def _synthetic_utterance(ws: websockets.ClientConnection) -> None:
    """Fake a wake → tiny PCM burst → end_utterance round-trip to verify
    the wire end-to-end before the audio pipeline lands."""
    await asyncio.sleep(0.5)
    await ws.send(Wake(
        keyword="hey-jarvis",
        confidence=0.0,
        unix_millis=int(time.time() * 1000),
    ).model_dump_json())

    # 100 ms of silence at 16 kHz s16le = 1600 samples = 3200 bytes.
    silence = bytes(3200)
    for _ in range(5):
        await ws.send(silence)

    await ws.send(EndUtterance().model_dump_json())
