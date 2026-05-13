"""Daily transcript log.

Every server-side STT result lands here as a timestamped bullet,
regardless of whether the wake-word trigger fired. Commands get a
`[cmd]` marker so the file is grep-friendly for actual interactions
vs ambient chat.

Path: $JARVIS_DICTATION_DIR/YYYY-MM-DD.md, defaulting to
~/jarvis-dictation. Point at your Obsidian vault if you want the
logs there.

Append-only and synchronous-via-thread (asyncio.to_thread). Append
mode on POSIX/Windows is atomic for small writes so concurrent
sessions don't corrupt the file.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _dir() -> Path:
    raw = os.getenv("JARVIS_DICTATION_DIR")
    return Path(raw).expanduser() if raw else (Path.home() / "jarvis-dictation")


def _write_line(line: str) -> None:
    d = _dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("can't create dictation dir %s: %s", d, exc)
        return
    file = d / f"{datetime.now():%Y-%m-%d}.md"
    try:
        with file.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        log.warning("dictation write to %s failed: %s", file, exc)


async def log_utterance(text: str, *, is_command: bool) -> None:
    """Append one transcript bullet to today's dictation file. Safe to
    await from the session loop — file I/O runs on a worker thread."""
    text = text.strip()
    if not text:
        return
    now = datetime.now()
    tag = "`[cmd]` " if is_command else ""
    line = f"- {now:%H:%M:%S} {tag}{text}\n"
    await asyncio.to_thread(_write_line, line)
