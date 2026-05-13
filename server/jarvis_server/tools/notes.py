"""Append a quick note to a daily file.

Path: $JARVIS_NOTES_DIR/jarvis-YYYY-MM-DD.md, defaulting to
~/jarvis-notes. Point this at an Obsidian Inbox if you want notes to
land in your vault. Each note is one bullet with a timestamp.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis_server.tools import Tool

log = logging.getLogger(__name__)


def _notes_dir() -> Path:
    raw = os.getenv("JARVIS_NOTES_DIR")
    return Path(raw).expanduser() if raw else (Path.home() / "jarvis-notes")


async def _append_note(args: dict[str, Any]) -> str:
    text = str(args.get("text", "")).strip()
    if not text:
        return "I didn't catch what to note down."

    notes_dir = _notes_dir()
    try:
        notes_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("can't create %s: %s", notes_dir, exc)
        return f"Couldn't save the note: {exc}"

    now = datetime.now()
    file = notes_dir / f"jarvis-{now:%Y-%m-%d}.md"
    line = f"- {now:%H:%M}  {text}\n"

    try:
        with file.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        log.warning("write to %s failed: %s", file, exc)
        return f"Couldn't save the note: {exc}"

    log.info("note saved to %s: %r", file, text)
    return "Got it."


def notes_tools() -> list[Tool]:
    return [
        Tool(
            name="append_note",
            description=(
                "Append a short note to today's Jarvis notes file. Use "
                "for things the user wants to remember or come back to "
                "later — quick ideas, to-do items, reminders. The user "
                "already trusts where this lands; just save the text "
                "and confirm briefly."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The exact text to save as a note.",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_append_note,
        ),
    ]
