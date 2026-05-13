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
from jarvis_server.tools.timer import parse_duration

log = logging.getLogger(__name__)


# (compiled pattern, tool name) — first match wins.
_FAST_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(what\s*(?:'s| is)?\s*the\s+time|what\s+time\s+is\s+it)\b", re.I), "get_time"),
    (re.compile(r"\b(what\s*(?:'s| is)?\s*the\s+date|what\s+day\s+is\s+(?:it|today)|today's\s+date)\b", re.I), "get_date"),
    # Plain "what's the weather" → default location. Claude still gets
    # invoked for "weather in Tokyo" type queries that need an argument.
    (re.compile(r"\bwhat\s*(?:'s| is)?\s*the\s+weather\b(?!\s+(?:in|at|for)\b)", re.I), "get_weather"),
    (re.compile(r"\bweather\s*(?:like|report|outside)?\s*\??$", re.I), "get_weather"),
]

# Tools that pull an argument out of the utterance. Matched as a second
# pass; the captured group is passed to the tool handler.
_FAST_WITH_ARG: list[tuple[re.Pattern[str], str, str]] = [
    # "note: pick up bread" / "make a note that ..." / "remind me to ..."
    (re.compile(r"\b(?:note|remind\s+me)\s*(?:that|to|:)?\s*(.+)", re.I), "append_note", "text"),
    # "wake bigiron" / "boot the nuc" / "turn on tower"
    (re.compile(r"\b(?:wake|boot|turn\s+on)\s+(?:the\s+|my\s+)?(\w+)\b", re.I), "wake_on_lan", "host"),
]


class Router:
    def __init__(self, registry: ToolRegistry, claude: ClaudeRouter | None) -> None:
        self.registry = registry
        self.claude = claude

    async def handle(self, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            return "I didn't catch that."

        # Fast path: zero-arg tools.
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

        # "set a 5 minute timer" / "30 second timer" / "start a timer for 2 hours"
        if re.search(r"\btimer\b", text, re.I) or re.search(r"\bset\s+(?:a\s+)?\d+", text, re.I):
            seconds = parse_duration(text)
            if seconds is not None:
                tool = self.registry.get("set_timer")
                if tool is not None:
                    log.info("fast path: set_timer (%ds)", seconds)
                    try:
                        return await tool.handler({"seconds": seconds})
                    except Exception as exc:  # noqa: BLE001
                        log.exception("fast-path set_timer failed")
                        return f"Something went wrong: {exc}"

        # Fast path: single-arg tools, with the captured group as the arg.
        for pattern, tool_name, arg_key in _FAST_WITH_ARG:
            m = pattern.search(text)
            if m:
                tool = self.registry.get(tool_name)
                if tool is not None:
                    log.info("fast path: %s(%s=%r)", tool_name, arg_key, m.group(1))
                    try:
                        return await tool.handler({arg_key: m.group(1).strip()})
                    except Exception as exc:  # noqa: BLE001
                        log.exception("fast-path tool %s failed", tool_name)
                        return f"Something went wrong: {exc}"

        # Otherwise let Claude figure it out, with full tool access.
        if self.claude is None:
            return ("I only know the time and date right now. "
                    "Set the Anthropic API key to enable more.")
        return await self.claude.ask(text)
