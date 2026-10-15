"""FastAPI + uvicorn backend (preferred when the dependencies are installed).

Enabled automatically by ``run.py`` when ``fastapi`` and ``uvicorn`` are
importable.  The stdlib backend in :mod:`graff.server_stdlib` exposes the exact
same JSON protocol, so the frontend is identical for both.
"""
import asyncio
from typing import Any, Dict, Optional

from .api import GraffApi

# NOTE: deliberately no "from __future__ import annotations" here. FastAPI
# resolves parameter annotations at runtime; the Request/WebSocket types live
# in local scope inside create_app(), so deferred string annotations would not
# resolve and the request body would be mistaken for a query parameter.


def _require():
    try:
        from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse, Response
        import uvicorn
    except Exception as exc:  # pragma: no cover - only hit when deps are missing
        raise RuntimeError(
            "FastAPI backend requested but dependencies are missing: %s" % exc
        )
    return (FastAPI, Request, WebSocket, WebSocketDisconnect,
            FileResponse, Response, uvicorn)


def create_app(engine, api: GraffApi):
    (FastAPI, Request, WebSocket, WebSocketDisconnect,
     FileResponse, Response, _uvicorn) = _require()

    from .static_files import NOSNIFF, cache_control_for, content_type_for, read_static

    app = FastAPI(title="GRAFF", version="0.2")

    @app.get("/api/graph")
    async def graph(window: float = 0.0, types: Optional[str] = None):
        return api.snapshot(window=window, types=types)

    @app.get("/api/status")
    async def status():
        return api.status()

    @app.get("/api/sessions")
    async def sessions(limit: int = 40):
        return api.sessions(limit=limit)

    @app.get("/api/ai")
    async def ai_status():
        return api.ai_status()

    @app.get("/api/graph/export")
    async def export(kind: str = "json"):
        return api.save(kind)

    @app.post("/api/session")
    async def open_session(request: Request):
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        spec = str(payload.get("spec") or payload.get("session") or "latest")
        return api.open_session(spec)

    @app.post("/api/follow")
    async def set_follow(request: Request):
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return api.set_follow(bool(payload.get("follow", True)))

    @app.get("/api/log")
    async def log():
        return api.log()

    @app.get("/api/state")
    async def state():
        return api.state()

    @app.post("/api/enrich")
    async def enrich(request: Request):
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        limit = payload.get("limit") or 40
        try:
            limit = int(limit)
        except Exception:
            limit = 40
        # Enrichment performs blocking HTTP; keep the event loop free.
        return await asyncio.to_thread(api.enrich, limit)

    @app.post("/api/delete")
    async def delete(request: Request):
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        event_id = str(payload.get("id") or payload.get("event_id") or "")
        cascade = bool(payload.get("cascade", True))
        return api.delete(event_id, cascade)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        loop = asyncio.get_running_loop()
        outbox: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=4000)

        def on_message(message: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(_offer, outbox, message)

        token = engine.hub.subscribe(on_message)

        async def pump() -> None:
            while True:
                message = await outbox.get()
                await socket.send_json(message)

        pump_task = asyncio.create_task(pump())
        try:
            await socket.send_json(api.bootstrap())
            while True:
                message = await socket.receive_json()
                kind = message.get("type")
                if kind == "ping":
                    await socket.send_json({"type": "pong", "t": message.get("t")})
                elif kind == "open_session":
                    result = await asyncio.to_thread(api.open_session, message.get("session") or "latest")
                    await socket.send_json({"type": "session_opened", "result": result})
                    await socket.send_json(api.bootstrap())
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            pump_task.cancel()
            engine.hub.unsubscribe(token)

    @app.get("/static/{path:path}")
    async def static_file(path: str):
        # Serve our own assets with the explicit graff.static_files table.
        # fastapi.staticfiles.StaticFiles is deliberately NOT used: it relies on
        # mimetypes, which on Windows resolves .js to text/plain (the Windows
        # registry maps .js to text/plain), so the browser refused to run the
        # frontend scripts.
        body, ctype, cache, status = read_static(api.static_dir, path)
        if body is None:
            import json as _json

            error = "forbidden" if status == 403 else "not found"
            payload = _json.dumps({"ok": False, "error": error, "path": path},
                                  ensure_ascii=False).encode("utf-8")
            return Response(content=payload, status_code=status,
                            media_type="application/json; charset=utf-8",
                            headers={"Cache-Control": cache, "X-Content-Type-Options": NOSNIFF})
        return Response(content=body, media_type=ctype,
                        headers={"Cache-Control": cache, "X-Content-Type-Options": NOSNIFF})

    @app.get("/")
    async def index():
        return FileResponse(
            _join(api.static_dir, "index.html"),
            media_type=content_type_for("index.html"),
            headers={"Cache-Control": cache_control_for("index.html"),
                     "X-Content-Type-Options": NOSNIFF},
        )

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    return app


def _offer(queue: "asyncio.Queue", message: Dict[str, Any]) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(message)
        except Exception:
            pass


def _join(root: str, name: str) -> str:
    import os

    return os.path.join(root, name)


def serve(host: str, port: int, engine, api: GraffApi, quiet: bool = False):
    (_, _, _, _, _, _, uvicorn) = _require()
    engine.start()
    app = create_app(engine, api)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning" if quiet else "info")
    finally:
        engine.stop()
