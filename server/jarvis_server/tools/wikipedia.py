"""Wikipedia summary lookup — for "who is X" / "what is X" style queries
that don't need the full power of Claude's general knowledge.

Uses the public REST endpoint:
    GET https://en.wikipedia.org/api/rest_v1/page/summary/<title>

Returns the lead paragraph (the model's "extract" field), trimmed for TTS.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import httpx

from jarvis_server.tools import Tool

log = logging.getLogger(__name__)

_BASE = "https://en.wikipedia.org/api/rest_v1/page/summary/"

# How many sentences of the extract to read aloud. Wikipedia extracts
# can be quite long; for TTS we want a snappy summary.
_MAX_SENTENCES = 2


def _trim(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    # Split on naive sentence boundary; good enough for the lead paragraph.
    parts: list[str] = []
    cur: list[str] = []
    for ch in text:
        cur.append(ch)
        if ch in ".!?" and len(cur) > 3:
            parts.append("".join(cur).strip())
            cur = []
            if len(parts) >= _MAX_SENTENCES:
                break
    if cur and len(parts) < _MAX_SENTENCES:
        parts.append("".join(cur).strip())
    return " ".join(parts)


async def _wiki(args: dict[str, Any]) -> str:
    topic = str(args.get("topic", "")).strip()
    if not topic:
        return "I need a topic to look up."

    title = urllib.parse.quote(topic.replace(" ", "_"))
    url = _BASE + title
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        log.warning("wikipedia GET failed: %s", exc)
        return f"Wikipedia lookup failed: {exc}"

    if r.status_code == 404:
        return f"I couldn't find a Wikipedia page for {topic}."
    if r.status_code != 200:
        return f"Wikipedia returned {r.status_code}."

    data = r.json()
    extract = data.get("extract") or ""
    if not extract:
        return f"Wikipedia has a page for {topic} but no summary text."
    return _trim(extract)


def wikipedia_tools() -> list[Tool]:
    return [
        Tool(
            name="wikipedia_summary",
            description=(
                "Look up a topic, person, or thing on Wikipedia and return "
                "a short spoken summary. Use for 'who is X', 'what is X', "
                "'tell me about X' style questions where general knowledge "
                "is enough — not for current events or anything Wikipedia "
                "wouldn't have."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The page title to look up (e.g. 'Nikola Tesla').",
                    },
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
            handler=_wiki,
        ),
    ]
