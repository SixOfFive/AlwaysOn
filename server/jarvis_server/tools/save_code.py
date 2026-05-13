"""save_code — Claude generates code, this tool writes it to disk.

Voice-driven coding: user says "write a python script that does X",
Claude composes the script and calls save_code(filename, content).
The file lands in a configurable workspace directory.

Path safety: filenames are sanitized to stay inside the workspace —
no absolute paths, no parent-traversal, no slashes. Suffix is added
based on language if missing.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis_server.tools import Tool

log = logging.getLogger(__name__)

_DEFAULT_WORKSPACE = Path.home() / "jarvis-workspace"

# Map common language names to file extensions.
_EXT: dict[str, str] = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "bash": ".sh", "shell": ".sh", "sh": ".sh",
    "powershell": ".ps1", "ps1": ".ps1",
    "kotlin": ".kt", "kt": ".kt",
    "java": ".java",
    "rust": ".rs", "rs": ".rs",
    "go": ".go", "golang": ".go",
    "c": ".c",
    "cpp": ".cpp", "c++": ".cpp",
    "html": ".html",
    "css": ".css",
    "json": ".json",
    "yaml": ".yaml", "yml": ".yaml",
    "toml": ".toml",
    "markdown": ".md", "md": ".md",
    "text": ".txt", "plain": ".txt", "txt": ".txt",
    "sql": ".sql",
    "ruby": ".rb", "rb": ".rb",
    "php": ".php",
}

_FILENAME_OK = re.compile(r"^[A-Za-z0-9._\-]+$")


def _workspace() -> Path:
    raw = os.getenv("JARVIS_WORKSPACE")
    return Path(raw) if raw else _DEFAULT_WORKSPACE


def _sanitize(filename: str, language: str | None) -> str:
    """Reject anything fancy, fall back to a timestamped name."""
    base = os.path.basename(filename.strip()) if filename else ""
    base = base.replace(" ", "_")
    if not base or not _FILENAME_OK.match(base):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"jarvis-{ts}"
    # Add suffix if missing
    if "." not in base and language:
        ext = _EXT.get(language.lower().strip())
        if ext:
            base += ext
    return base


async def _save_code(args: dict[str, Any]) -> str:
    content = str(args.get("content", ""))
    if not content.strip():
        return "Nothing to save — the code was empty."

    filename = _sanitize(
        filename=str(args.get("filename", "")),
        language=str(args.get("language", "")) or None,
    )
    workspace = _workspace()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("can't create workspace %s: %s", workspace, exc)
        return f"Couldn't create workspace: {exc}"

    target = workspace / filename
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        log.warning("write to %s failed: %s", target, exc)
        return f"Couldn't save the file: {exc}"

    log.info("saved %d bytes to %s", len(content), target)
    return f"Saved as {filename} in {workspace}."


def save_code_tools() -> list[Tool]:
    return [
        Tool(
            name="save_code",
            description=(
                "Save generated code or text to a file in the user's "
                "jarvis-workspace directory. Use after composing a "
                "complete program the user asked for. Pick a short "
                "filename without spaces; the suffix is added from the "
                "language argument if missing. Do not include markdown "
                "code fences in the content — write the raw file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Base name only (no slashes, no '..'). "
                            "Suffix optional — will be inferred from language.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The raw file contents.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Programming language for suffix inference. "
                            "Optional if filename includes the extension already.",
                    },
                },
                "required": ["filename", "content"],
                "additionalProperties": False,
            },
            handler=_save_code,
        ),
    ]
