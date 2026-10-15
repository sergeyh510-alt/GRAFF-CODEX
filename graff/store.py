"""Live graph state plus persistence (event journal, snapshots, exports)."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__
from .graph import ACTORS, DEFAULT_NODE_CAP, TEXT_CAP, TYPES, Event, build_graph


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + "Z"


class GraphStore:
    """Thread-safe, append-only model of the visualized session."""

    def __init__(
        self,
        session_path: Optional[str] = None,
        journal_path: Optional[str] = None,
        snapshot_dir: Optional[str] = None,
        node_cap: int = DEFAULT_NODE_CAP,
        text_cap: int = TEXT_CAP,
        max_events: int = 15000,
    ) -> None:
        self.session_path = session_path
        self.journal_path = journal_path
        self.snapshot_dir = snapshot_dir
        self.node_cap = node_cap
        self.text_cap = text_cap
        self.max_events = max_events

        self._events: List[Event] = []
        self._by_id: Dict[str, Event] = {}
        self._recent: deque = deque()
        self._lock = threading.RLock()
        self._dirty = True
        self._nodes: List[Dict[str, Any]] = []
        self._edges: List[Dict[str, Any]] = []
        self._dropped = 0
        self._seq = 0
        self._node_counter = 0

        self._journal = None
        if journal_path:
            os.makedirs(os.path.dirname(os.path.abspath(journal_path)) or ".", exist_ok=True)
            self._journal = open(journal_path, "a", encoding="utf-8", newline="\n")

    # -- lifecycle --------------------------------------------------------- #

    def close(self) -> None:
        if self._journal is not None:
            try:
                self._journal.flush()
                self._journal.close()
            except Exception:
                pass
            self._journal = None

    # -- mutation ---------------------------------------------------------- #

    def add(self, event: Event) -> bool:
        """Append an event.  Returns False when it is a duplicate."""

        with self._lock:
            if self._event_key(event) in self._by_id:
                return False
            self._assign_id(event)
            self._events.append(event)
            self._by_id[event.id] = event
            self._recent.append(event.id)
            if len(self._recent) > 20000:
                self._recent.popleft()
            if len(self._events) > self.max_events:
                overflow = len(self._events) - self.max_events
                for stale in self._events[:overflow]:
                    self._by_id.pop(stale.id, None)
                self._events = self._events[overflow:]
            self._dirty = True
        self._write_journal(event)
        return True

    def _event_key(self, event: Event) -> str:
        ordinal = event.extra.get("ordinal")
        if isinstance(ordinal, int):
            return "ord:%d:%s" % (ordinal, event.type)
        return "id:" + event.id

    def _assign_id(self, event: Event) -> None:
        # The parser already produced stable ids (ordinal based "n7" or content
        # hashes "h...").  Only synthesize one for events that arrive without it.
        if not event.id:
            self._node_counter += 1
            event.id = "g%d" % self._node_counter
        if event.id in self._by_id:
            self._seq += 1
            event.id = "%s_%d" % (event.id, self._seq)

    def clear(self) -> None:
        """Reset the store (used when switching to a different session)."""

        with self._lock:
            self._events = []
            self._by_id = {}
            self._recent = deque()
            self._nodes = []
            self._edges = []
            self._dropped = 0
            self._dirty = True

    def events_snapshot(self) -> List[Event]:
        """Return a shallow copy of the current event list (thread-safe)."""

        with self._lock:
            return list(self._events)

    def delete_event(self, event_id: str, cascade: bool = True) -> Dict[str, Any]:
        """Remove an event (and, by default, its whole subtree)."""

        with self._lock:
            removed: List[str] = []
            ids = [event_id]
            if cascade:
                children: Dict[Optional[str], List[str]] = {}
                for event in self._events:
                    children.setdefault(event.parent_id, []).append(event.id)
                stack = [event_id]
                while stack:
                    current = stack.pop()
                    for child_id in children.get(current, []):
                        ids.append(child_id)
                        stack.append(child_id)
            removed_set = set(ids)
            self._events = [event for event in self._events if event.id not in removed_set]
            for removed_id in removed_set:
                if self._by_id.pop(removed_id, None) is not None:
                    removed.append(removed_id)
            if removed:
                self._dirty = True
            return {"removed": removed, "count": len(removed)}

    def load_events(self, events: Iterable[Event]) -> int:
        count = 0
        for event in events:
            if self.add(event):
                count += 1
        return count

    def inject_reason(self, reason_id: str, ts: float, text: str, parent_id: Optional[str],
                      children: Sequence[str]) -> Tuple[bool, List[str]]:
        """Insert a synthetic ``reason`` node and re-parent its tool calls.

        Used by optional AI enrichment.  Returns ``(created, reparented)`` where
        ``reparented`` is the list of child ids whose parent was moved.
        """

        event = Event(
            id=reason_id,
            ts=float(ts or 0.0),
            actor="codex",
            type="reason",
            text=text,
            parent_id=parent_id,
            extra={"synthetic": True},
        )
        with self._lock:
            created = self.add(event)
            reparented: List[str] = []
            for child_id in children:
                child = self._by_id.get(child_id)
                if child is None:
                    continue
                if child.parent_id == reason_id:
                    continue
                child.parent_id = reason_id
                child.extra["reparented"] = parent_id
                reparented.append(child_id)
            if reparented:
                self._dirty = True
        return created, reparented

    # -- journal / snapshot ------------------------------------------------- #

    def _write_journal(self, event: Event) -> None:
        if self._journal is None:
            return
        try:
            record = event.to_dict()
            record["wall"] = iso(event.ts)
            self._journal.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._journal.flush()
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            if self._dirty:
                nodes, edges, dropped = build_graph(self._events, self.node_cap, self.text_cap)
                self._nodes, self._edges, self._dropped = nodes, edges, dropped
                self._dirty = False
            nodes = self._nodes
            edges = self._edges
            dropped = self._dropped

        counts = {etype: 0 for etype in TYPES}
        for node in nodes:
            counts[node["type"]] = counts.get(node["type"], 0) + 1
        t0 = nodes[0]["ts"] if nodes else 0.0
        t1 = nodes[-1]["ts"] if nodes else 0.0
        return {
            "session": self.session_path,
            "nodes": nodes,
            "edges": edges,
            "dropped": dropped,
            "counts": counts,
            "meta": {
                "path": self.session_path,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "dropped": dropped,
                "started": t0,
                "updated": t1,
                "generated_at": time.time(),
                "version": __version__,
                "journal": self.journal_path,
            },
        }

    def _in_window(self, ts: Optional[float], window: float) -> bool:
        if not window or ts is None or ts <= 0:
            return False
        latest = self._events[-1].ts if self._events else 0.0
        return (latest - ts) <= window

    def filter_snapshot(self, window_seconds: float = 0.0, types: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        snap = self.snapshot()
        if window_seconds and window_seconds > 0:
            latest = snap["meta"]["updated"] or time.time()
            cutoff = latest - window_seconds
            keep = {n["id"] for n in snap["nodes"] if n["ts"] >= cutoff}
            snap["nodes"] = [n for n in snap["nodes"] if n["id"] in keep]
            snap["edges"] = [e for e in snap["edges"] if e["from"] in keep and e["to"] in keep]
        if types:
            wanted = set(types)
            keep = {n["id"] for n in snap["nodes"] if n["type"] in wanted}
            snap["nodes"] = [n for n in snap["nodes"] if n["id"] in keep]
            snap["edges"] = [e for e in snap["edges"] if e["from"] in keep and e["to"] in keep]
        return snap

    def counts(self) -> Dict[str, int]:
        with self._lock:
            result = {etype: 0 for etype in TYPES}
            for event in self._events:
                result[event.type] = result.get(event.type, 0) + 1
            return result

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            event_count = len(self._events)
            started = self._events[0].ts if self._events else 0.0
            updated = self._events[-1].ts if self._events else 0.0
        return {
            "event_count": event_count,
            "started": started,
            "updated": updated,
            "session": self.session_path,
        }

    # -- persistence helpers ------------------------------------------------ #

    def write_snapshot_json(self, path: str) -> str:
        snap = self.snapshot()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(snap, handle, ensure_ascii=False, indent=2)
        return path

    def write_graphml(self, path: str) -> str:
        import networkx as nx  # optional dependency

        snap = self.snapshot()
        graph = nx.DiGraph()
        for node in snap["nodes"]:
            graph.add_node(
                node["id"],
                label=node["label"],
                type=node["type"],
                actor=node["actor"],
                ts=node["ts"],
                text=(node["text"] or "")[:2000],
            )
        for edge in snap["edges"]:
            graph.add_edge(edge["from"], edge["to"])
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        nx.write_graphml(graph, path)
        return path

    def write_gexf(self, path: str) -> str:
        import networkx as nx

        snap = self.snapshot()
        graph = nx.DiGraph()
        for node in snap["nodes"]:
            graph.add_node(
                node["id"],
                label=node["label"],
                type=node["type"],
                actor=node["actor"],
                ts=node["ts"],
                text=(node["text"] or "")[:2000],
            )
        for edge in snap["edges"]:
            graph.add_edge(edge["from"], edge["to"])
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        nx.write_gexf(graph, path)
        return path


    @classmethod
    def from_journal(cls, journal_path: str, **kwargs: Any) -> "GraphStore":
        """Rebuild a store by replaying an event journal.

        The whole journal is read into memory *before* the store is opened for
        appending, so reloading never writes into the file it is reading (which
        would otherwise grow the file forever while iterating it).
        """

        records: List[Dict[str, Any]] = []
        if os.path.exists(journal_path):
            with open(journal_path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
        store = cls(journal_path=journal_path, **kwargs)
        for data in records:
            try:
                store.add(Event.from_dict(data))
            except Exception:
                continue
        return store
