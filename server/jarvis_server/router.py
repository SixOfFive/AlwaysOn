"""Intent router.

Cheap fast path for the obvious commands (time, date, etc.) — they don't
need Claude. Anything else falls through to the Claude client, which has
access to the same tools plus the vault tools.

Keeping the fast path is mostly a latency win on the trivial cases and
saves a few tokens; everything still works if you delete it.
"""

from __future__ import annotations

import logging
import re

from jarvis_server.claude import ClaudeRouter
from jarvis_server.tools import ToolRegistry

log = logging.getLogger(__name__)


# (compiled pattern, tool name) — first match wins.
_FAST_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(what\s*(?:'s| is)?\s*the\s+time|what\s+time\s+is\s+it)\b", re.I), "get_time"),
    (re.compile(r"\b(what\s*(?:'s| is)?\s*the\s+date|what\s+day\s+is\s+(?:it|today)|today's\s+date)\b", re.I), "get_date"),
]


class Router:
    def __init__(self, registry: ToolRegistry, claude: ClaudeRouter | None) -> None:
        self.registry = registry
        self.claude = claude

    async def handle(self, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            return "I didn't catch that."

        # Fast path: a builtin pattern matched outright.
        for pattern, tool_name in _FAST_PATTERNS:
            if pattern.search(text):
                tool = self.registry.get(tool_name)
                if tool is not None:
                    log.info("fast path: %s", tool_name)
                    try:
                        return await tool.handler({})
                    except Exception as exc:  # noqa: BLE001
                        log.exception("fast-path tool %s failed", tool_name)
                        return f"Something went wrong: {exc}"

        # Otherwise let Claude figure it out, with full tool access.
        if self.claude is None:
            return ("I only know the time and date right now. "
                    "Set the Anthropic API key to enable more.")
        return await self.claude.ask(text)
