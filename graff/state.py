"""Persistent, hand-editable UI/server state (``runtime/state.json``).

The visualizer remembers which session the user was looking at and whether the
follow-newest mode is on, so restarting the server lands you back where you were.
The file lives in the *runtime* directory (next to the journal), never inside the
program directory, so a packaged ``GRAFF.exe`` stays read-only.

The module is deliberately dependency-free and defensive: a corrupt or missing
file simply falls back to defaults instead of raising.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

#: Bump when the on-disk shape changes in an incompatible way.
STATE_VERSION = 1

DEFAULT_FOLLOW = True


def default_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "session": None,          # last opened rollout path (None = "latest")
        "follow": DEFAULT_FOLLOW,  # auto-switch to a newer contentful session
        "updated": 0.0,
    }


class StateFile:
    """Tiny JSON store with atomic writes and an in-memory cache."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = self._read()

    # -- io ---------------------------------------------------------------- #

    def _read(self) -> Dict[str, Any]:
        data = default_state()
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                for key in ("session", "follow", "updated", "version"):
                    if key in loaded:
                        data[key] = loaded[key]
        except FileNotFoundError:
            pass
        except Exception:
            # Corrupt file: keep defaults, do not crash the server.
            pass
        return data

    def _write(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(tmp, self.path)

    # -- accessors --------------------------------------------------------- #

    def get_session(self) -> Optional[str]:
        with self._lock:
            value = self._data.get("session")
        return value if isinstance(value, str) and value else None

    def set_session(self, path: Optional[str]) -> None:
        with self._lock:
            self._data["session"] = path or None
            self._data["updated"] = time.time()
            self._write()

    def get_follow(self) -> bool:
        with self._lock:
            value = self._data.get("follow")
        return DEFAULT_FOLLOW if value is None else bool(value)

    def set_follow(self, follow: bool) -> None:
        with self._lock:
            self._data["follow"] = bool(follow)
            self._data["updated"] = time.time()
            self._write()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def exists(self) -> bool:
        return os.path.isfile(self.path)