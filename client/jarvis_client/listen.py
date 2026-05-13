"""Transcribe-mode: mic → VAD → Whisper → stdout, optionally → server.

Flow:
- Every speech segment is transcribed locally and printed.
- If --server is set and the transcript contains "jarvis", the text
  *after* the trigger is sent to the server as a Command. The server's
  router decides whether to fast-path it (time, date, weather, ...) or
  forward to Claude with full tool access.
- The server's Say reply is spoken via local TTS.
- Audio captured while the assistant is speaking is ignored, to keep
  the TTS output from triggering itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import sys

import websockets
from pydantic import ValidationError

from jarvis_client.audio import AudioCapture
from jarvis_client.segmenter import SpeechSegmenter, segments_from
from jarvis_client.stt import STT
from jarvis_client.tts import TTS
from jarvis_shared import (
    Command,
    ErrorMsg,
    Hello,
    Say,
    Thinking,
    Transcript,
    Welcome,
    parse_control,
)

log = logging.getLogger(__name__)

# Match "jarvis" / "hey jarvis" / "ok jarvis" anywhere in the transcript.
# We return the text *after* the match as the command.
_TRIGGER = re.compile(r"\b(?:hey\s+|ok\s+|okay\s+)?jarvis\b[\s,.\-:;!?]*", re.IGNORECASE)


def extract_command(text: str) -> str | None:
    """Return the command after the wake phrase, or None if the wake
    phrase isn't present or nothing follows it."""
    m = _TRIGGER.search(text)
    if not m:
        return None
    rest = text[m.end():].strip()
    return rest if rest else None


async def run(
    *,
    audio_device: int | str | None,
    stt_model: str,
    stt_device: str,
    stt_compute_type: str,
    speech_threshold: float,
    min_silence_ms: int,
    server_url: str | None,
    client_id: str | None,
) -> None:
    stt = STT(model_name=stt_model, device=stt_device, compute_type=stt_compute_type)
    segmenter = SpeechSegmenter(
        speech_threshold=speech_threshold,
        min_silence_ms=min_silence_ms,
    )
    tts = TTS()

    capture = AudioCapture(device=audio_device)
    raw_chunks = await capture.start()
    segments = await segments_from(raw_chunks, segmenter)

    ws: websockets.ClientConnection | None = None
    if server_url:
        ws = await _connect(server_url, client_id or f"{socket.gethostname()}-mic")

    speaking = asyncio.Event()  # set while TTS is playing
    if ws is not None:
        asyncio.create_task(_read_server(ws, tts, speaking))
        banner = (f"\n[transcribe + jarvis trigger] connected to {server_url}. "
                  "Say 'jarvis <command>' to act. Ctrl-C to stop.\n")
    else:
        banner = ("\n[transcribe mode] no server — printing only. "
                  "Ctrl-C to stop.\n")
    print(banner, file=sys.stderr, flush=True)

    try:
        while True:
            pcm = await segments.get()
            if speaking.is_set():
                # Drop anything captured while TTS was speaking.
                continue
            text = await stt.transcribe(pcm)
            if not text:
                continue
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

            command = extract_command(text)
            if command and ws is not None:
                log.info("→ jarvis: %s", command)
                try:
                    await ws.send(Command(text=command).model_dump_json())
                except websockets.ConnectionClosed:
                    log.warning("server connection dropped")
                    ws = None
    finally:
        capture.stop()
        if ws is not None:
            await ws.close()


async def _connect(url: str, client_id: str) -> websockets.ClientConnection:
    log.info("dialing %s as %s", url, client_id)
    ws = await websockets.connect(url)
    await ws.send(Hello(client_id=client_id, hostname=socket.gethostname()).model_dump_json())

    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    try:
        msg = parse_control(raw)
    except ValidationError as exc:
        raise RuntimeError(f"server sent unparseable welcome: {exc}") from exc
    if not isinstance(msg, Welcome):
        raise RuntimeError(f"expected welcome, got {msg.type}")
    log.info("server welcomed: session=%s", msg.session_id)
    return ws


async def _read_server(
    ws: websockets.ClientConnection,
    tts: TTS,
    speaking: asyncio.Event,
) -> None:
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = parse_control(raw)
            except ValidationError as exc:
                log.warning("bad frame from server: %s", exc)
                continue

            if isinstance(msg, Say):
                if not msg.text:
                    continue
                sys.stdout.write(f"jarvis> {msg.text}\n")
                sys.stdout.flush()
                speaking.set()
                try:
                    await tts.say(msg.text)
                finally:
                    speaking.clear()
            elif isinstance(msg, Thinking):
                log.info("thinking… %s", msg.note)
            elif isinstance(msg, Transcript):
                # server's STT echo — irrelevant in client-STT mode
                pass
            elif isinstance(msg, ErrorMsg):
                log.error("server error %s: %s", msg.code, msg.message)
    except websockets.ConnectionClosed:
        log.info("server closed connection")
