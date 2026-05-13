"""Claude integration.

The router calls `ask()` with the user's transcript and the full tool
list. We loop on tool_use turns, dispatch each tool call to the
registered handler, and feed results back until Claude returns a final
text response. That text is what gets spoken.

System prompt is intentionally short and voice-aware — replies should be
spoken aloud, so no markdown, no bullet lists, just a sentence or two.

Prompt caching: the system prompt and tool definitions are stable across
calls, so we mark them with cache_control so subsequent turns get the
cache discount.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message

from jarvis_server.tools import ToolRegistry

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = (
    "You are Jarvis, a voice assistant. Replies will be spoken aloud by "
    "text-to-speech, so:\n"
    "- Answer in one or two short sentences. Conversational, not formal.\n"
    "- No markdown, no bullet points, no code blocks.\n"
    "- Spell out numbers and units the way a person would say them.\n"
    "- If you don't know, say so plainly. Don't invent facts.\n"
    "- When the user asks about their own past work, projects, or where they "
    "left off, use the vault_* tools — that is the source of truth for "
    "their history."
)


class ClaudeRouter:
    def __init__(
        self,
        registry: ToolRegistry,
        model: str = DEFAULT_MODEL,
        max_tool_iterations: int = 5,
    ) -> None:
        # api_key=None lets the SDK pull from env or fail loudly when called.
        self.client = AsyncAnthropic()
        self.registry = registry
        self.model = model
        self.max_iter = max_tool_iterations

    @classmethod
    def try_create(cls, registry: ToolRegistry, model: str) -> "ClaudeRouter | None":
        if not os.getenv("ANTHROPIC_API_KEY"):
            log.warning("ANTHROPIC_API_KEY not set — Claude fallback disabled, "
                        "only builtin and vault tools will work")
            return None
        return cls(registry, model=model)

    async def ask(self, user_text: str) -> str:
        tools = _with_cache(self.registry.as_claude_tools())
        system = [{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_text},
        ]

        for i in range(self.max_iter):
            resp: Message = await self.client.messages.create(
                model=self.model,
                max_tokens=512,
                system=system,
                tools=tools,
                messages=messages,
            )
            log.debug("claude turn %d: stop_reason=%s usage=%s",
                      i, resp.stop_reason, resp.usage)

            if resp.stop_reason != "tool_use":
                return _final_text(resp)

            # Echo assistant's tool-use turn back, then append tool_result for each.
            messages.append({"role": "assistant", "content": resp.content})
            results: list[dict[str, Any]] = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                tool = self.registry.get(block.name)
                if tool is None:
                    out = f"(unknown tool: {block.name})"
                else:
                    try:
                        out = await tool.handler(block.input or {})
                    except Exception as exc:  # noqa: BLE001
                        log.exception("tool %s failed", block.name)
                        out = f"(tool error: {exc})"
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })
            messages.append({"role": "user", "content": results})

        log.warning("hit tool-use iteration cap (%d)", self.max_iter)
        return "Sorry, I got stuck thinking about that."


def _final_text(resp: Message) -> str:
    return " ".join(
        block.text for block in resp.content if block.type == "text"
    ).strip() or "(no response)"


def _with_cache(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tag the last tool with cache_control so the whole tools array
    (which is stable across calls) gets cached."""
    if not tools:
        return tools
    tagged = [dict(t) for t in tools]
    tagged[-1]["cache_control"] = {"type": "ephemeral"}
    return tagged
