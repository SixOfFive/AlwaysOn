"""Model catalog: download, cache, and pick the best-fit model.

The catalog at https://github.com/SixOfFive/TypeCast describes every
Ollama model the user has benchmarked, including per-role scores
(`router`, `orchestrator`, `coder`, …), `estimatedVramGb`,
`contextLength`, and which servers it's already installed on. We
download once, cache locally, and select the model with the highest
`orchestrator` score that fits our VRAM/context constraints.

The catalog is ~18 MB. We cache it under
`%LOCALAPPDATA%\\jarvis-server\\models-catalog.json` with the matching
ETag, and only re-download if the cached copy is >24 h old AND the
remote ETag changed. If the network is down or the catalog 404s, we
fall back to the cached copy. Best-fit selection is silent unless a
new download changes the result.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_URL = "https://raw.githubusercontent.com/SixOfFive/TypeCast/main/models-catalog.json"
_CACHE_TTL_SEC = 24 * 3600
# Role we care about: the model has to drive a tool-using voice
# assistant. "orchestrator" in the catalog == tool-call quality +
# multi-step reasoning, which is the closest match.
_ROLE = "orchestrator"


def _cache_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "jarvis-server"


def _cache_path() -> Path:
    return _cache_dir() / "models-catalog.json"


def _etag_path() -> Path:
    return _cache_dir() / "models-catalog.etag"


@dataclass(slots=True)
class ModelChoice:
    tag: str
    score: float
    estimated_vram_gb: float
    context_length: int
    parameter_size: str
    installed_locally: bool
    avg_tok_sec: float | None
    reason: str


async def load_catalog(url: str = _DEFAULT_URL) -> dict[str, Any]:
    """Download or load the cached catalog. Returns the parsed JSON.

    Raises RuntimeError if no catalog can be obtained at all (network
    down AND no cache on disk).
    """
    cache = _cache_path()
    etag_file = _etag_path()
    cache.parent.mkdir(parents=True, exist_ok=True)

    cached_ok = cache.is_file()
    cache_fresh = cached_ok and (time.time() - cache.stat().st_mtime) < _CACHE_TTL_SEC

    if cache_fresh:
        log.info("catalog cache hit (fresh, age=%.0fs): %s",
                 time.time() - cache.stat().st_mtime, cache)
        return _read_cache(cache)

    headers: dict[str, str] = {}
    if cached_ok and etag_file.is_file():
        try:
            headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=headers, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.warning("catalog download failed (%s); using cache", exc)
        if cached_ok:
            return _read_cache(cache)
        raise RuntimeError(f"catalog unreachable and no cache: {exc}") from exc

    if r.status_code == 304 and cached_ok:
        log.info("catalog unchanged (ETag match); using cache")
        cache.touch()  # extend freshness window
        return _read_cache(cache)

    if r.status_code != 200:
        log.warning("catalog HTTP %d; using cache", r.status_code)
        if cached_ok:
            return _read_cache(cache)
        raise RuntimeError(f"catalog HTTP {r.status_code} and no cache")

    cache.write_bytes(r.content)
    etag = r.headers.get("ETag")
    if etag:
        etag_file.write_text(etag, encoding="utf-8")
    log.info("catalog downloaded: %s (%.1f MB)", cache, len(r.content) / 1e6)
    return _read_cache(cache)


def _read_cache(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def pick_model(
    catalog: dict[str, Any],
    *,
    vram_budget_gb: float,
    min_context: int,
    preferred_server: str,
    banned: set[str] | None = None,
) -> ModelChoice | None:
    """Filter and rank catalog entries, return the best fit or None.

    Selection criteria (in order):
      1. capabilities contains "tools" (hard filter)
      2. tag not in `banned` — auto-populated when a previous run's
         pick timed out (see banlist.py)
      3. estimatedVramGb <= vram_budget_gb
      4. contextLength >= min_context
      5. has a roleScores.<role> entry (so we have a benchmark to rank on)
    Ranking:
      - roleScores.<role>.score desc
      - installed locally on preferred_server desc (tiebreak)
      - avgTokSec desc (final tiebreak, faster wins)
    """
    banned = banned or set()
    candidates: list[tuple[float, int, float, str, dict[str, Any]]] = []
    for tag, entry in catalog.items():
        if tag.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        if tag in banned:
            continue
        caps = entry.get("capabilities") or []
        if "tools" not in caps:
            continue
        vram = entry.get("estimatedVramGb")
        if not isinstance(vram, (int, float)) or vram > vram_budget_gb:
            continue
        ctx = entry.get("contextLength")
        if not isinstance(ctx, int) or ctx < min_context:
            continue
        role_scores = entry.get("roleScores") or {}
        role_entry = role_scores.get(_ROLE)
        if not isinstance(role_entry, dict):
            continue
        score = role_entry.get("score")
        if not isinstance(score, (int, float)):
            continue

        servers = entry.get("servers") or []
        # When preferred_server is empty (default — no per-deployment
        # catalog hint), the on_local tiebreak is meaningless. Treat
        # every candidate as equally "not preferred" so the sort falls
        # through to score / tok_sec cleanly.
        on_local = 1 if (preferred_server and preferred_server in servers) else 0
        tok_sec = role_entry.get("avgTokSec") or 0.0
        candidates.append((float(score), on_local, float(tok_sec), tag, entry))

    if not candidates:
        return None

    # Sort by (score desc, on_local desc, tok_sec desc).
    candidates.sort(key=lambda c: (-c[0], -c[1], -c[2]))
    score, on_local, tok_sec, tag, entry = candidates[0]

    parts = [
        f"top {_ROLE} score={score:.0f}",
        f"vram={entry['estimatedVramGb']:.1f}GB",
        f"ctx={entry['contextLength']}",
        f"params={entry.get('parameterSize', '?')}",
    ]
    if preferred_server:
        # Only include the "already on X" / "needs pull" clause when
        # the user actually configured a server name. Otherwise it
        # would leak the catalog-author's server names into our logs.
        parts.append(
            f"already on {preferred_server}" if on_local else "needs pull"
        )

    return ModelChoice(
        tag=tag,
        score=score,
        estimated_vram_gb=float(entry["estimatedVramGb"]),
        context_length=int(entry["contextLength"]),
        parameter_size=str(entry.get("parameterSize", "?")),
        installed_locally=bool(on_local),
        avg_tok_sec=float(tok_sec) if tok_sec else None,
        reason=", ".join(parts),
    )
