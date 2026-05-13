r"""Server configuration.

Loaded from a TOML file plus environment-variable overrides. Code
defaults are intentionally generic so the repo can ship without leaking
anyone's personal paths or IPs — put your real values in `jarvis.toml`
(gitignored) or in JARVIS_* env vars.

Loading order, first match wins:
  1. $JARVIS_CONFIG (explicit override; full path)
  2. ./jarvis.toml (in current working directory)
  3. <repo root>/jarvis.toml (two levels up from this package)
  4. ~/.config/jarvis/jarvis.toml
  5. nothing — built-in defaults only

For every field, an environment variable always wins over the TOML
value. So you can ship a "good enough" `jarvis.toml` and tweak one
knob with `set JARVIS_OLLAMA_MODEL=...` without editing the file.

Env vars:
    JARVIS_CONFIG             (path to a TOML file to load)
    JARVIS_TRIGGER            (default: computer)
    JARVIS_STT_MODEL          (default: large-v3)
    JARVIS_STT_DEVICE         (default: cuda)
    JARVIS_STT_COMPUTE_TYPE   (default: float16)
    JARVIS_OLLAMA_URL         (default: http://localhost:11434)
    JARVIS_OLLAMA_VRAM_BUDGET (default: 14 — GB, max for model+kv-cache)
    JARVIS_OLLAMA_CONTEXT     (default: 16384 — tokens)
    JARVIS_OLLAMA_MODEL       (override catalog pick; default: auto)
    JARVIS_OLLAMA_SERVER_NAME (default: empty — set to your catalog "localServers" name)
    JARVIS_CATALOG_URL        (default: TypeCast models-catalog.json on GitHub)
    JARVIS_IDLE_RESET_SEC     (default: 300)
    JARVIS_VAULT_DISABLED     (set to "1" to skip the vault MCP)
    JARVIS_VAULT_CMD          (default: python)
    JARVIS_VAULT_ARGS         (default: ~/.claude/scripts/vault-server.py)
    JARVIS_NOTES_DIR          (default: ~/jarvis-notes)
    JARVIS_DICTATION_DIR      (default: ~/jarvis-dictation)
    JARVIS_WORKSPACE          (default: ~/jarvis-workspace)
    JARVIS_HOSTS              (JSON: {"name": "MAC", ...})
    JARVIS_WOL_BROADCAST      (default: 255.255.255.255)
    JARVIS_WOL_PORT           (default: 9)
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/SixOfFive/TypeCast/main/models-catalog.json"
)


def _candidate_config_paths() -> list[Path]:
    paths: list[Path] = []
    if env := os.getenv("JARVIS_CONFIG"):
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "jarvis.toml")
    # <package>/.. /.. = repo root (server/jarvis_server → server → repo)
    paths.append(Path(__file__).resolve().parent.parent.parent / "jarvis.toml")
    paths.append(Path.home() / ".config" / "jarvis" / "jarvis.toml")
    return paths


def _load_toml() -> dict[str, Any]:
    for path in _candidate_config_paths():
        try:
            if path.is_file():
                with path.open("rb") as f:
                    data = tomllib.load(f)
                log.info("loaded config: %s", path)
                return data
        except OSError as exc:
            log.warning("config %s unreadable: %s", path, exc)
        except tomllib.TOMLDecodeError as exc:
            log.warning("config %s has bad TOML: %s", path, exc)
    log.info("no jarvis.toml found; using built-in defaults")
    return {}


def _toml_get(data: dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return default
        if key not in cur:
            return default
        cur = cur[key]
    return cur


def _expand(p: str) -> str:
    """Expand ~ and env vars; leave the slash flavor as-is so Windows
    backslash paths round-trip."""
    return os.path.expandvars(os.path.expanduser(p))


def _env_or(name: str, fallback: Any) -> Any:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return fallback
    return raw


def _float_or(name: str, fallback: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def _int_or(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


@dataclass(slots=True)
class Config:
    trigger_phrase: str = "computer"

    stt_model: str = "large-v3"
    stt_device: str = "cuda"
    stt_compute_type: str = "float16"

    ollama_url: str = "http://localhost:11434"
    ollama_vram_budget_gb: float = 14.0
    ollama_context_length: int = 16384
    ollama_model_override: str = ""
    # Empty by default — set this in jarvis.toml to your catalog
    # localServers[].name so the picker prefers already-pulled models
    # on that host. Empty disables the tiebreak.
    ollama_server_name: str = ""
    catalog_url: str = _DEFAULT_CATALOG_URL

    idle_reset_sec: int = 300

    notes_dir: str = "~/jarvis-notes"
    dictation_dir: str = "~/jarvis-dictation"
    workspace_dir: str = "~/jarvis-workspace"

    vault_disabled: bool = False
    vault_command: str = "python"
    vault_args: list[str] = field(default_factory=lambda: ["~/.claude/scripts/vault-server.py"])

    wol_broadcast: str = "255.255.255.255"
    wol_port: int = 9
    wol_hosts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        toml = _load_toml()
        cfg = cls(
            trigger_phrase=_env_or(
                "JARVIS_TRIGGER",
                _toml_get(toml, "trigger_phrase", default="computer"),
            ),
            stt_model=_env_or(
                "JARVIS_STT_MODEL",
                _toml_get(toml, "stt", "model", default="large-v3"),
            ),
            stt_device=_env_or(
                "JARVIS_STT_DEVICE",
                _toml_get(toml, "stt", "device", default="cuda"),
            ),
            stt_compute_type=_env_or(
                "JARVIS_STT_COMPUTE_TYPE",
                _toml_get(toml, "stt", "compute_type", default="float16"),
            ),
            ollama_url=_env_or(
                "JARVIS_OLLAMA_URL",
                _toml_get(toml, "ollama", "url", default="http://localhost:11434"),
            ),
            ollama_vram_budget_gb=_float_or(
                "JARVIS_OLLAMA_VRAM_BUDGET",
                float(_toml_get(toml, "ollama", "vram_budget_gb", default=14.0)),
            ),
            ollama_context_length=_int_or(
                "JARVIS_OLLAMA_CONTEXT",
                int(_toml_get(toml, "ollama", "context_length", default=16384)),
            ),
            ollama_model_override=_env_or(
                "JARVIS_OLLAMA_MODEL",
                _toml_get(toml, "ollama", "model_override", default=""),
            ),
            ollama_server_name=_env_or(
                "JARVIS_OLLAMA_SERVER_NAME",
                _toml_get(toml, "ollama", "server_name", default=""),
            ),
            catalog_url=_env_or(
                "JARVIS_CATALOG_URL",
                _toml_get(toml, "ollama", "catalog_url", default=_DEFAULT_CATALOG_URL),
            ),
            idle_reset_sec=_int_or(
                "JARVIS_IDLE_RESET_SEC",
                int(_toml_get(toml, "session", "idle_reset_sec", default=300)),
            ),
            notes_dir=_env_or(
                "JARVIS_NOTES_DIR",
                _toml_get(toml, "paths", "notes_dir", default="~/jarvis-notes"),
            ),
            dictation_dir=_env_or(
                "JARVIS_DICTATION_DIR",
                _toml_get(toml, "paths", "dictation_dir", default="~/jarvis-dictation"),
            ),
            workspace_dir=_env_or(
                "JARVIS_WORKSPACE",
                _toml_get(toml, "paths", "workspace_dir", default="~/jarvis-workspace"),
            ),
            vault_disabled=(
                os.getenv("JARVIS_VAULT_DISABLED") == "1"
                if os.getenv("JARVIS_VAULT_DISABLED") is not None
                else bool(_toml_get(toml, "vault", "disabled", default=False))
            ),
            vault_command=_env_or(
                "JARVIS_VAULT_CMD",
                _toml_get(toml, "vault", "command", default="python"),
            ),
            wol_broadcast=_env_or(
                "JARVIS_WOL_BROADCAST",
                _toml_get(toml, "wol", "broadcast", default="255.255.255.255"),
            ),
            wol_port=_int_or(
                "JARVIS_WOL_PORT",
                int(_toml_get(toml, "wol", "port", default=9)),
            ),
        )

        # vault_args is a list — TOML can give us a list directly, env
        # gives us a shlex-able string. Env wins.
        env_args = os.getenv("JARVIS_VAULT_ARGS")
        if env_args:
            cfg.vault_args = shlex.split(env_args, posix=False)
        else:
            toml_args = _toml_get(toml, "vault", "args")
            if isinstance(toml_args, list):
                cfg.vault_args = [str(a) for a in toml_args]
            # else: keep the dataclass default

        # wol_hosts: TOML gives a table → dict; env gives JSON.
        env_hosts = os.getenv("JARVIS_HOSTS")
        if env_hosts:
            try:
                parsed = json.loads(env_hosts)
                if isinstance(parsed, dict):
                    cfg.wol_hosts = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError as exc:
                log.warning("JARVIS_HOSTS isn't valid JSON: %s", exc)
        else:
            toml_hosts = _toml_get(toml, "wol", "hosts")
            if isinstance(toml_hosts, dict):
                cfg.wol_hosts = {str(k): str(v) for k, v in toml_hosts.items()}

        # Expand ~ on path-like fields.
        cfg.notes_dir = _expand(cfg.notes_dir)
        cfg.dictation_dir = _expand(cfg.dictation_dir)
        cfg.workspace_dir = _expand(cfg.workspace_dir)
        cfg.vault_args = [_expand(a) for a in cfg.vault_args]

        cfg._export_to_env()
        return cfg

    def _export_to_env(self) -> None:
        """Backfill env vars from the resolved config so tool modules
        that use `os.getenv(...)` directly (notes, dictation, save_code,
        wol) pick up TOML values without each having to know about
        Config. Only sets vars that aren't already set, so an explicit
        env override at process start still wins."""
        exports = {
            "JARVIS_TRIGGER": self.trigger_phrase,
            "JARVIS_NOTES_DIR": self.notes_dir,
            "JARVIS_DICTATION_DIR": self.dictation_dir,
            "JARVIS_WORKSPACE": self.workspace_dir,
            "JARVIS_WOL_BROADCAST": self.wol_broadcast,
            "JARVIS_WOL_PORT": str(self.wol_port),
        }
        if self.wol_hosts:
            exports["JARVIS_HOSTS"] = json.dumps(self.wol_hosts)
        for k, v in exports.items():
            os.environ.setdefault(k, v)
