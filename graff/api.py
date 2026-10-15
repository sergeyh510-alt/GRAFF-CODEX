"""Framework-agnostic HTTP/WebSocket API for the visualizer."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Sequence

from .graph import TYPES


class GraffApi:
    """All request handling shared by the FastAPI and stdlib backends."""

    def __init__(self, engine, static_dir: str, runtime_dir: str) -> None:
        self.engine = engine
        self.static_dir = static_dir
        self.runtime_dir = runtime_dir
        self.exports_dir = os.path.join(runtime_dir, "exports")
        self.snapshots_dir = os.path.join(runtime_dir, "snapshots")
        os.makedirs(self.exports_dir, exist_ok=True)
        os.makedirs(self.snapshots_dir, exist_ok=True)

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def parse_types(raw: Optional[str]) -> Optional[List[str]]:
        if not raw:
            return None
        wanted = [t for t in raw.split(",") if t in TYPES]
        return wanted or None

    # -- endpoints --------------------------------------------------------- #

    def snapshot(self, window: float = 0.0, types: Optional[str] = None) -> Dict[str, Any]:
        return self.engine.snapshot(window=window, types=self.parse_types(types))

    def status(self) -> Dict[str, Any]:
        return self.engine.status()

    def sessions(self, limit: int = 40) -> Dict[str, Any]:
        return {"sessions": self.engine.sessions(limit=limit)}

    def ai_status(self) -> Dict[str, Any]:
        return self.engine.ai_status(explicit=None)

    def open_session(self, spec: str) -> Dict[str, Any]:
        from .rollout import resolve_session

        if not spec:
            spec = "latest"
        resolved = resolve_session(spec, self.engine.sessions_root)
        if not resolved:
            return {"ok": False, "error": "сессии не найдены", "spec": spec}
        if not os.path.exists(resolved):
            return {"ok": False, "error": "файл сессии не найден", "spec": spec, "session": resolved}
        self.engine.open_session(resolved, remember=True, reason="открыта вручную: "
                                 + os.path.basename(resolved))
        return {
            "ok": True,
            "session": resolved,
            "name": os.path.basename(resolved),
            "follow": self.engine.get_follow(),
            "status": self.engine.status(),
        }

    def set_follow(self, follow: bool) -> Dict[str, Any]:
        """Turn "follow new sessions" on/off (persisted in state.json)."""

        value = self.engine.set_follow(bool(follow))
        return {"ok": True, "follow": value}

    def log(self) -> Dict[str, Any]:
        return {"log": self.engine.log_snapshot()}

    def state(self) -> Dict[str, Any]:
        state = getattr(self.engine, "state", None)
        return {
            "session": self.engine.store.session_path,
            "follow": self.engine.get_follow(),
            "session_locked": bool(getattr(self.engine, "session_locked", False)),
            "state_path": state.path if state is not None else None,
        }

    def delete(self, event_id: str, cascade: bool = True) -> Dict[str, Any]:
        if not event_id:
            return {"ok": False, "error": "event_id is required"}
        result = self.engine.delete_event(event_id, cascade=cascade)
        return {"ok": True, "deleted": result["count"], "ids": result["removed"]}

    def enrich(self, limit: int = 40) -> Dict[str, Any]:
        """Summarise assistant steps into reason nodes via an LLM (opt-in)."""

        status = self.engine.ai_status()
        if not status.get("available"):
            return {
                "ok": False,
                "error": status.get("error") or (
                    "нет ключа: задайте ROUTERAI_API_KEY или AITUNNEL_API_KEY"
                ),
                "ai": status,
            }
        result = self.engine.enrich(limit=limit)
        result["ai"] = self.engine.ai_status()
        return result

    def bootstrap(self, window: float = 0.0, types: Optional[str] = None) -> Dict[str, Any]:
        return self.engine.bootstrap(window=window, types=self.parse_types(types))

    def save(self, kind: str = "json") -> Dict[str, Any]:
        kind = (kind or "json").lower()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        session_name = os.path.splitext(os.path.basename(self.engine.store.session_path or "session"))[0]
        try:
            if kind in ("json", "snapshot"):
                target = os.path.join(self.snapshots_dir, "%s-%s.json" % (session_name, stamp))
                path = self.engine.store.write_snapshot_json(target)
            elif kind == "graphml":
                target = os.path.join(self.exports_dir, "%s-%s.graphml" % (session_name, stamp))
                path = self.engine.store.write_graphml(target)
            elif kind == "gexf":
                target = os.path.join(self.exports_dir, "%s-%s.gexf" % (session_name, stamp))
                path = self.engine.store.write_gexf(target)
            else:
                return {"ok": False, "error": "unknown kind: %s" % kind}
        except ImportError:
            return {
                "ok": False,
                "error": "networkx is not installed; run: pip install -r requirements.txt",
                "kind": kind,
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": str(exc), "kind": kind}
        return {"ok": True, "kind": kind, "path": path}
