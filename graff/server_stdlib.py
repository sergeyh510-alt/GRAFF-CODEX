"""Standard-library HTTP + WebSocket server backend.

This backend has no third-party dependencies at all, so ``python run.py`` works
even offline.  It implements just enough of HTTP/1.1 and RFC6455 to serve the
single-page frontend, the JSON API and the live WebSocket stream.
"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .api import GraffApi
from .static_files import NOSNIFF, cache_control_for, read_static
from .ws import WSConnection, compute_accept


class GraffHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GRAFF/0.2"

    # -- plumbing ----------------------------------------------------------- #

    @property
    def api(self) -> GraffApi:
        return self.server.api  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.server.quiet:  # type: ignore[attr-defined]
            return
        super().log_message(fmt, *args)

    def _send_bytes(self, body: bytes, status: int = 200, content_type: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control_for(self.path))
        self.send_header("X-Content-Type-Options", NOSNIFF)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", NOSNIFF)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    # -- routing ------------------------------------------------------------ #

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/api/session", "/api/enrich", "/api/delete", "/api/follow"):
            try:
                payload = json.loads(self._read_body().decode("utf-8") or "{}")
            except Exception:
                payload = {}
            if path == "/api/session":
                spec = str(payload.get("spec") or payload.get("session") or "latest")
                self._send_json(self.api.open_session(spec))
                return
            if path == "/api/enrich":
                try:
                    limit = int(payload.get("limit") or 40)
                except Exception:
                    limit = 40
                self._send_json(self.api.enrich(limit))
                return
            if path == "/api/follow":
                self._send_json(self.api.set_follow(bool(payload.get("follow", True))))
                return
            event_id = str(payload.get("id") or payload.get("event_id") or "")
            cascade = bool(payload.get("cascade", True))
            self._send_json(self.api.delete(event_id, cascade))
            return
        self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/ws":
            self._handle_ws()
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path == "/api/graph":
            window = float(query.get("window", ["0"])[0] or 0)
            types = query.get("types", [None])[0]
            self._send_json(self.api.snapshot(window=window, types=types))
            return
        if path == "/api/status":
            self._send_json(self.api.status())
            return
        if path == "/api/ai":
            self._send_json(self.api.ai_status())
            return
        if path == "/api/log":
            self._send_json(self.api.log())
            return
        if path == "/api/state":
            self._send_json(self.api.state())
            return
        if path == "/api/sessions":
            limit = int(query.get("limit", ["40"])[0] or 40)
            self._send_json(self.api.sessions(limit=limit))
            return
        if path == "/api/graph/export":
            kind = query.get("kind", ["json"])[0]
            self._send_json(self.api.save(kind))
            return
        if path.startswith("/static/"):
            self._serve_static(unquote(path[len("/static/"):]))
            return
        if path == "/favicon.ico":
            self._send_bytes(b"", status=204)
            return
        # Anything else: fall back to the single page app.
        self._serve_static("index.html")

    # -- static files ------------------------------------------------------- #

    def _serve_static(self, relative: str) -> None:
        # Content-type comes from graff.static_files (NOT mimetypes, which on
        # Windows reports text/plain for .js).  JAVASCRIPT_* / CSS are the
        # important ones here.
        body, ctype, cache, status = read_static(self.api.static_dir, relative)
        if body is None:
            error = "forbidden" if status == 403 else "not found"
            self._send_json({"ok": False, "error": error, "path": relative}, status=status)
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", NOSNIFF)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- websocket ---------------------------------------------------------- #

    def _handle_ws(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json({"ok": False, "error": "expected websocket upgrade"}, status=400)
            return

        accept = compute_accept(key)
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        )
        self.wfile.write(handshake.encode("ascii"))
        self.wfile.flush()
        self.close_connection = True

        connection = WSConnection(self.connection, self.rfile)
        engine = self.api.engine

        outbox: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=4000)

        def on_message(message: Dict[str, Any]) -> None:
            try:
                outbox.put_nowait(message)
            except queue.Full:
                try:
                    outbox.get_nowait()
                    outbox.put_nowait(message)
                except Exception:
                    pass

        token = engine.hub.subscribe(on_message)
        alive = threading.Event()
        alive.set()

        def reader() -> None:
            try:
                while alive.is_set():
                    text = connection.recv()
                    if text is None:
                        break
                    try:
                        message = json.loads(text)
                    except Exception:
                        continue
                    kind = message.get("type")
                    if kind == "ping":
                        connection.send_json({"type": "pong", "t": message.get("t")})
                    elif kind == "open_session":
                        engine.open_session(message.get("session") or "latest",
                                            remember=True, reason="открыта вручную")
                        connection.send_json(engine.bootstrap())
                    elif kind == "set_follow":
                        engine.set_follow(bool(message.get("follow", True)))
                        connection.send_json({"type": "follow", "follow": engine.get_follow()})
            except Exception:
                pass
            finally:
                alive.clear()

        try:
            connection.send_json(self.api.bootstrap())
            thread = threading.Thread(target=reader, name="graff-ws-reader", daemon=True)
            thread.start()
            while alive.is_set():
                try:
                    message = outbox.get(timeout=0.4)
                except queue.Empty:
                    continue
                if not connection.send_json(message):
                    break
        finally:
            alive.clear()
            engine.hub.unsubscribe(token)
            connection.close()


def serve(
    host: str,
    port: int,
    engine,
    api: GraffApi,
    quiet: bool = False,
) -> None:
    """Run the stdlib server until interrupted."""

    server = ThreadingHTTPServer((host, port), GraffHandler)
    server.daemon_threads = True
    server.api = api  # type: ignore[attr-defined]
    server.quiet = quiet  # type: ignore[attr-defined]

    engine.start()
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.shutdown()
        server.server_close()
        engine.stop()
