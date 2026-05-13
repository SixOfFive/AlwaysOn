"""Weather tool — uses wttr.in (no API key, no account).

wttr.in auto-detects location from IP; pass `location` to override.
Output is shaped for TTS — no emoji, no arrows, no degree symbol.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

import httpx

from jarvis_server.tools import Tool

log = logging.getLogger(__name__)

# wttr.in format codes: %l location, %C condition text, %t temp,
# %f feels-like, %w wind, %h humidity.
_FORMAT = "%l: %C, %t (feels like %f), wind %w, humidity %h"

# wttr.in encodes wind direction as a Unicode arrow that TTS botches.
# It points in the direction the wind is moving toward; we render as
# the cardinal it's coming *from*, which is how weather is normally spoken.
_WIND_ARROWS = {
    "↑": "south",      # ↑ moving north → from south
    "↓": "north",      # ↓ from north
    "←": "east",       # ← from east
    "→": "west",       # → from west
    "↖": "southeast",  # ↖ from SE
    "↗": "southwest",  # ↗ from SW
    "↘": "northwest",  # ↘ from NW
    "↙": "northeast",  # ↙ from NE
}


def _voicify(text: str) -> str:
    for arrow, word in _WIND_ARROWS.items():
        text = text.replace(arrow, word + " ")
    text = text.replace("°C", " degrees Celsius")
    text = text.replace("°F", " degrees Fahrenheit")
    text = text.replace("°", " degrees")
    text = text.replace("%", " percent")
    return re.sub(r"\s+", " ", text).strip()


async def _get_weather(args: dict[str, Any]) -> str:
    loc = str(args.get("location", "")).strip()
    path = urllib.parse.quote(loc) if loc else ""
    url = f"https://wttr.in/{path}"
    params = {"format": _FORMAT}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            text = r.text.strip()
    except httpx.HTTPError as exc:
        log.warning("wttr.in failed: %s", exc)
        return f"Weather lookup failed: {exc}"

    return _voicify(text.replace("\n", " "))


def weather_tools() -> list[Tool]:
    return [
        Tool(
            name="get_weather",
            description=(
                "Get the current weather. Returns a one-sentence summary "
                "with condition, temperature, feels-like, wind, humidity. "
                "Location optional — defaults to the user's IP-detected city."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name, e.g. 'Boston' or 'Tokyo'. Omit for auto-detect.",
                    },
                },
                "additionalProperties": False,
            },
            handler=_get_weather,
        ),
    ]
