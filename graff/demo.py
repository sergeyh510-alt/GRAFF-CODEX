"""Demo rollout generator (optional ``--demo`` mode).

This module synthesises a Codex CLI rollout that exercises every branch of the
tolerant parser: session metadata, reasoning items, assistant messages, tool
calls with branching, their results, a file edit, an error and a
"reasoning withheld by provider" case.  It exists purely so that GRAFF can be
demonstrated without a live Codex run; real sessions are the default.

It is importable::

    from graff.demo import generate_demo_session
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "C:\\\\IntelliJ IDEA_PROJECT\\\\GRAFF_Codex"

REASONING = [
    "The repository is almost empty, so first I should look at the layout before writing anything.",
    "Let me check the Python runtime and whether a virtual environment already exists in the project.",
    "I need to understand the shape of the rollout records to design a tolerant parser.",
    "The file is appended to while I read it, so a polling tailer that tolerates a partial trailing line is safest.",
    "DeepSeek through RouterAI may not return reasoning items at all, so the graph is derived from messages and tool calls.",
    "The first attempt failed because the console cannot print Cyrillic; I will write the output to a UTF-8 file instead.",
    "Now that parsing works I should build the graph model and make branching visible for parallel tool calls.",
    "I will persist the event journal as JSONL so a run can be reloaded and replayed later.",
    "Everything looks consistent; time to start the server and watch the graph grow in the browser.",
]

MESSAGES = [
    "Repository layout noted: `run.py`, a `graff/` package, `static/` and `scripts/`. I will start with the parser.",
    "I found the rollout format: records are `session_meta`, `event_msg`, `response_item` and `token_usage_record`.",
    "The parser handles unknown record types gracefully and counts them instead of crashing.",
    "Tool calls and their results now branch correctly beneath the step that produced them.",
    "The FastAPI backend is optional; the stdlib backend keeps the demo runnable offline.",
    "The graph updates in real time over WebSocket, with directed and curved edges.",
]

COMMANDS: List[Tuple[str, str]] = [
    ("Get-ChildItem -Force", "Mode   Name\n----   ----\nd----- .venv\nd----- graff\nd----- scripts\nd----- static\n-a---- run.py"),
    ("python --version", "Python 3.14.7"),
    ("python -m pip list", "fastapi      0.143.0\nnetworkx     3.7\nuvicorn      0.54.0\nwatchdog     6.0.0"),
    ("python -c \"import json; print('ok')\"", "ok"),
    ("python -c \"print('\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442')\"", "Traceback (most recent call last):\nUnicodeEncodeError: 'charmap' codec can't encode"),
    ("python -m pytest -q tests", "12 passed in 0.61s"),
    ("git status --short", " M graff/rollout.py\n?? runtime/"),
]

PATCH_TEXT = """*** Begin Patch
*** Add File: graff/graph.py
+from dataclasses import dataclass
+
+@dataclass
+class Event:
+    id: str
+    ts: float
+    actor: str
+    type: str
+    text: str = ""
+    parent_id: str | None = None
*** End Patch"""


def _iso(ts: float) -> str:
    moment = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def _iso(ts: float) -> str:
    moment = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


class DemoWriter:
    def __init__(self, path: str, model: str = "deepseek/deepseek-v3.1") -> None:
        self.path = path
        self.model = model
        self.ordinal = 0
        self.session_id = str(uuid.uuid4())
        self.turn_id = str(uuid.uuid4())
        self.start = time.time()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.handle = open(path, "w", encoding="utf-8", newline="\n")
        self._reasoning_budget = 2

    # -- low level --------------------------------------------------------- #

    def emit(self, record: Dict[str, Any]) -> None:
        record.setdefault("timestamp", _iso(time.time()))
        record["ordinal"] = self.ordinal
        self.ordinal += 1
        self.handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.handle.flush()

    # -- record kinds ------------------------------------------------------ #

    def session_meta(self) -> None:
        self.emit({
            "type": "session_meta",
            "payload": {
                "session_id": self.session_id,
                "id": self.session_id,
                "timestamp": _iso(self.start),
                "cwd": PROJECT_ROOT,
                "originator": "codex_cli",
                "cli_version": "0.153.4",
                "model_provider": "routerai",
                "model": self.model,
                "base_instructions": {"text": "You are a coding agent running in the Codex CLI."},
            },
        })

    def turn_context(self) -> None:
        self.emit({
            "type": "turn_context",
            "payload": {
                "turn_id": self.turn_id,
                "root_turn_id": self.turn_id,
                "cwd": PROJECT_ROOT,
                "workspace_roots": [PROJECT_ROOT],
                "approval_policy": "on-request",
                "sandbox_policy": {"type": "workspace-write", "network_access": False},
            },
        })

    def task_started(self) -> None:
        self.emit({"type": "event_msg", "payload": {"type": "task_started", "turn_id": self.turn_id}})

    def user_message(self, text: str) -> None:
        message_id = "msg_%s" % uuid.uuid4().hex[:22]
        self.emit({
            "type": "response_item",
            "payload": {"type": "message", "id": "u_" + message_id, "role": "user",
                        "content": [{"type": "input_text", "text": text}]},
        })
        self.emit({
            "type": "event_msg",
            "payload": {"type": "item_completed", "thread_id": self.session_id, "turn_id": self.turn_id,
                        "item": {"type": "UserMessage", "id": message_id,
                                 "content": [{"type": "text", "text": text}]}},
        })

    def reasoning(self, text: Optional[str] = None, encrypted: bool = False) -> Optional[str]:
        if text is None:
            text = random.choice(REASONING)
        if encrypted:
            self.emit({
                "type": "response_item",
                "payload": {"type": "reasoning", "id": "rs_%s" % uuid.uuid4().hex[:20],
                            "summary": [], "content": [], "encrypted_content": "Z0FBQUFBQl..."},
            })
            return None
        self.emit({
            "type": "response_item",
            "payload": {"type": "reasoning", "id": "rs_%s" % uuid.uuid4().hex[:20],
                        "summary": [], "content": [{"type": "reasoning_text", "text": text}]},
        })
        return text

    def agent_message(self, text: str) -> None:
        message_id = "msg_%s" % uuid.uuid4().hex[:22]
        self.emit({
            "type": "event_msg",
            "payload": {"type": "item_completed", "thread_id": self.session_id, "turn_id": self.turn_id,
                        "item": {"type": "AgentMessage", "id": message_id,
                                 "content": [{"type": "Text", "text": text}]}},
        })
        self.emit({
            "type": "response_item",
            "payload": {"type": "message", "id": message_id, "role": "assistant",
                        "content": [{"type": "output_text", "text": text}]},
        })

    def function_call(self, name: str, arguments: Dict[str, Any]) -> str:
        call_id = "call_%02d_%s" % (random.randint(0, 9), uuid.uuid4().hex[:20])
        self.emit({
            "type": "response_item",
            "payload": {"type": "function_call", "id": "fc_%s" % uuid.uuid4().hex[:22], "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False), "call_id": call_id},
        })
        return call_id

    def command_item(self, call_id: str, command: str, output: str, exit_code: int = 0) -> None:
        shell = ["C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe", "-Command", command]
        self.emit({
            "type": "event_msg",
            "payload": {"type": "item_completed", "thread_id": self.session_id, "turn_id": self.turn_id,
                        "item": {"type": "CommandExecution", "id": call_id, "process_id": str(random.randint(1000, 9999)),
                                 "command": shell, "cwd": "file:///" + PROJECT_ROOT.replace("\\\\", "/"),
                                 "source": "unified_exec_startup", "status": "completed" if exit_code == 0 else "failed",
                                 "stdout": output, "stderr": "" if exit_code == 0 else output, "exit_code": exit_code}},
        })

    def function_output(self, call_id: str, output: str) -> None:
        self.emit({
            "type": "response_item",
            "payload": {"type": "function_call_output", "id": "fco_%s" % uuid.uuid4().hex[:22],
                        "call_id": call_id, "output": output},
        })

    def token_count(self) -> None:
        self.emit({
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": random.randint(5000, 20000), "output_tokens": random.randint(100, 900),
                "reasoning_output_tokens": random.randint(0, 400), "total_tokens": random.randint(6000, 21000)}}},
        })

    def task_complete(self, message: str) -> None:
        self.emit({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": self.turn_id,
                                                    "last_agent_message": message}})

    def close(self) -> None:
        self.handle.close()

    # -- high level scenarios ---------------------------------------------- #

    def scenario(self, index: int) -> None:
        """Emit one plausible turn of agent activity."""

        # ~ every third turn the provider withholds the reasoning text.
        encrypted = (index % 3 == 2)
        self.reasoning(encrypted=encrypted)
        self.agent_message(random.choice(MESSAGES))

        mode = index % 4
        if mode == 0:
            # branch: two parallel read-only commands, then their results
            first = random.choice(COMMANDS[:3])
            call_a = self.function_call("exec_command", {"cmd": first[0]})
            call_b = self.function_call("exec_command", {"cmd": COMMANDS[3][0]})
            self.command_item(call_a, first[0], first[1])
            self.function_output(call_a, first[1])
            self.command_item(call_b, COMMANDS[3][0], COMMANDS[3][1])
            self.function_output(call_b, COMMANDS[3][1])
        elif mode == 1:
            # a command that fails -> error node
            command, output = COMMANDS[4]
            call_id = self.function_call("exec_command", {"cmd": command})
            self.command_item(call_id, command, output, exit_code=1)
            self.function_output(call_id, output)
            self.reasoning("The console encoding failed, so I will redirect the output to a file.")
            ok_command, ok_output = COMMANDS[5]
            call_id = self.function_call("exec_command", {"cmd": ok_command})
            self.command_item(call_id, ok_command, ok_output)
            self.function_output(call_id, ok_output)
        elif mode == 2:
            # file edit via apply_patch -> file_edit node
            call_id = self.function_call("apply_patch", {"patch": PATCH_TEXT})
            self.command_item(call_id, "apply_patch <<< patch", "Success. Updated the following files:\\nA graff/graph.py")
            self.function_output(call_id, "Success. Updated the following files:\\nA graff/graph.py")
            self.agent_message("Added `graff/graph.py` with the normalized event model.")
        else:
            # single command + a closing summary
            command, output = random.choice(COMMANDS[:3])
            call_id = self.function_call("exec_command", {"cmd": command})
            self.command_item(call_id, command, output)
            self.function_output(call_id, output)
            self.agent_message(random.choice(MESSAGES))

        self.token_count()


def generate_demo_session(
    path: str,
    turns: int = 6,
    interval: float = 0.0,
    keep_open: bool = False,
    model: str = "deepseek/deepseek-v3.1",
    seed: Optional[int] = None,
    on_status: Optional[Any] = None,
) -> str:
    """Write a demo rollout to ``path`` and return the path."""

    if seed is not None:
        random.seed(seed)
    writer = DemoWriter(path, model=model)
    writer.session_meta()
    writer.turn_context()
    writer.task_started()
    writer.user_message(
        "Build the GRAFF visualizer: tail the Codex rollout, turn it into a live graph "
        "and render it in the browser."
    )
    try:
        for index in range(turns):
            writer.scenario(index)
            if interval > 0:
                time.sleep(interval)
        writer.task_complete(random.choice(MESSAGES))
        if keep_open:
            if on_status:
                on_status("session written; keeping the file open for live tailing")
            try:
                while True:
                    time.sleep(interval or 1.0)
                    writer.scenario(random.randint(0, 3))
            except KeyboardInterrupt:
                pass
    finally:
        writer.close()
    return path
