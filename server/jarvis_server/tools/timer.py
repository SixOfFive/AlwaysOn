"""Timer tool — schedule a deferred TTS notification.

The tool returns immediately with a confirmation. A background task
sleeps for the requested duration, then pushes a Say back to the
client via ActiveSession.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from jarvis_server.active_session import ActiveSession
from jarvis_server.tools import Tool

log = logging.getLogger(__name__)

_UNIT_SECONDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
}

# Extracts a duration like "5 minutes" / "30 seconds" / "2 hours".
DURATION_RE = re.compile(
    r"(\d+)\s*(seconds?|minutes?|hours?|secs?|mins?|hrs?)",
    re.IGNORECASE,
)


def parse_duration(text: str) -> int | None:
    """Pull a duration out of free text. Returns seconds, or None if
    nothing parseable is found."""
    m = DURATION_RE.search(text)
    if not m:
        return None
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


def _humanize(seconds: int) -> str:
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} hour" + ("s" if h != 1 else "")
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} minute" + ("s" if m != 1 else "")
    return f"{seconds} second" + ("s" if seconds != 1 else "")


async def _fire(seconds: int, label: str) -> None:
    await asyncio.sleep(seconds)
    log.info("timer fired: %s", label)
    await ActiveSession.push_say(f"Your {label} timer is up.")


async def _set_timer(args: dict[str, Any]) -> str:
    seconds = int(args.get("seconds", 0))
    if seconds <= 0:
        # Fallback: try to parse from a free-text "text" arg if the LLM
        # passed one instead of a numeric seconds.
        raw = str(args.get("text", "")).strip()
        parsed = parse_duration(raw)
        if parsed is None:
            return "I couldn't figure out how long that should be."
        seconds = parsed
    if seconds > 24 * 3600:
        return "That's longer than a day — I'll cap timers at 24 hours."

    label = _humanize(seconds)
    asyncio.create_task(_fire(seconds, label))
    log.info("scheduled timer: %s (%ds)", label, seconds)
    return f"OK, {label} timer started."


def timer_tools() -> list[Tool]:
    return [
        Tool(
            name="set_timer",
            description=(
                "Start a countdown timer. The assistant will speak when it "
                "fires. Use this for cooking, reminders to check things, "
                "Pomodoro-style intervals, etc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                        "description": "Duration in seconds.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Fallback: the raw utterance if you can't extract seconds.",
                    },
                },
                "additionalProperties": False,
            },
            handler=_set_timer,
        ),
    ]
