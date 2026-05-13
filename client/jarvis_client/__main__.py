"""Entry point: `python -m jarvis_client`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys

from jarvis_client.audio import list_devices
from jarvis_client.main import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-client")
    parser.add_argument("--server", default="ws://127.0.0.1:7333/ws")
    parser.add_argument("--id", dest="client_id", default=f"{socket.gethostname()}-mic")
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--mode",
        choices=("live", "synthetic"),
        default="live",
        help="live = real mic + wake word; synthetic = wire smoke test.",
    )
    parser.add_argument(
        "--audio-device",
        default=None,
        help="Audio input device index or name (default: system default).",
    )
    parser.add_argument(
        "--wake-threshold",
        type=float,
        default=0.5,
        help="openWakeWord confidence threshold (0.0–1.0).",
    )
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="Print available audio input devices and exit.",
    )
    args = parser.parse_args()

    if args.list_audio_devices:
        print(list_devices())
        sys.exit(0)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [client] %(levelname)s %(name)s — %(message)s",
    )

    # Allow numeric device index strings on the CLI.
    device: int | str | None = args.audio_device
    if isinstance(device, str) and device.isdigit():
        device = int(device)

    try:
        asyncio.run(run(
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
