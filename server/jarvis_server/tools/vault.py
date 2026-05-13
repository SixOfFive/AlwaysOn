"""Vault tools — talk to the user's obsidian-vault MCP server.

The MCP server is started as a subprocess over stdio and kept alive for
the jarvis-server lifetime. The command and args come from config so
the user can repoint it without changing code.

Exposed tools (v1):
- vault_list_recent — what was last touched (good for "where did I leave off")
- vault_semantic_search — concept-flavored lookup
- vault_read_topic — fetch a note's full body once it's been surfaced
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jarvis_server.tools import Tool

log = logging.getLogger(__name__)


class VaultClient:
    """Long-lived MCP client over stdio to the obsidian-vault server."""

    def __init__(self, command: str, args: list[str]) -> None:
        self._params = StdioServerParameters(command=command, args=args)
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        log.info("starting obsidian-vault MCP server: %s %s",
                 self._params.command, " ".join(self._params.args))
        read, write = await self._stack.enter_async_context(stdio_client(self._params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        tools = await self._session.list_tools()
        log.info("vault MCP connected; %d tools available", len(tools.tools))

    async def close(self) -> None:
        await self._stack.aclose()
        self._session = None

    async def call(self, tool_name: str, args: dict[str, Any]) -> str:
        if self._session is None:
            return "(vault MCP not connected)"
        async with self._lock:
            try:
                result = await self._session.call_tool(tool_name, args)
            except Exception as exc:  # noqa: BLE001
                log.warning("vault call %s failed: %s", tool_name, exc)
                return f"(vault error: {exc})"

        chunks: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                chunks.append(text)
        return "\n".join(chunks) if chunks else "(no result)"


def vault_tools(client: VaultClient) -> list[Tool]:
    """Wrap the vault client's calls as router-visible Tools."""

    async def list_recent(args: dict[str, Any]) -> str:
        limit = int(args.get("limit", 5))
        return await client.call("list_recent_topics", {"limit": limit})

    async def semantic_search(args: dict[str, Any]) -> str:
        query = str(args["query"])
        limit = int(args.get("limit", 5))
        return await client.call("semantic_search", {"query": query, "limit": limit})

    async def read_topic(args: dict[str, Any]) -> str:
        name = str(args["name"])
        return await client.call("read_topic", {"name": name})

    return [
        Tool(
            name="vault_list_recent",
            description=(
                "List the most recently updated topics in the user's Obsidian "
                "vault. Use when the user asks where they left off, what's new, "
                "or what was recently worked on."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            handler=list_recent,
        ),
        Tool(
            name="vault_semantic_search",
            description=(
                "Semantic search across the user's Obsidian vault for a concept "
                "or question. Returns the most relevant Topic and Decision notes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=semantic_search,
        ),
        Tool(
            name="vault_read_topic",
            description=(
                "Fetch the full body of a specific Topic or Decision note by "
                "name. Use after a search surfaces a relevant title."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=read_topic,
        ),
    ]
