r"""Server configuration. Env-first; defaults are sensible for this rig.

Override via env vars at launch:
    JARVIS_STT_MODEL          (default: small.en)
    JARVIS_STT_DEVICE         (default: cpu)
    JARVIS_STT_COMPUTE_TYPE   (default: int8)
    JARVIS_CLAUDE_MODEL       (default: claude-haiku-4-5-20251001)
    JARVIS_VAULT_CMD          (default: python)
    JARVIS_VAULT_ARGS         (default: <user>\.claude\scripts\vault-server.py)
    JARVIS_VAULT_DISABLED     (set to "1" to skip the vault MCP entirely)
    ANTHROPIC_API_KEY         (required for Claude fallback; optional otherwise)
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path


def _default_vault_args() -> list[str]:
    # User's known vault MCP server location.
    return [str(Path.home() / ".claude" / "scripts" / "vault-server.py")]


@dataclass(slots=True)
class Config:
    stt_model: str = field(default_factory=lambda: os.getenv("JARVIS_STT_MODEL", "small.en"))
    stt_device: str = field(default_factory=lambda: os.getenv("JARVIS_STT_DEVICE", "cpu"))
    stt_compute_type: str = field(default_factory=lambda: os.getenv("JARVIS_STT_COMPUTE_TYPE", "int8"))
    claude_model: str = field(default_factory=lambda: os.getenv("JARVIS_CLAUDE_MODEL", "claude-haiku-4-5-20251001"))
    vault_command: str = field(default_factory=lambda: os.getenv("JARVIS_VAULT_CMD", "python"))
    vault_disabled: bool = field(default_factory=lambda: os.getenv("JARVIS_VAULT_DISABLED") == "1")

    vault_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        raw = os.getenv("JARVIS_VAULT_ARGS")
        if raw:
            self.vault_args = shlex.split(raw, posix=False)
        elif not self.vault_args:
            self.vault_args = _default_vault_args()
