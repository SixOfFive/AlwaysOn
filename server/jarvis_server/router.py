"""Intent router.

Cheap fast path for the obvious commands (time, date, etc.) — they don't
need the LLM. Anything else falls through to the local Ollama-backed
router, which has access to the same tools plus the vault tools.

Keeping the fast path is mostly a latency win on trivial cases and lets
the assistant work even when Ollama is down — everything still works if
you delete it (assuming the LLM is up).
"""

from __future__ import annotations

import logging
import re

from jarvis_server.conversation import Conversation
from jarvis_server.ollama_router import OllamaRouter
from jarvis_server.tools import ToolRegistry
from jarvis_server.tools.search import top_snippet
from jarvis_server.tools.timer import parse_duration

log = logging.getLogger(__name__)


# (compiled pattern, tool name) — first match wins.
_FAST_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(what\s*(?:'s| is)?\s*the\s+time|what\s+time\s+is\s+it)\b", re.I), "get_time"),
    (re.compile(r"\b(what\s*(?:'s| is)?\s*the\s+date|what\s+day\s+is\s+(?:it|today)|today's\s+date)\b", re.I), "get_date"),
    # Plain "what's the weather" / "weather?" with nothing meaningful
    # after it → default location. Anchor to end-of-string so things
    # like "weather there", "weather today", "weather in Tokyo", etc
    # fall through to the LLM where conversation context can resolve
    # the implicit location.
    (re.compile(r"\bwhat\s*(?:'s| is)?\s*the\s+weather\s*[?.!,]?\s*$", re.I), "get_weather"),
    (re.compile(r"^\s*weather\s*[?.!,]?\s*$", re.I), "get_weather"),
]

# Tools that pull an argument out of the utterance. Matched as a second
# pass; the captured group is passed to the tool handler.
_FAST_WITH_ARG: list[tuple[re.Pattern[str], str, str]] = [
    # "note: pick up bread" / "make a note that ..." / "remind me to ..."
    (re.compile(r"\b(?:note|remind\s+me)\s*(?:that|to|:)?\s*(.+)", re.I), "append_note", "text"),
    # "wake desktop" / "boot server" / "turn on tower" — the captured
    # word is looked up against the WoL hosts map from jarvis.toml.
    (re.compile(r"\b(?:wake|boot|turn\s+on)\s+(?:the\s+|my\s+)?(\w+)\b", re.I), "wake_on_lan", "host"),
]


class Router:
    def __init__(self, registry: ToolRegistry, llm: OllamaRouter | None) -> None:
        self.registry = registry
        self.llm = llm

    async def handle(self, transcript: str, conversation: Conversation) -> str:
        """Route a transcript and return the spoken reply. The session
        has already appended the user turn to `conversation`; this method
        is responsible for appending the assistant turn(s) it produces.
        Fast-path replies record a single assistant text turn. The LLM
        path delegates that bookkeeping to OllamaRouter."""
        text = transcript.strip()
        if not text:
            reply = "I didn't catch that."
            conversation.add_assistant_text(reply)
            return reply

        # Fast path: zero-arg tools.
        for pattern, tool_name in _FAST_PATTERNS:
            if pattern.search(text):
                tool = self.registry.get(tool_name)
                if tool is not None:
                    log.info("fast path: %s", tool_name)
                    try:
                        reply = await tool.handler({})
                    except Exception as exc:  # noqa: BLE001
                        log.exception("fast-path tool %s failed", tool_name)
                        reply = f"Something went wrong: {exc}"
                    conversation.add_assistant_text(reply)
                    return reply

        # "set a 5 minute timer" / "30 second timer" / "start a timer for 2 hours"
        if re.search(r"\btimer\b", text, re.I) or re.search(r"\bset\s+(?:a\s+)?\d+", text, re.I):
            seconds = parse_duration(text)
            if seconds is not None:
                tool = self.registry.get("set_timer")
                if tool is not None:
                    log.info("fast path: set_timer (%ds)", seconds)
                    try:
                        reply = await tool.handler({"seconds": seconds})
                    except Exception as exc:  # noqa: BLE001
                        log.exception("fast-path set_timer failed")
                        reply = f"Something went wrong: {exc}"
                    conversation.add_assistant_text(reply)
                    return reply

        # "search the web for X" / "search for X" / "look up X" — top
        # DDG snippet, fast and direct. The LLM still gets web_search as
        # a tool for multi-step answers.
        m = re.search(
            r"\b(?:search\s+(?:the\s+web\s+)?(?:for\s+)?|look\s+up\s+|google\s+|ddg\s+)(.+)",
            text,
            re.I,
        )
        if m:
            query = m.group(1).strip(" .?!,")
            if query:
                log.info("fast path: web_search snippet for %r", query)
                try:
                    reply = await top_snippet(query)
                except Exception as exc:  # noqa: BLE001
                    log.exception("fast-path search failed")
                    reply = f"Search failed: {exc}"
                conversation.add_assistant_text(reply)
                return reply

        # Fast path: single-arg tools, with the captured group as the arg.
        for pattern, tool_name, arg_key in _FAST_WITH_ARG:
            m = pattern.search(text)
            if m:
                tool = self.registry.get(tool_name)
                if tool is not None:
                    log.info("fast path: %s(%s=%r)", tool_name, arg_key, m.group(1))
                    try:
                        reply = await tool.handler({arg_key: m.group(1).strip()})
                    except Exception as exc:  # noqa: BLE001
                        log.exception("fast-path tool %s failed", tool_name)
                        reply = f"Something went wrong: {exc}"
                    conversation.add_assistant_text(reply)
                    return reply

        # Otherwise let the local LLM figure it out, with full tool access.
        if self.llm is None:
            reply = ("I only know the time and date right now. "
                     "Start Ollama on the LAN to enable more.")
            conversation.add_assistant_text(reply)
            return reply
        return await self.llm.ask(text, conversation)
