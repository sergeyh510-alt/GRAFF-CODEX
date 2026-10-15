"""Tolerant reader for Codex CLI rollout files (rollout-*.jsonl).

The rollout format is an internal Codex CLI artifact and changes between
versions.  On top of that, when the CLI talks to a third-party "RawAI" style
provider (``wire_api = "responses"``) there may be *no* reasoning items at all or
they may arrive encrypted.  This module therefore follows one rule: never crash
on an unknown record.  Anything that cannot be understood is counted and
skipped, and the graph structure is derived from whatever *is* present
(messages, tool calls and their results).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Sequence

from .graph import Event, clean_text

# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

_WRITE_TOKENS = ("apply_patch", "apply_diff", "write_file", "write", "create_file",
                 "add_file", "new_file", "edit_file", "edit", "replace_in_file",
                 "str_replace", "delete_file", "remove_file", "rename_file")

_IMAGE_MARKERS = ("input_image", "image_url", "image")


def parse_ts(value: Any) -> Optional[float]:
    """Best-effort conversion of any timestamp representation to epoch seconds."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e17:
            return number / 1e9
        if number > 1e14:
            return number / 1e6
        if number > 1e11:
            return number / 1e3
        return number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_ts(int(text))
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = _dt.datetime.fromisoformat(iso)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.timestamp()
    return None


def extract_text(value: Any) -> str:
    """Recursively pull readable text out of the many content shapes Codex uses."""

    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [extract_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        marker = str(value.get("type") or "").lower()
        for key in ("text", "output_text", "input_text", "reasoning_text", "content", "summary"):
            if key in value:
                piece = extract_text(value[key])
                if piece:
                    return piece
        if marker in _IMAGE_MARKERS or "image" in marker:
            return "[image]"
        for key in ("message", "output", "arguments"):
            if key in value:
                piece = extract_text(value[key])
                if piece:
                    return piece
        return ""
    return ""


def _tool_args_summary(name: str, arguments: Any) -> str:
    """Return a compact, human friendly description of a tool call."""

    parsed: Any = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except Exception:
            return clean_text(arguments)

    if isinstance(parsed, dict):
        for key in ("cmd", "command", "path", "file_path", "target_file", "filename"):
            if key in parsed:
                value = parsed[key]
                if isinstance(value, list):
                    value = " ".join(str(v) for v in value)
                return clean_text(value)
        if name in ("apply_patch", "apply_diff"):
            patch = str(parsed.get("patch") or parsed.get("input") or "")
            for line in patch.split("\n"):
                line = line.strip()
                if line.startswith("*** Update File:") or line.startswith("*** Add File:"):
                    return line
                if line and not line.startswith("***"):
                    return line
        try:
            return json.dumps(parsed, ensure_ascii=False)[:400]
        except Exception:
            return str(parsed)[:400]
    return clean_text(parsed)


def _looks_like_write(name: str) -> bool:
    lowered = (name or "").lower()
    if not lowered:
        return False
    if lowered in _WRITE_TOKENS:
        return True
    return any(token in lowered for token in ("patch", "write", "edit", "create", "replace"))


def _hash_id(text: str, length: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:length]


def _status_from_text(text: str) -> str:
    head = (text or "")[:600]
    match = re.search(r"Process exited with code (\d+)", head)
    if match and match.group(1) != "0":
        return "error"
    if "Traceback (most recent call last)" in head:
        return "error"
    if "exec_command failed" in head or "Rejected(" in head:
        return "error"
    if re.search(r"^\s*(error|fatal)\b", head, re.IGNORECASE | re.MULTILINE):
        return "error"
    return "ok"


_REVIEWER_MARKERS = (
    "whose request action you are assessing",
    ">>> TRANSCRIPT START",
    "You are assessing whether the following",
)


def _is_reviewer_noise(text: str) -> bool:
    """Filter Codex' own approval-reviewer transcripts out of the user stream."""

    head = (text or "")[:400]
    return any(marker in head for marker in _REVIEWER_MARKERS)


# --------------------------------------------------------------------------- #
# record -> event parser
# --------------------------------------------------------------------------- #

class RolloutParser:
    """Stateful, tolerant translator of raw rollout records into events.

    The parser keeps a bounded set of seen ids so that ``response_item`` records
    (which duplicate the richer ``event_msg/item_completed`` records) are not
    turned into duplicate nodes.  It also remembers, per tool ``call_id``, which
    node represents the call so that a later result can be attached to it.
    """

    def __init__(self) -> None:
        self.seq = 0
        self._seen: set[str] = set()
        self._seen_order: deque = deque()
        self._call_parent: Dict[str, str] = {}
        self._call_names: Dict[str, str] = {}
        self._user_seen: set[str] = set()
        self._user_order: deque = deque()
        self._last_node_id: Optional[str] = None

    # -- id bookkeeping ---------------------------------------------------- #

    def _seen_before(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > 20000:
            self._seen.discard(self._seen_order.popleft())
        return False

    def _register(self, key: Any, node_id: str) -> None:
        if key is None:
            return
        self._call_parent[str(key)] = node_id
        self._seen.add(str(key))
        self._seen_order.append(str(key))
        if len(self._seen_order) > 20000:
            self._seen.discard(self._seen_order.popleft())

    def _resolve_parent(self, call_id: Any) -> Optional[str]:
        if call_id is not None:
            known = self._call_parent.get(str(call_id))
            if known:
                return known
        return self._last_node_id

    # -- public API -------------------------------------------------------- #

    def parse(self, raw: Dict[str, Any]) -> Optional[Event]:
        if not isinstance(raw, dict):
            return None
        rtype = str(raw.get("type") or "")
        payload = raw.get("payload")

        if rtype == "response_item":
            return self._from_response_item(raw, payload)
        if rtype == "event_msg":
            return self._from_event_msg(raw, payload)
        if rtype == "compacted":
            return self._from_compacted(raw)
        # session_meta / turn_context / world_state / token_usage_record and any
        # future record type: intentionally ignored (tolerant by design).
        return None

    # -- response_item ----------------------------------------------------- #

    def _from_response_item(self, raw: Dict[str, Any], payload: Any) -> Optional[Event]:
        if not isinstance(payload, dict):
            return None
        ptype = str(payload.get("type") or "")
        payload_id = payload.get("id")

        if payload_id is not None and self._seen_before(str(payload_id)):
            return None

        if ptype == "reasoning":
            return self._emit(
                raw, actor="codex", etype="reason",
                text=self._reasoning_text(payload), key=payload_id,
            )

        if ptype == "message":
            role = str(payload.get("role") or "assistant").lower()
            if role in ("developer", "system"):
                return None  # harness plumbing, not part of the visible graph
            text = extract_text(payload.get("content"))
            if _is_reviewer_noise(text):
                return None
            actor = "user" if role == "user" else "codex"
            return self._emit(raw, actor=actor, etype="message", text=text, key=payload_id)

        if ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            name = str(payload.get("name") or payload.get("tool") or "tool")
            arguments = payload.get("arguments")
            if arguments is None:
                arguments = payload.get("input")
            summary = _tool_args_summary(name, arguments) or name
            call_id = payload.get("call_id")
            event = self._emit(
                raw, actor="codex", etype="tool_call", text=summary, key=payload_id,
                extra={"name": name, "args": arguments, "call_id": call_id},
            )
            if event is not None and call_id is not None:
                self._register(str(call_id), event.id)
            return event

        if ptype in ("function_call_output", "custom_tool_call_output", "tool_result"):
            call_id = payload.get("call_id")
            output = payload.get("output")
            if output is None:
                output = payload.get("content")
            text = extract_text(output)
            status = _status_from_text(text)
            name = self._tool_name_for(call_id)
            etype = "file_edit" if _looks_like_write(name) and status == "ok" else "tool_result"
            if status == "error":
                etype = "error"
            return self._emit(
                raw, actor="codex", etype=etype, text=text, key=payload_id,
                parent=self._resolve_parent(call_id),
                extra={"call_id": call_id, "name": name, "status": status},
            )

        # Unknown response_item payload type: ignore quietly.
        return None

    def _from_event_msg(self, raw: Dict[str, Any], payload: Any) -> Optional[Event]:
        if not isinstance(payload, dict):
            return None
        ptype = str(payload.get("type") or "")

        if ptype == "item_completed":
            item = payload.get("item")
            return self._from_item(raw, payload, item) if isinstance(item, dict) else None

        if ptype == "user_message":
            text = extract_text(payload.get("message") or payload.get("text"))
            if _is_reviewer_noise(text):
                return None
            return self._emit(raw, actor="user", etype="message", text=text, key=payload.get("id"))

        if ptype in ("error", "stream_error", "turn_error"):
            return self._emit(raw, actor="codex", etype="error",
                              text=extract_text(payload.get("message") or payload.get("error") or payload))

        # task_started / task_complete / token_count / thread_settings_applied ...
        return None

    def _from_item(self, raw: Dict[str, Any], payload: Dict[str, Any], item: Dict[str, Any]) -> Optional[Event]:
        itype = str(item.get("type") or "")
        item_id = item.get("id")
        if item_id is not None and self._seen_before(str(item_id)):
            return None

        if itype == "UserMessage":
            return self._emit(raw, actor="user", etype="message", text=extract_text(item.get("content")), key=item_id)

        if itype == "AgentMessage":
            text = extract_text(item.get("content"))
            actor = str(item.get("actor") or "codex").lower()
            if actor not in ("codex", "rowboat", "user"):
                actor = "codex"
            return self._emit(raw, actor=actor, etype="message", text=text, key=item_id)

        if itype == "Reasoning":
            return self._emit(raw, actor="codex", etype="reason", text=self._reasoning_text(item), key=item_id)

        if itype in ("CommandExecution", "CommandExecutionResult"):
            return self._from_command(raw, item)

        if itype in ("FileEdit", "FileChange", "PatchApply", "PatchApplied"):
            return self._emit(
                raw, actor="codex", etype="file_edit", text=self._file_edit_text(item), key=item_id,
                parent=self._resolve_parent(item.get("call_id") or item_id), extra={"name": "file_edit"},
            )

        if itype in ("McpToolCall", "LocalShellCall", "ToolCall", "FunctionCall", "CustomToolCall"):
            name = str(item.get("name") or item.get("tool") or itype)
            arguments = item.get("arguments") or item.get("input")
            summary = _tool_args_summary(name, arguments) or name
            call_id = item.get("call_id") or item_id
            event = self._emit(raw, actor="codex", etype="tool_call", text=summary, key=item_id,
                               extra={"name": name, "args": arguments, "call_id": call_id})
            if event is not None and call_id is not None:
                self._register(str(call_id), event.id)
            return event

        if itype in ("Error", "TaskError"):
            return self._emit(raw, actor="codex", etype="error",
                              text=extract_text(item.get("message") or item.get("error") or item), key=item_id)

        if itype == "ImageView":
            return self._emit(raw, actor="codex", etype="tool_result",
                              text=extract_text(item.get("path") or item.get("image") or item), key=item_id,
                              extra={"name": "image"})

        if itype == "ContextCompaction":
            return self._emit(raw, actor="codex", etype="message", text="[context compacted]", key=item_id,
                              extra={"name": "compaction"})

        # Unknown item type: still show it so nothing silently disappears.
        return self._emit(raw, actor="codex", etype="message", text=itype, key=item_id)

    def _from_command(self, raw: Dict[str, Any], item: Dict[str, Any]) -> Event:
        call_id = item.get("id") or item.get("call_id")
        command = item.get("command")
        if isinstance(command, list):
            command_text = " ".join(str(part) for part in command)
            if len(command) >= 3 and command[0].lower().endswith(("powershell.exe", "cmd.exe", "bash", "sh")):
                command_text = " ".join(str(part) for part in command[2:])
        else:
            command_text = str(command or "")

        stdout = item.get("stdout") or item.get("aggregated_output") or ""
        stderr = item.get("stderr") or ""
        exit_code = item.get("exit_code")
        if exit_code is None:
            exit_code = item.get("exitCode")
        status = str(item.get("status") or "")
        failed = bool(stderr.strip()) or (isinstance(exit_code, int) and exit_code != 0) or status in ("failed", "error")

        lines = []
        if command_text:
            lines.append("$ " + clean_text(command_text))
        body = clean_text(stdout) or clean_text(stderr)
        if body:
            lines.append(body)
        elif failed:
            lines.append("command failed")
        text = "\n".join(lines)

        etype = "error" if failed else ("file_edit" if _looks_like_write(command_text) else "tool_result")
        return self._emit(
            raw, actor="codex", etype=etype, text=text,
            parent=self._resolve_parent(call_id), key=call_id,
            extra={"call_id": call_id, "name": "exec_command",
                   "status": status or ("failed" if failed else "completed")},
        )

    def _from_compacted(self, raw: Dict[str, Any]) -> Optional[Event]:
        text = extract_text(raw.get("message") or raw.get("payload"))
        if not text:
            return None
        return self._emit(raw, actor="codex", etype="message", text=text,
                          key="compacted@%s" % raw.get("ordinal"), extra={"name": "compaction"})

    # -- shared helpers ---------------------------------------------------- #

    def _emit(
        self,
        raw: Dict[str, Any],
        actor: str,
        etype: str,
        text: str = "",
        parent: Optional[str] = None,
        key: Any = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Event]:
        if actor == "user" and etype == "message":
            fingerprint = _hash_id(clean_text(text)[:4000], 16)
            if fingerprint in self._user_seen:
                return None
            self._user_seen.add(fingerprint)
            self._user_order.append(fingerprint)
            if len(self._user_order) > 2000:
                self._user_seen.discard(self._user_order.popleft())

        ordinal = raw.get("ordinal")
        ts = parse_ts(raw.get("timestamp"))
        if ts is None:
            ts = parse_ts(raw.get("ts")) or 0.0
        event_id = "n%d" % ordinal if isinstance(ordinal, int) else "h" + _hash_id(
            "%s|%s|%s|%s" % (actor, etype, text[:200], ts))

        payload_extra: Dict[str, Any] = {"ordinal": ordinal}
        if extra:
            payload_extra.update({k: v for k, v in extra.items() if v is not None})

        if parent is None:
            if etype in ("tool_result", "file_edit", "error"):
                parent = self._last_node_id
            elif etype == "tool_call":
                parent = self._last_node_id
            elif etype in ("reason", "message") and actor != "user":
                parent = self._last_node_id

        if etype == "tool_call":
            self._register_name(payload_extra.get("call_id"), str(payload_extra.get("name") or ""))
            self._last_node_id = event_id
        elif actor != "user":
            self._last_node_id = event_id

        return Event(
            id=event_id,
            ts=float(ts or 0.0),
            actor=actor,
            type=etype,
            text=text,
            parent_id=parent,
            extra=payload_extra,
        )

    def _reasoning_text(self, payload: Dict[str, Any]) -> str:
        text = extract_text(payload.get("content"))
        if text:
            return text
        text = extract_text(payload.get("summary"))
        if text:
            return text
        if payload.get("encrypted_content") or payload.get("encrypted"):
            return "[reasoning encrypted by provider]"
        return ""

    def _tool_name_for(self, call_id: Any) -> str:
        if call_id is None:
            return ""
        return self._call_names.get(str(call_id), "")

    def _register_name(self, call_id: Any, name: str) -> None:
        if call_id is not None and name:
            self._call_names[str(call_id)] = name

    def _file_edit_text(self, item: Dict[str, Any]) -> str:
        path = item.get("path") or item.get("file") or item.get("filename") or ""
        diff = item.get("diff") or item.get("patch") or ""
        if path and diff:
            return "%s\n%s" % (path, clean_text(diff))
        return clean_text(path or diff or item.get("content") or "file edit")




# --------------------------------------------------------------------------- #
# session discovery, statistics and selection
# --------------------------------------------------------------------------- #

DEFAULT_SESSIONS_ROOT = os.path.join(os.path.expanduser("~"), ".codex", "sessions")

# A session is considered "contentful" (worth opening by default) when it has at
# least one tool invocation or at least this many graph events.
CONTENTFUL_MIN_TOOL_CALLS = 1
CONTENTFUL_MIN_EVENTS = 20


class SessionInfo:
    """Lightweight, cheap description of one rollout file."""

    __slots__ = ("path", "name", "size", "mtime", "lines", "events", "tool_calls", "reason", "contentful")

    def __init__(self, path: str, size: int, mtime: float, lines: int = 0,
                 events: int = 0, tool_calls: int = 0, reason: str = "") -> None:
        self.path = path
        self.name = os.path.basename(path)
        self.size = size
        self.mtime = mtime
        self.lines = lines
        self.events = events
        self.tool_calls = tool_calls
        self.reason = reason
        self.contentful = tool_calls >= CONTENTFUL_MIN_TOOL_CALLS or events >= CONTENTFUL_MIN_EVENTS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "mtime": self.mtime,
            "lines": self.lines,
            "events": self.events,
            "tool_calls": self.tool_calls,
            "reason": self.reason,
            "contentful": self.contentful,
        }

    @property
    def timestamp(self) -> float:
        """Best-effort session start time parsed from the file name."""

        match = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})", self.name)
        if not match:
            return self.mtime
        try:
            moment = _dt.datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4)), int(match.group(5)), int(match.group(6)),
                tzinfo=_dt.timezone.utc,
            )
        except ValueError:
            return self.mtime
        return moment.timestamp()


def list_session_paths(root: str = DEFAULT_SESSIONS_ROOT, limit: Optional[int] = None) -> List[str]:
    """Return rollout file paths newest-first (by mtime)."""

    found: List[str] = []
    if not os.path.isdir(root):
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                found.append(os.path.join(dirpath, name))
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    if limit is not None:
        found = found[:limit]
    return found


# Backwards compatible alias.
def list_sessions(root: str = DEFAULT_SESSIONS_ROOT, limit: Optional[int] = None) -> List[str]:
    return list_session_paths(root, limit=limit)


def scan_session(path: str) -> SessionInfo:
    """Count lines, graph events and tool calls in a rollout file.

    This is tolerant: unreadable or malformed records only reduce the counts.
    """

    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return SessionInfo(path, 0, 0.0)

    lines = events = tool_calls = 0
    reason = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                rtype = record.get("type")
                payload = record.get("payload")
                if rtype == "response_item" and isinstance(payload, dict):
                    ptype = payload.get("type")
                    if ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                        events += 1
                        tool_calls += 1
                        continue
                    if ptype in ("reasoning", "message", "function_call_output", "custom_tool_call_output"):
                        if ptype == "message" and isinstance(payload.get("role"), str) and \
                                str(payload.get("role")).lower() in ("developer", "system"):
                            continue
                        events += 1
                        continue
                elif rtype == "event_msg" and isinstance(payload, dict):
                    ptype = payload.get("type")
                    if ptype == "item_completed":
                        item = payload.get("item")
                        if isinstance(item, dict):
                            itype = item.get("type")
                            if itype in ("CommandExecution", "McpToolCall", "LocalShellCall", "ToolCall",
                                         "FunctionCall", "CustomToolCall", "FileEdit", "FileChange",
                                         "PatchApply", "PatchApplied"):
                                events += 1
                                if itype in ("McpToolCall", "LocalShellCall", "ToolCall", "FunctionCall",
                                             "CustomToolCall"):
                                    tool_calls += 1
                                continue
                            if itype in ("UserMessage", "AgentMessage", "Reasoning", "ImageView",
                                         "ContextCompaction", "Error", "TaskError"):
                                events += 1
                                continue
                    elif ptype == "user_message":
                        events += 1
                        continue
    except OSError as exc:
        reason = str(exc)

    return SessionInfo(path, size, mtime, lines, events, tool_calls, reason)


def summarise_sessions(
    root: str = DEFAULT_SESSIONS_ROOT,
    limit: int = 40,
) -> List[SessionInfo]:
    """Scan and summarise the newest ``limit`` sessions (newest-first)."""

    infos = [scan_session(path) for path in list_session_paths(root, limit=limit)]
    infos.sort(key=lambda info: info.mtime, reverse=True)
    return infos


def latest_session(
    root: str = DEFAULT_SESSIONS_ROOT,
    require_content: bool = True,
    scan_limit: int = 60,
) -> Optional[str]:
    """Return the newest session that actually contains something to show.

    With ``require_content`` (the default) degenerate sessions that contain no
    tool calls and only a couple of events are skipped, so ``--session latest``
    never lands the user on an empty canvas.
    """

    paths = list_session_paths(root, limit=scan_limit)
    if not paths:
        return None
    if not require_content:
        return paths[0]
    fallback: Optional[str] = None
    for path in paths:
        info = scan_session(path)
        if info.contentful:
            return path
        if fallback is None and info.events > 0:
            fallback = path
    return fallback or paths[0]


def get_session_by_index(root: str = DEFAULT_SESSIONS_ROOT, index: int = 0) -> Optional[str]:
    """Return the ``index``-th newest session path (0-based)."""

    paths = list_session_paths(root)
    if index < 0:
        index += len(paths)
    if 0 <= index < len(paths):
        return paths[index]
    return None


def resolve_session(
    spec: Optional[str],
    root: str = DEFAULT_SESSIONS_ROOT,
    require_content: bool = True,
    index_from_sessions: Optional[List[str]] = None,
) -> Optional[str]:
    """Turn a CLI/UI ``--session`` value into a concrete rollout path.

    Supported forms:

    * ``None`` / ``latest`` / ``auto`` / ``newest`` - newest *contentful* session
    * ``index:N`` - the N-th newest session (0-based, N may be negative)
    * ``first`` / ``oldest`` - the oldest session
    * ``path`` - an explicit path (absolute, or relative to the CWD)
    """

    if not spec or spec.lower() in ("latest", "auto", "newest"):
        return latest_session(root, require_content=require_content)
    lowered = spec.strip().lower()
    if lowered in ("first", "oldest"):
        paths = list_session_paths(root)
        return paths[-1] if paths else None
    match = re.match(r"^(?:index|#)\s*:?\s*(-?\d+)$", lowered)
    if match:
        return get_session_by_index(root, int(match.group(1)))
    return os.path.abspath(os.path.expanduser(spec))


class SessionTailer:
    """Follow a growing JSONL file and emit normalized events.

    The reader is intentionally polling based: it is robust to the file being
    appended to, truncated or replaced, and it never blocks on a still-open
    handle.  A partial trailing line (the file being written right now) is kept
    in the buffer until its newline arrives.
    """

    def __init__(
        self,
        path: str,
        on_event: Callable[[Event], None],
        parser: Optional[RolloutParser] = None,
        poll_interval: float = 0.4,
        on_unknown: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.path = path
        self.on_event = on_event
        self.parser = parser or RolloutParser()
        self.poll_interval = poll_interval
        self.on_unknown = on_unknown
        self._buffer = ""
        self._pos = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.lines_read = 0
        self.parse_errors = 0
        self.unknown_records = 0
        self.bytes_read = 0
        self.finished = False

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="graff-tailer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def read_available(self) -> int:
        """Consume whatever new complete lines exist.  Returns events emitted."""

        emitted = 0
        with self._lock:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                return 0

            if size < self._pos:
                # File truncated or replaced: start over from the beginning.
                self._pos = 0
                self._buffer = ""

            if size == self._pos:
                return 0

            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._pos)
                chunk = handle.read()
                self._pos = handle.tell()
            self.bytes_read = self._pos

            self._buffer += chunk
            # Split the whole chunk in ONE pass.  Splitting the growing buffer
            # with `split("\n", 1)` inside a loop is O(n^2) in the file size,
            # which made a multi-megabyte session take ~a minute to load.
            if "\n" in self._buffer:
                lines = self._buffer.split("\n")
                self._buffer = lines.pop()   # trailing partial line, if any
                for line in lines:
                    emitted += self._consume_line(line)
        return emitted

    # -- internals --------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.read_available()
            except Exception:  # never let the tailer die
                self.parse_errors += 1
            self._stop.wait(self.poll_interval)

    def _consume_line(self, line: str) -> int:
        line = line.strip()
        if not line:
            return 0
        self.lines_read += 1
        try:
            raw = json.loads(line)
        except Exception:
            self.parse_errors += 1
            return 0
        if not isinstance(raw, dict):
            self.unknown_records += 1
            return 0
        if self.on_unknown is not None and not self._is_known_record(raw):
            self.unknown_records += 1
            try:
                self.on_unknown(raw)
            except Exception:
                pass
        try:
            event = self.parser.parse(raw)
        except Exception:
            self.parse_errors += 1
            return 0
        if event is None:
            return 0
        try:
            self.on_event(event)
        except Exception:
            self.parse_errors += 1
            return 0
        return 1

    @staticmethod
    def _is_known_record(raw: Dict[str, Any]) -> bool:
        rtype = raw.get("type")
        return rtype in ("response_item", "event_msg", "compacted", "session_meta",
                         "turn_context", "world_state", "token_usage_record")
