"""Thread-safe fan-out of live updates to connected clients.

The engine publishes from its own background thread, so subscribers are plain
callables (thread-safe) rather than asyncio queues.  Each server backend adapts
its own transport on top of this (a ``queue.Queue`` for the stdlib server, an
``asyncio.Queue`` bridged with ``call_soon_threadsafe`` for FastAPI).
"""
from __future__ import annotations

import itertools
import threading
from typing import Any, Callable, Dict, List, Tuple

Subscriber = Callable[[Dict[str, Any]], None]


class Fanout:
    def __init__(self) -> None:
        self._subs: List[Tuple[int, Subscriber]] = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def subscribe(self, callback: Subscriber) -> int:
        token = next(self._ids)
        with self._lock:
            self._subs.append((token, callback))
        return token

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subs = [(tok, cb) for tok, cb in self._subs if tok != token]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, message: Dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subs)
        for _token, callback in subscribers:
            try:
                callback(message)
            except Exception:
                pass
