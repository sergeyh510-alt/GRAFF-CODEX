"""Filesystem layout that works both from the source tree and from a frozen exe.

When GRAFF is started with ``python run.py`` the project directory holds the
``static/`` assets and the ``runtime/`` output.  Inside a PyInstaller one-file exe
the assets are unpacked into ``sys._MEIPASS`` while the program directory is
read-only (and may be a shared/temp folder), so:

* **read-only** assets (``static/``) are resolved with :func:`resource_dir`;
* **writable** state (journal, snapshots, ``state.json``) is resolved with
  :func:`default_runtime_dir`, which prefers a ``runtime/`` folder next to the
  executable and falls back to ``%LOCALAPPDATA%/GRAFF`` when that is not writable.

Nothing is ever written back into the exe.
"""
from __future__ import annotations

import os
import sys
from typing import Optional


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""

    return bool(getattr(sys, "frozen", False))


def program_dir() -> str:
    """Directory of the executable (frozen) or the project root (source)."""

    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_dir() -> str:
    """Directory that holds the bundled read-only resources."""

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.abspath(meipass)
    return program_dir()


def resource_dir(*parts: str) -> str:
    """Path to a bundled resource (``static`` lives here)."""

    return os.path.join(bundle_dir(), *parts)


def _writable(directory: str) -> bool:
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".graff-write-test")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _local_app_data() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "GRAFF")


def default_runtime_dir() -> str:
    """Writable directory for ``runtime/`` output (never inside the bundle)."""

    if not is_frozen():
        return os.path.join(program_dir(), "runtime")
    beside_exe = os.path.join(program_dir(), "runtime")
    if _writable(beside_exe):
        return beside_exe
    return os.path.join(_local_app_data(), "runtime")


def static_dir() -> str:
    """The bundled ``static/`` directory."""

    return resource_dir("static")