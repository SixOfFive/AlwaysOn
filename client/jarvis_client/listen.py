"""Transcribe-only mode: mic -> VAD -> Whisper -> stdout. No server.

Useful for: confirming the audio chain works, eyeballing Whisper's
accuracy in your environment, tuning the silence threshold, deciding
what a real "wake" should look like.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from jarvis_client.audio import AudioCapture
from jarvis_client.segmenter import SpeechSegmenter, segments_from
from jarvis_client.stt import STT

log = logging.getLogger(__name__)


async def run(
    *,
    audio_device: int | str | None,
    stt_model: str,
    stt_device: str,
    stt_compute_type: str,
    speech_threshold: float,
    min_silence_ms: int,
) -> None:
    stt = STT(model_name=stt_model, device=stt_device, compute_type=stt_compute_type)
    segmenter = SpeechSegmenter(
        speech_threshold=speech_threshold,
        min_silence_ms=min_silence_ms,
    )

    capture = AudioCapture(device=audio_device)
    raw_chunks = await capture.start()
    segments = await segments_from(raw_chunks, segmenter)

    print("\n[transcribe mode] speak — silence ends each segment. Ctrl-C to stop.\n",
          file=sys.stderr, flush=True)

    try:
        while True:
            pcm = await segments.get()
            text = await stt.transcribe(pcm)
            if not text:
                continue
            # One line per utterance to stdout, log details to stderr.
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    finally:
        capture.stop()
