"""FastAPI application. Today: one WebSocket endpoint and a health check.
Later: HTTP routes for serving pre-rendered TTS audio."""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket

from jarvis_server.session import Session

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="jarvis-server")
    client_count = {"n": 0}

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "clients": client_count["n"]}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        client_count["n"] += 1
        try:
            session = Session(websocket)
            await session.run()
        except Exception as exc:  # noqa: BLE001 — final-stop logging
            log.exception("session crashed: %s", exc)
        finally:
            client_count["n"] -= 1

    return app
