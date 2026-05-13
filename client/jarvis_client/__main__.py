"""Entry point: `python -m jarvis_client`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket

from jarvis_client.main import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-client")
    parser.add_argument("--server", default="ws://127.0.0.1:7333/ws")
    parser.add_argument("--id", dest="client_id", default=f"{socket.gethostname()}-mic")
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--no-synthetic",
        action="store_true",
        help="Skip the synthetic-utterance smoke test on connect.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [client] %(levelname)s %(name)s — %(message)s",
    )

    try:
        asyncio.run(run(
            server_url=args.server,
            client_id=args.client_id,
            synthetic=not args.no_synthetic,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
