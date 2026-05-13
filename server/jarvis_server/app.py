"""FastAPI application — wires together STT, tools, Claude, and the
WebSocket endpoint. All heavy components load once at startup."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from jarvis_server import banlist
from jarvis_server.catalog import load_catalog, pick_model
from jarvis_server.config import Config
from jarvis_server.ollama_router import OllamaRouter, ensure_pulled
from jarvis_server.router import Router
from jarvis_server.session import Session
from jarvis_server.stt import STT
from jarvis_server.tools import ToolRegistry
from jarvis_server.tools.builtin import builtin_tools
from jarvis_server.tools.notes import notes_tools
from jarvis_server.tools.save_code import save_code_tools
from jarvis_server.tools.search import search_tools
from jarvis_server.tools.timer import timer_tools
from jarvis_server.tools.vault import VaultClient, vault_tools
from jarvis_server.tools.weather import weather_tools
from jarvis_server.tools.wikipedia import wikipedia_tools
from jarvis_server.tools.wol import wol_tools

log = logging.getLogger(__name__)


async def _build_llm_router(
    cfg: Config,
    registry,  # ToolRegistry, but avoid circular type hint
) -> OllamaRouter | None:
    """Pick a model from the catalog (or honor JARVIS_OLLAMA_MODEL),
    make sure Ollama has it, and return a configured OllamaRouter.

    Returns None if no suitable model is available and Ollama can't be
    reached at all — the server still works for fast-path tools in that
    case.
    """
    model_tag: str | None = cfg.ollama_model_override or None
    selection_reason = "JARVIS_OLLAMA_MODEL override"

    if model_tag is None:
        try:
            catalog = await load_catalog(cfg.catalog_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("catalog load failed (%s); LLM disabled", exc)
            return None
        banned = banlist.read()
        if banned:
            log.info("banlist active: skipping %d model(s) — %s",
                     len(banned), ", ".join(sorted(banned)))
        choice = pick_model(
            catalog,
            vram_budget_gb=cfg.ollama_vram_budget_gb,
            min_context=cfg.ollama_context_length,
            preferred_server=cfg.ollama_server_name,
            banned=banned,
        )
        if choice is None:
            log.warning(
                "no catalog model fits constraints (vram<=%.0fGB, ctx>=%d, tools, "
                "not in banlist); LLM disabled",
                cfg.ollama_vram_budget_gb, cfg.ollama_context_length,
            )
            return None
        log.info("model pick: %s — %s", choice.tag, choice.reason)
        model_tag = choice.tag
        selection_reason = choice.reason

    try:
        await ensure_pulled(cfg.ollama_url, model_tag)
    except Exception as exc:  # noqa: BLE001
        log.warning("ollama unreachable or pull failed (%s); LLM disabled", exc)
        return None

    log.info("LLM router ready: model=%s ctx=%d (%s)",
             model_tag, cfg.ollama_context_length, selection_reason)
    return OllamaRouter(
        registry,
        model=model_tag,
        base_url=cfg.ollama_url,
        context_length=cfg.ollama_context_length,
    )


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.load()

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
        for tool in save_code_tools():
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

        llm_router = await _build_llm_router(cfg, registry)
        app.state.llm_router = llm_router
        app.state.router = Router(registry, llm_router)
        app.state.client_count = 0

        try:
            yield
        finally:
            log.info("shutdown")
            if llm_router is not None:
                await llm_router.aclose()
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
                idle_reset_sec=cfg.idle_reset_sec,
                trigger_phrase=cfg.trigger_phrase,
            )
            await session.run()
        except Exception as exc:  # noqa: BLE001
            log.exception("session crashed: %s", exc)
        finally:
            app.state.client_count -= 1

    return app
