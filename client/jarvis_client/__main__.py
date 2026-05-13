"""Entry point: `python -m jarvis_client`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys

from jarvis_client.audio import list_devices


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-client")
    parser.add_argument(
        "--mode",
        choices=("transcribe", "live", "synthetic"),
        default="transcribe",
        help=(
            "transcribe (default): VAD-gated local STT, prints to stdout, "
            "no server. live: wake-word + server (legacy). "
            "synthetic: wire smoke test."
        ),
    )
    parser.add_argument("--server", default="ws://127.0.0.1:7333/ws")
    parser.add_argument("--id", dest="client_id", default=f"{socket.gethostname()}-mic")
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--audio-device", default=None,
        help="Audio input device index or name (default: system default).",
    )
    parser.add_argument(
        "--list-audio-devices", action="store_true",
        help="Print available audio input devices and exit.",
    )
    # transcribe-mode knobs
    parser.add_argument("--stt-model", default="small.en",
                        help="faster-whisper model (tiny.en|base.en|small.en|medium.en|...).")
    parser.add_argument("--stt-device", default="cpu", help="cpu or cuda.")
    parser.add_argument("--stt-compute-type", default="int8",
                        help="int8 (CPU) or float16 (CUDA), etc.")
    parser.add_argument("--speech-threshold", type=float, default=0.5,
                        help="silero-vad speech probability threshold (0.0-1.0).")
    parser.add_argument("--min-silence-ms", type=int, default=700,
                        help="silence length that ends a speech segment.")
    # live-mode knobs
    parser.add_argument("--wake-threshold", type=float, default=0.5,
                        help="openWakeWord threshold (live mode only).")

    args = parser.parse_args()

    if args.list_audio_devices:
        print(list_devices())
        sys.exit(0)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [client] %(levelname)s %(name)s — %(message)s",
    )

    device: int | str | None = args.audio_device
    if isinstance(device, str) and device.isdigit():
        device = int(device)

    try:
        if args.mode == "transcribe":
            from jarvis_client.listen import run as run_transcribe
            asyncio.run(run_transcribe(
                audio_device=device,
                stt_model=args.stt_model,
                stt_device=args.stt_device,
                stt_compute_type=args.stt_compute_type,
                speech_threshold=args.speech_threshold,
                min_silence_ms=args.min_silence_ms,
            ))
        else:
            # live / synthetic still go through the server.
            from jarvis_client.main import run as run_with_server
            asyncio.run(run_with_server(
                server_url=args.server,
                client_id=args.client_id,
                mode=args.mode,
                audio_device=device,
                wake_threshold=args.wake_threshold,
            ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
