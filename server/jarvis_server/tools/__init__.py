"""Tools the router can dispatch to. Each tool exposes:

- name: stable identifier (also used as the Claude tool name)
- description: one-line summary for the LLM
- input_schema: JSON Schema for arguments
- handler: async callable that takes the parsed args and returns a string

The Tool dataclass keeps these together so we can register, route, and
hand them to Claude tool-use without duplication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Handler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def as_ollama_tool(self) -> dict[str, Any]:
        """OpenAI-compatible function-tool shape — what Ollama's /api/chat
        and /v1/chat/completions both accept."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def as_ollama_tools(self) -> list[dict[str, Any]]:
        return [t.as_ollama_tool() for t in self._tools.values()]
