"""Web search via DuckDuckGo.

`duckduckgo-search` wraps DDG's HTML / Lite endpoints. No API key, no
account. Returns title + snippet + URL for each result. We return a
short formatted block — when Claude calls this tool it synthesizes a
spoken answer; on the router fast path we just read the first result's
snippet.

If DDG starts rate-limiting or HTML-shape-changes, this tool will fail
gracefully ("search unavailable") rather than crash the router.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jarvis_server.tools import Tool

log = logging.getLogger(__name__)

# Default to a few results so Claude has something to synthesize from.
_DEFAULT_K = 3
_MAX_K = 5


def _run_search(query: str, k: int) -> list[dict[str, str]]:
    # Import lazily so import failures don't break the rest of the server.
    # The package was renamed from duckduckgo-search to ddgs.
    try:
        from ddgs import DDGS  # type: ignore[import-not-found]
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore[import-not-found]

    out: list[dict[str, str]] = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=k, safesearch="moderate"):
            out.append({
                "title": str(r.get("title", "")).strip(),
                "body":  str(r.get("body", "")).strip(),
                "href":  str(r.get("href", "")).strip(),
            })
            if len(out) >= k:
                break
    return out


def _format(results: list[dict[str, str]]) -> str:
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, start=1):
        title = r["title"]
        body = r["body"]
        lines.append(f"{i}. {title}: {body}")
    return "\n".join(lines)


async def _web_search(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "I need a search query."
    k = max(1, min(_MAX_K, int(args.get("max_results", _DEFAULT_K))))
    try:
        # DDGS uses sync HTTP; run off the event loop.
        results = await asyncio.to_thread(_run_search, query, k)
    except Exception as exc:  # noqa: BLE001 — third-party scraper, anything goes
        log.warning("web_search failed: %s", exc)
        return f"Web search failed: {exc}"
    return _format(results)


def search_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=(
                "Search the web (DuckDuckGo). Returns the top result titles "
                "and snippets. Use when the user wants current information "
                "or anything not in your training data — news, recent "
                "events, product specs, error messages, etc. Synthesize a "
                "short spoken answer from the results rather than reading "
                "them verbatim."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for. Plain English query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "default": 3,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_web_search,
        ),
    ]


# Convenience helper for the router fast-path: get just the top snippet,
# formatted for direct TTS playback.
async def top_snippet(query: str) -> str:
    try:
        results = await asyncio.to_thread(_run_search, query, 1)
    except Exception as exc:  # noqa: BLE001
        log.warning("web_search (fast path) failed: %s", exc)
        return f"Web search failed: {exc}"
    if not results:
        return "I didn't find anything useful."
    r = results[0]
    body = r["body"] or r["title"]
    return body
