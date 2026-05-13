"""FastAPI application — wires together STT, tools, Claude, and the
WebSocket endpoint. All heavy components load once at startup."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from jarvis_server.claude import ClaudeRouter
from jarvis_server.config import Config
from jarvis_server.router import Router
from jarvis_server.session import Session
from jarvis_server.stt import STT
from jarvis_server.tools import ToolRegistry
from jarvis_server.tools.builtin import builtin_tools
from jarvis_server.tools.notes import notes_tools
from jarvis_server.tools.search import search_tools
from jarvis_server.tools.timer import timer_tools
from jarvis_server.tools.vault import VaultClient, vault_tools
from jarvis_server.tools.weather import weather_tools
from jarvis_server.tools.wikipedia import wikipedia_tools
from jarvis_server.tools.wol import wol_tools

log = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("startup")
        app.state.stt = STT(
            model_name=cfg.stt_model,
            device=cfg.stt_device,
            compute_type=cfg.stt_compute_type,
        )

        registry = ToolRegistry()
        for tool in builtin_tools():
            registry.register(tool)
        for tool in weather_tools():
            registry.register(tool)
        for tool in timer_tools():
            registry.register(tool)
        for tool in notes_tools():
            registry.register(tool)
        for tool in wikipedia_tools():
            registry.register(tool)
        for tool in wol_tools():
            registry.register(tool)
        for tool in search_tools():
            registry.register(tool)

        vault: VaultClient | None = None
        if cfg.vault_disabled:
            log.info("vault MCP disabled by config")
        else:
            vault = VaultClient(cfg.vault_command, cfg.vault_args)
            try:
                await vault.start()
                for tool in vault_tools(vault):
                    registry.register(tool)
            except BaseException as exc:
                # Catch BaseException so a CancelledError or ExceptionGroup
                # raised by the MCP TaskGroup doesn't tank the whole server.
                log.warning("vault MCP failed to start (%s: %s); continuing without it",
                            type(exc).__name__, exc)
                try:
                    await vault.close()
                except BaseException:
                    pass
                vault = None

        claude = ClaudeRouter.try_create(registry, model=cfg.claude_model)
        app.state.router = Router(registry, claude)
        app.state.client_count = 0

        try:
            yield
        finally:
            log.info("shutdown")
            if vault is not None:
                await vault.close()

    app = FastAPI(title="jarvis-server", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "clients": getattr(app.state, "client_count", 0)}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        app.state.client_count += 1
        try:
            session = Session(
                websocket,
                stt=app.state.stt,
                router=app.state.router,
            )
            await session.run()
        except Exception as exc:  # noqa: BLE001
            log.exception("session crashed: %s", exc)
        finally:
            app.state.client_count -= 1

    return app
