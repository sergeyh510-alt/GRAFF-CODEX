"""Minimal RFC6455 WebSocket helper for the standard-library server.

When the ``websockets`` package is installed the FastAPI backend is preferred.
This module exists so that ``python run.py`` still streams live updates with no
third-party dependency, using the same JSON protocol.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from typing import Any, Dict, Optional, Tuple

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def websockets_available() -> bool:
    try:
        import websockets  # noqa: F401
    except Exception:
        return False
    return True


def compute_accept(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _build_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + payload


def _read_exact(rfile, count: int) -> Optional[bytes]:
    chunks = bytearray()
    while len(chunks) < count:
        piece = rfile.read(count - len(chunks))
        if not piece:
            return None
        chunks += piece
    return bytes(chunks)


def _read_frame(rfile) -> Optional[Tuple[int, bytes]]:
    header = _read_exact(rfile, 2)
    if header is None:
        return None
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        extra = _read_exact(rfile, 2)
        if extra is None:
            return None
        length = struct.unpack(">H", extra)[0]
    elif length == 127:
        extra = _read_exact(rfile, 8)
        if extra is None:
            return None
        length = struct.unpack(">Q", extra)[0]
    mask = _read_exact(rfile, 4) if masked else b"\x00\x00\x00\x00"
    if mask is None:
        return None
    payload = _read_exact(rfile, length) if length else b""
    if payload is None:
        return None
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


class WSConnection:
    """A single server-side WebSocket connection."""

    def __init__(self, sock: socket.socket, rfile) -> None:
        self.sock = sock
        self.rfile = rfile
        self._send_lock = threading.Lock()
        self.closed = False

    def send_text(self, text: str) -> bool:
        if self.closed:
            return False
        try:
            with self._send_lock:
                self.sock.sendall(_build_frame(text.encode("utf-8"), OP_TEXT))
            return True
        except Exception:
            self.closed = True
            return False

    def send_json(self, message: Dict[str, Any]) -> bool:
        return self.send_text(json.dumps(message, ensure_ascii=False))

    def recv(self) -> Optional[str]:
        """Block until a text message arrives.  Returns None when closed."""

        while not self.closed:
            frame = _read_frame(self.rfile)
            if frame is None:
                self.closed = True
                return None
            opcode, payload = frame
            if opcode == OP_TEXT:
                return payload.decode("utf-8", "replace")
            if opcode == OP_CLOSE:
                self.close(send_close=False)
                return None
            if opcode == OP_PING:
                try:
                    with self._send_lock:
                        self.sock.sendall(_build_frame(payload, OP_PONG))
                except Exception:
                    self.closed = True
                    return None
        return None

    def close(self, send_close: bool = True) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if send_close:
                with self._send_lock:
                    self.sock.sendall(_build_frame(b"", OP_CLOSE))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
