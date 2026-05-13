"""Stream-mode: mic → local VAD → ship audio to server → TTS reply.

The Windows/Linux counterpart to the Android ENGINE_SERVER path. Audio
capture and Silero VAD run on the client; transcription, the wake-phrase
trigger filter, and Claude/LLM routing all happen on the server.

Flow per utterance:
  1. AudioCapture yields 32 ms / 512-sample chunks.
  2. SpeechSegmenter accumulates until VAD declares end-of-speech.
  3. We ship `Wake(keyword="")` + raw PCM bytes + `EndUtterance` over WS.
     Empty keyword tells the server "no on-device wake — please apply
     your transcript-based trigger filter before routing".
  4. Server transcribes, sends back Transcript (for the log) and Say
     (the spoken reply) when the trigger fires.
  5. Local TTS speaks the reply; mic chunks captured during TTS are
     dropped entirely so the assistant can't hear itself talk.

No on-device whisper model needed — the server has the big one.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import time

import websockets
from pydantic import ValidationError

from jarvis_client.audio import AudioCapture
from jarvis_client.segmenter import SpeechSegmenter
from jarvis_client.tts import TTS
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
    client_id: str | None,
    audio_device: int | str | None,
    speech_threshold: float,
    min_silence_ms: int,
) -> None:
    segmenter = SpeechSegmenter(
        speech_threshold=speech_threshold,
        min_silence_ms=min_silence_ms,
    )
    tts = TTS()

    capture = AudioCapture(device=audio_device)
    chunks = await capture.start()

    ws = await _connect(server_url, client_id or f"{socket.gethostname()}-mic")

    # `speaking` is set when TTS is playing AND the server requested
    # mic-mute (mute_mic=true on Say, which is the default). The audio
    # loop drops chunks at the top while it's set so neither VAD nor
    # the server ever sees the TTS audio.
    speaking = asyncio.Event()
    reader = asyncio.create_task(_read_server(ws, tts, speaking))

    banner = (f"\n[stream mode] connected to {server_url}. "
              "Server transcribes, applies trigger, routes. Ctrl-C to stop.\n")
    print(banner, file=sys.stderr, flush=True)

    try:
        while True:
            chunk = await chunks.get()
            if speaking.is_set():
                continue
            segment = segmenter.feed(chunk)
            if segment is None:
                continue

            # VAD just declared end-of-speech. Ship Wake → PCM → EndUtterance.
            await ws.send(Wake(
                keyword="",  # tell the server we did no on-device wake check
                confidence=0.0,
                unix_millis=int(time.time() * 1000),
            ).model_dump_json())
            await ws.send(segment)
            await ws.send(EndUtterance().model_dump_json())
            log.info("shipped %d-byte segment to server", len(segment))
    except KeyboardInterrupt:
        pass
    except websockets.ConnectionClosed:
        log.info("server closed connection")
    finally:
        reader.cancel()
        capture.stop()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


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

            if isinstance(msg, Transcript) and msg.final and msg.text:
                # The server's transcript is the source of truth in this
                # mode. Mirror it to stdout so the user can see what the
                # server heard, whether or not it triggered routing.
                sys.stdout.write(msg.text + "\n")
                sys.stdout.flush()

            elif isinstance(msg, Say):
                if not msg.text:
                    continue
                sys.stdout.write(f"jarvis> {msg.text}\n")
                sys.stdout.flush()
                if msg.mute_mic:
                    speaking.set()
                try:
                    await tts.say(msg.text)
                finally:
                    speaking.clear()

            elif isinstance(msg, Thinking):
                log.info("thinking… %s", msg.note)

            elif isinstance(msg, ErrorMsg):
                log.error("server error %s: %s", msg.code, msg.message)
    except websockets.ConnectionClosed:
        log.info("server closed connection")
