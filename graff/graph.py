"""Graph model built from the normalized event stream."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

ACTORS: Tuple[str, ...] = ("codex", "rowboat", "user")
TYPES: Tuple[str, ...] = ("reason", "message", "tool_call", "tool_result", "file_edit", "error")

DEFAULT_NODE_CAP = 4000
TEXT_CAP = 20000
LABEL_CAP = 180


def clean_text(text: Any) -> str:
    if text is None:
        return ""
    return str(text).replace("\r\n", "\n").replace("\r", "\n").strip()


@dataclass
class Event:
    """A single normalized graph event."""

    id: str
    ts: float
    actor: str
    type: str
    text: str = ""
    parent_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "type": self.type,
            "text": self.text,
            "parent_id": self.parent_id,
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        known = {"id", "ts", "actor", "type", "text", "parent_id"}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            id=str(data["id"]),
            ts=float(data.get("ts") or 0.0),
            actor=str(data.get("actor") or "codex"),
            type=str(data.get("type") or "message"),
            text=data.get("text") or "",
            parent_id=data.get("parent_id"),
            extra=extra,
        )

    def label(self, max_len: int = LABEL_CAP) -> str:
        text = clean_text(self.text)
        if not text:
            if self.type == "tool_call":
                return str(self.extra.get("name") or "tool_call")
            return self.type
        line = text.split("\n", 1)[0].strip()
        if len(line) > max_len:
            line = line[: max_len - 1].rstrip() + "\u2026"
        return line


def build_graph(
    events: Sequence[Event],
    node_cap: int = DEFAULT_NODE_CAP,
    text_cap: int = TEXT_CAP,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Return (nodes, edges, dropped) for the last ``node_cap`` events."""

    usable = [e for e in events if e.type in TYPES]
    dropped = max(0, len(usable) - node_cap)
    window = usable[dropped:]

    present = {e.id for e in window}
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    for event in window:
        meta = dict(event.extra)
        args = meta.get("args")
        if isinstance(args, str) and len(args) > 4000:
            meta["args"] = args[:4000] + "\u2026"
        text = event.text
        truncated = False
        if len(text) > text_cap:
            text = text[:text_cap]
            truncated = True
        nodes.append(
            {
                "id": event.id,
                "label": event.label(),
                "type": event.type,
                "actor": event.actor,
                "ts": event.ts,
                "parent": event.parent_id,
                "text": text,
                "truncated": truncated,
                "meta": meta,
            }
        )

    for event in window:
        parent = event.parent_id
        if parent and parent in present and parent != event.id:
            edges.append(
                {
                    "id": "e:%s>%s" % (parent, event.id),
                    "from": parent,
                    "to": event.id,
                    "type": event.type,
                }
            )

    return nodes, edges, dropped
