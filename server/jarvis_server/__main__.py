"""Entry point: `python -m jarvis_server`."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from jarvis_server.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7333)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [server] %(levelname)s %(name)s — %(message)s",
    )

    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
