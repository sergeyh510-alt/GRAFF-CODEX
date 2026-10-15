"""Live engine: tail a rollout session and broadcast the growing graph.

The engine is deliberately independent of the web framework.  It runs a small
background thread that moves events from the tailer into the store and then
publishes batches through a thread-safe :class:`~graff.hub.Fanout`.  Both the
FastAPI and the standard-library server backends subscribe to that fan-out.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from .graph import DEFAULT_NODE_CAP, TEXT_CAP, Event
from .hub import Fanout
from .rollout import (
    DEFAULT_SESSIONS_ROOT,
    SessionTailer,
    list_session_paths,
    resolve_session,
    scan_session,
    summarise_sessions,
)
from .state import StateFile
from .store import GraphStore


class LiveEngine:
    """Owns the store, the tailer and the fan-out for one session."""

    def __init__(
        self,
        session_spec: Optional[str] = None,
        sessions_root: str = DEFAULT_SESSIONS_ROOT,
        journal_path: Optional[str] = None,
        snapshot_dir: Optional[str] = None,
        node_cap: int = DEFAULT_NODE_CAP,
        text_cap: int = TEXT_CAP,
        poll_interval: float = 0.4,
        flush_interval: float = 0.2,
        state_path: Optional[str] = None,
        session_locked: bool = False,
        follow: Optional[bool] = None,
    ) -> None:
        self.sessions_root = sessions_root
        self.session_spec = session_spec
        #: True when the session came from the command line, which must win over
        #: whatever was persisted in state.json.
        self.session_locked = bool(session_locked)
        self.node_cap = node_cap
        self.text_cap = text_cap
        self.poll_interval = poll_interval
        self.flush_interval = flush_interval
        self.hub = Fanout()
        self.state = StateFile(state_path) if state_path else None
        if follow is not None:
            self._follow = bool(follow)
        elif self.state is not None:
            self._follow = self.state.get_follow()
        else:
            self._follow = True
        self._last_follow_check = 0.0
        self._follow_interval = 2.0
        self._log: List[Dict[str, Any]] = []
        self.store = GraphStore(
            session_path=None,
            journal_path=journal_path,
            snapshot_dir=snapshot_dir,
            node_cap=node_cap,
            text_cap=text_cap,
        )
        self.tailer: Optional[SessionTailer] = None
        self._want_path: Optional[str] = None
        self._pending: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.started_at = time.time()

        # Optional AI enrichment (off unless clearly enabled by the user).
        self._enrich_lock = threading.Lock()
        self._enrich_state: Dict[str, Any] = {
            "enabled": False,
            "running": False,
            "model": None,
            "provider": None,
            "last": None,
            "error": None,
        }
        self._enricher = None
        self._enrich_cache = None

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        self.open_session(resolve_session(self._initial_spec(), self.sessions_root))
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._drain_loop, name="graff-engine", daemon=True)
            self._thread.start()

    def _initial_spec(self) -> Optional[str]:
        """Which session to open at startup.

        Priority: an explicit ``--session`` value > the session remembered in
        state.json > ``latest``.  A remembered path is only used when it still
        exists, so a deleted session silently falls back to ``latest``.
        """

        if self.session_locked and self.session_spec:
            return self.session_spec
        if self.state is not None:
            remembered = self.state.get_session()
            if remembered and os.path.exists(remembered):
                return remembered
        return self.session_spec or "latest"

    def stop(self) -> None:
        self._stop.set()
        if self.tailer is not None:
            self.tailer.stop()
            self.tailer = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.store.close()

    def open_session(self, path: Optional[str], remember: bool = True, reason: str = "") -> Optional[str]:
        with self._lock:
            if self.tailer is not None:
                self.tailer.stop()
                self.tailer = None
            # Drain anything still queued from the previous session before we
            # reset, otherwise its events would leak into the new graph.
            self._drain_pending()
            self.store.clear()
            self._want_path = path
            self.store.session_path = path
            if not path or not os.path.exists(path):
                # The file may not exist yet (e.g. a session about to start);
                # the drain loop retries until it appears.
                return None
            tailer = SessionTailer(path, self._enqueue, poll_interval=self.poll_interval)
            tailer.read_available()  # synchronous backfill of the existing log
            # Drain the backfill into the (now empty) store right away so the
            # very first snapshot already reflects the new session.
            self._drain_pending()
            tailer.start()
            self.tailer = tailer
            # A pinned session (CLI --session / demo) must not overwrite the
            # remembered choice, otherwise the next launch would resurrect it.
            if remember and self.state is not None and not self.session_locked:
                self.state.set_session(path)
            if reason:
                self.log_event("session", reason)
            self.hub.publish({
                "type": "reload",
                "graph": self.snapshot(),
                "status": self.status(),
                "sessions": self.sessions(),
            })
            return path

    def _drain_pending(self) -> int:
        """Move all queued events into the store without publishing."""

        moved = 0
        while True:
            try:
                event = self._pending.get_nowait()
            except queue.Empty:
                break
            if self.store.add(event):
                moved += 1
        return moved

    def delete_event(self, event_id: str, cascade: bool = True) -> Dict[str, Any]:
        result = self.store.delete_event(event_id, cascade=cascade)
        if result["count"]:
            self.hub.publish({
                "type": "reload",
                "graph": self.snapshot(),
                "status": self.status(),
                "sessions": self.sessions(),
            })
        return result

    def _retry_open(self) -> None:
        with self._lock:
            if self.tailer is not None:
                return
            if self._want_path and os.path.exists(self._want_path):
                self.open_session(self._want_path)

    # -- follow-newest + activity log -------------------------------------- #

    def log_event(self, kind: str, message: str) -> None:
        """Append a human-readable line to the server-side activity log."""

        entry = {"ts": time.time(), "kind": kind, "text": message}
        with self._lock:
            self._log.append(entry)
            if len(self._log) > 200:
                self._log = self._log[-200:]
        self.hub.publish({"type": "log", "entry": entry})

    def log_snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._log)

    def get_follow(self) -> bool:
        return bool(self._follow)

    def set_follow(self, follow: bool, announce: bool = True) -> bool:
        self._follow = bool(follow)
        if self.state is not None:
            self.state.set_follow(self._follow)
        if announce:
            self.log_event("follow", "слежение за новыми сессиями: "
                           + ("вкл" if self._follow else "выкл"))
        self.hub.publish({"type": "follow", "follow": self._follow})
        return self._follow

    def _check_follow(self) -> None:
        """Switch to a newer contentful session when one appears (follow mode)."""

        if not self._follow or self.session_locked:
            return
        try:
            newest = resolve_session("latest", self.sessions_root, require_content=True)
        except Exception:
            return
        if not newest:
            return
        current = self.store.session_path
        if current and os.path.abspath(newest) == os.path.abspath(current):
            return
        name = os.path.basename(newest)
        self.open_session(newest, remember=True,
                          reason="новая сессия: " + name)

    # -- drain loop -------------------------------------------------------- #

    def _enqueue(self, event) -> None:
        self._pending.put(event)

    def _drain_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.flush_interval)
            if self.tailer is None:
                self._retry_open()
            self._maybe_follow()
            self.drain_once()

    def _maybe_follow(self) -> None:
        now = time.time()
        if now - self._last_follow_check < self._follow_interval:
            return
        self._last_follow_check = now
        self._check_follow()

    def drain_once(self) -> int:
        batch: List[Any] = []
        while True:
            try:
                batch.append(self._pending.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        fresh = []
        for event in batch:
            if self.store.add(event):
                fresh.append(event)
        if not fresh:
            return 0
        self.hub.publish(
            {
                "type": "events",
                "events": [event.to_dict() for event in fresh],
                "counts": self.store.counts(),
                "stats": self.store.stats(),
            }
        )
        return len(fresh)

    # -- queries ----------------------------------------------------------- #

    def snapshot(self, window: float = 0.0, types: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        return self.store.filter_snapshot(window_seconds=window, types=types)

    def sessions(self, limit: int = 40, with_stats: bool = True) -> List[Dict[str, Any]]:
        """List the newest sessions, newest-first, with per-session statistics."""

        if not with_stats:
            result = []
            for path in list_session_paths(self.sessions_root, limit=limit):
                try:
                    stat = os.stat(path)
                    size, mtime = stat.st_size, stat.st_mtime
                except OSError:
                    size, mtime = 0, 0.0
                result.append({
                    "path": path,
                    "name": os.path.basename(path),
                    "size": size,
                    "mtime": mtime,
                    "events": 0,
                    "nodes": 0,
                    "tool_calls": 0,
                    "current": path == self.store.session_path,
                })
            return result

        current = self.store.session_path
        result = []
        for info in summarise_sessions(self.sessions_root, limit=limit):
            entry = info.to_dict()
            entry["current"] = info.path == current
            entry["empty"] = not info.contentful
            # The parser emits roughly one graph node per accepted event, so the
            # event count is a good (and cheap) proxy for the node count.
            entry["nodes"] = info.events
            result.append(entry)
        return result

    def bootstrap(self, window: float = 0.0, types: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        return {
            "type": "init",
            "graph": self.snapshot(window, types),
            "status": self.status(),
            "sessions": self.sessions(),
        }

    def status(self) -> Dict[str, Any]:
        tailer = self.tailer
        stats = self.store.stats()
        return {
            "session": self.store.session_path,
            "sessions_root": self.sessions_root,
            "journal": self.store.journal_path,
            "snapshot_dir": self.store.snapshot_dir,
            "clients": self.hub.count,
            "started_at": self.started_at,
            "uptime": time.time() - self.started_at,
            "events": stats["event_count"],
            "started": stats["started"],
            "updated": stats["updated"],
            "lines_read": tailer.lines_read if tailer else 0,
            "parse_errors": tailer.parse_errors if tailer else 0,
            "unknown_records": tailer.unknown_records if tailer else 0,
            "counts": self.store.counts(),
            "ai": dict(self._enrich_state),
            "can_enrich": bool(self._enrich_state.get("available")),
            "follow": bool(self._follow),
            "session_locked": bool(self.session_locked),
            "state_path": self.state.path if self.state else None,
            "log": self.log_snapshot()[-40:],
        }

    # -- optional AI enrichment ------------------------------------------- #

    def ai_status(self, explicit: Optional[bool] = None) -> Dict[str, Any]:
        """Describe whether enrichment can run, without ever exposing the key.

        ``explicit`` only changes the user-facing ``enabled`` flag (set from the
        ``--enrich`` CLI switch).  ``available`` reflects whether a key is
        actually present in the environment.
        """

        from .enrich import EnrichConfig

        config = EnrichConfig.from_env()
        if explicit is not None:
            self._enrich_state["enabled"] = bool(explicit)
        available = config is not None
        self._enrich_state["available"] = available
        self._enrich_state["model"] = config.model if config else None
        self._enrich_state["provider"] = config.provider if config else None
        if available:
            self._enrich_state["error"] = None
        else:
            self._enrich_state["error"] = (
                "нет ключа: задайте ROUTERAI_API_KEY или AITUNNEL_API_KEY"
            )
        return dict(self._enrich_state)

    def _ensure_enricher(self):
        if self._enricher is not None:
            return self._enricher
        from .enrich import EnrichCache, EnrichConfig, Enricher

        config = EnrichConfig.from_env()
        if config is None:
            raise RuntimeError("нет ключа: задайте ROUTERAI_API_KEY или AITUNNEL_API_KEY")
        if self._enrich_cache is None:
            cache_path = None
            if self.store.snapshot_dir:
                cache_path = os.path.join(os.path.dirname(self.store.snapshot_dir), "enrich_cache.json")
            self._enrich_cache = EnrichCache(cache_path)
        self._enricher = Enricher(config, cache=self._enrich_cache)
        return self._enricher

    def enrich(self, limit: int = 40) -> Dict[str, Any]:
        """Run enrichment synchronously and apply the resulting reason nodes."""

        with self._enrich_lock:
            self._enrich_state["running"] = True
            try:
                enricher = self._ensure_enricher()
                plan_source = self.store.events_snapshot()
                result = enricher.enrich(plan_source, limit=max(1, int(limit)))
                for item in result["results"]:
                    self.store.inject_reason(
                        reason_id=item["reason"]["id"],
                        ts=item["reason"]["ts"],
                        text=item["reason"]["text"],
                        parent_id=item["parent_id"],
                        children=item["children"],
                    )
                self._enrich_state["last"] = {
                    "ts": time.time(),
                    "enriched": result["enriched"],
                    "requests": result["requests"],
                    "cache_hits": result["cache_hits"],
                }
                self._enrich_state["error"] = result["errors"][0] if result["errors"] else None
                snapshot = self.snapshot()
                self.hub.publish({
                    "type": "reload",
                    "graph": snapshot,
                    "status": self.status(),
                    "sessions": self.sessions(),
                })
                result = dict(result)
                result.pop("results", None)
                return {"ok": True, **result}
            except Exception as exc:
                self._enrich_state["error"] = str(exc)
                return {"ok": False, "error": str(exc)}
            finally:
                self._enrich_state["running"] = False
