"""Builtin tools — answered locally without calling out to Claude or
external services."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jarvis_server.tools import Tool


async def _get_time(_args: dict[str, Any]) -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


async def _get_date(_args: dict[str, Any]) -> str:
    return datetime.now().strftime("%A, %B %d, %Y")


async def _get_datetime(_args: dict[str, Any]) -> str:
    return datetime.now().strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")


def builtin_tools() -> list[Tool]:
    no_args = {"type": "object", "properties": {}, "additionalProperties": False}
    return [
        Tool(
            name="get_time",
            description="Return the current local time (e.g. '3:47 PM').",
            input_schema=no_args,
            handler=_get_time,
        ),
        Tool(
            name="get_date",
            description="Return today's date (e.g. 'Tuesday, May 12, 2026').",
            input_schema=no_args,
            handler=_get_date,
        ),
        Tool(
            name="get_datetime",
            description="Return the current date and time together.",
            input_schema=no_args,
            handler=_get_datetime,
        ),
    ]
