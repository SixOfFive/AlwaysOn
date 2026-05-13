"""Persistent list of Ollama model tags to skip when the catalog picks.

When the runtime determines that a picked model is unusable on this
hardware (currently: chat call timed out, which usually means the model
overflowed VRAM and is running partially on CPU), it appends the tag
here. On the next startup, `pick_model` filters these out and the
catalog's next-best candidate gets selected automatically.

The file is gitignored — banlists are per-deployment, not shared.

File format: plain text, one Ollama tag per line. Blank lines and lines
starting with `#` are ignored.

Path resolution:
  1. $JARVIS_BANLIST (explicit override)
  2. <repo root>/model-banlist.txt
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _path() -> Path:
    env = os.getenv("JARVIS_BANLIST")
    if env:
        return Path(env).expanduser()
    # <package>/.. /.. = repo root (server/jarvis_server → server → repo)
    return Path(__file__).resolve().parent.parent.parent / "model-banlist.txt"


def read() -> set[str]:
    """Return the set of banned Ollama tags. Missing file = empty set.

    Strips inline `# comment` trailers from each line — those are notes
    the user (or this module) leaves about why a tag was banned, not
    part of the tag itself.
    """
    p = _path()
    if not p.is_file():
        return set()
    out: set[str] = set()
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Drop inline trailing comments: "gemma4:e4b  # spilled to CPU"
            # → "gemma4:e4b". Ollama tags can't contain `#`, so this is
            # always safe.
            if "#" in line:
                line = line.split("#", 1)[0].strip()
            if line:
                out.add(line)
    except OSError as exc:
        log.warning("banlist %s unreadable: %s", p, exc)
    return out


def add(tag: str, *, reason: str = "") -> bool:
    """Append `tag` to the banlist if not already present. Returns True
    if a new entry was written. Creates the file (with a header comment)
    on first write so the user can find it."""
    tag = tag.strip()
    if not tag:
        return False
    current = read()
    if tag in current:
        return False

    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        is_new = not p.is_file()
        with p.open("a", encoding="utf-8") as f:
            if is_new:
                f.write(
                    "# Ollama tags the catalog picker should skip on this host.\n"
                    "# One tag per line. Lines starting with `#` are comments.\n"
                    "# Auto-appended on chat timeouts; safe to hand-edit.\n"
                )
            line = tag
            if reason:
                line += f"  # {reason}"
            f.write(line + "\n")
    except OSError as exc:
        log.warning("banlist write to %s failed: %s", p, exc)
        return False

    log.warning("banned %s (reason: %s) — restart server to pick a new model", tag, reason or "—")
    return True
