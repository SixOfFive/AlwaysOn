"""Register pip-installed NVIDIA DLLs with Python's DLL search path.

faster-whisper / CTranslate2 dlopen() libraries by short name (e.g.
`cublas64_12.dll`). The `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels
install those into `site-packages/nvidia/<lib>/bin/`, which is NOT on
PATH and which Windows doesn't search by default since Python 3.8.

Importing this module before faster_whisper makes `--stt-device cuda`
work without the user manually managing PATH or installing the CUDA
Toolkit system-wide.
"""

from __future__ import annotations

import logging
import os
import site
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _site_packages_dirs() -> list[Path]:
    seen: set[Path] = set()
    candidates: list[Path] = []
    for p in site.getsitepackages():
        path = Path(p)
        if path not in seen and path.is_dir():
            seen.add(path)
            candidates.append(path)
    usp = site.getusersitepackages()
    if usp:
        path = Path(usp)
        if path not in seen and path.is_dir():
            candidates.append(path)
    return candidates


def register_cuda_dlls() -> int:
    """Return the number of bin dirs registered. Safe to call on non-Windows
    (no-op).

    CTranslate2 calls Win32 LoadLibrary by short name, which searches PATH
    (and not the directories added by os.add_dll_directory). We do both:
    add_dll_directory for Python-level loads, AND prepend PATH for the
    native dlopen path.
    """
    if sys.platform != "win32":
        return 0
    bin_dirs: list[Path] = []
    for site_dir in _site_packages_dirs():
        nvidia_root = site_dir / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for bin_dir in nvidia_root.glob("*/bin"):
            if bin_dir.is_dir():
                bin_dirs.append(bin_dir)
    if not bin_dirs:
        return 0
    for bin_dir in bin_dirs:
        try:
            os.add_dll_directory(str(bin_dir))
        except OSError as exc:
            log.debug("add_dll_directory(%s): %s", bin_dir, exc)
    # Prepend to PATH so LoadLibrary() finds DLLs called by short name.
    prepend = os.pathsep.join(str(p) for p in bin_dirs)
    existing = os.environ.get("PATH", "")
    if existing:
        os.environ["PATH"] = prepend + os.pathsep + existing
    else:
        os.environ["PATH"] = prepend
    log.debug("registered %d NVIDIA DLL dirs for CUDA load", len(bin_dirs))
    return len(bin_dirs)


register_cuda_dlls()
