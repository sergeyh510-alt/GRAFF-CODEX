#!/usr/bin/env python
"""GRAFF entry point.

Usage:
    python run.py [--session latest|<path>|index:N] [--port 8756]
    python run.py --list-sessions
    python run.py --demo                      # optional synthetic session

Real Codex rollout sessions are the default source.  The FastAPI/uvicorn backend
is used automatically when those packages are importable, otherwise the server
transparently falls back to the standard library.
"""
from __future__ import annotations

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from graff import __version__
from graff.api import GraffApi
from graff.engine import LiveEngine
from graff.paths import default_runtime_dir, is_frozen, resource_dir
from graff.rollout import DEFAULT_SESSIONS_ROOT


def have_fastapi() -> bool:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except Exception:
        return False
    return True


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="graff",
        description="Live graph of Codex reasoning and actions from rollout sessions.",
    )
    parser.add_argument("--session", default=None,
                        help="'latest' (newest contentful session), 'index:N', 'first', "
                             "or an explicit path to a rollout-*.jsonl file.  When omitted "
                             "the session saved in runtime/state.json is restored.")
    parser.add_argument("--port", type=int, default=8756, help="HTTP port (default: 8756)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--sessions-root", default=DEFAULT_SESSIONS_ROOT,
                        help="root directory of Codex sessions")
    parser.add_argument("--backend", choices=("auto", "fastapi", "stdlib"), default="auto",
                        help="web backend to use (default: auto)")
    parser.add_argument("--node-cap", type=int, default=8000, help="maximum nodes sent to the browser")
    parser.add_argument("--runtime", default=None,
                        help="directory for journal/snapshots/exports (default: <project>/runtime)")
    parser.add_argument("--no-journal", action="store_true", help="disable the JSONL event journal")
    parser.add_argument("--poll", type=float, default=0.4, help="session polling interval in seconds")
    parser.add_argument("--list-sessions", action="store_true",
                        help="print a table of recent sessions and exit")
    parser.add_argument("--limit", type=int, default=20, help="rows for --list-sessions (default: 20)")
    parser.add_argument("--demo", action="store_true",
                        help="optional demo mode: generate a synthetic session and watch it live")
    parser.add_argument("--demo-turns", type=int, default=6, help="turns for --demo (default: 6)")
    parser.add_argument("--demo-interval", type=float, default=1.2,
                        help="seconds between demo turns (default: 1.2)")
    parser.add_argument("--demo-seed", type=int, default=None, help="random seed for --demo")
    parser.add_argument("--enrich", action="store_true",
                        help="enable optional AI enrichment (needs ROUTERAI_API_KEY or AITUNNEL_API_KEY)")
    parser.add_argument("--open", action="store_true", help="open the browser after startup")
    parser.add_argument("--no-open", action="store_true",
                        help="do NOT open the browser (the packaged exe opens it by default)")
    parser.add_argument("--follow", dest="follow", action="store_true", default=None,
                        help="follow new sessions: auto-switch to the newest contentful rollout")
    parser.add_argument("--no-follow", dest="follow", action="store_false",
                        help="pin the current session; do not auto-switch")
    parser.add_argument("--version", action="version", version="GRAFF %s" % __version__)
    return parser.parse_args(argv)


def print_sessions_table(sessions_root: str, limit: int) -> int:
    from graff.rollout import CONTENTFUL_MIN_EVENTS, CONTENTFUL_MIN_TOOL_CALLS, summarise_sessions

    infos = summarise_sessions(sessions_root, limit=limit)
    if not infos:
        print("[graff] no sessions found under %s" % sessions_root)
        return 1
    print("sessions under %s (newest first)" % sessions_root)
    print("-" * 92)
    print("%-4s %-20s %10s %7s %7s %7s  %s" % ("idx", "started", "size", "lines", "events", "calls", "status"))
    print("-" * 92)
    for index, info in enumerate(infos):
        started = _fmt_time(info.timestamp)
        state = "ok" if info.contentful else "empty"
        print("%-4d %-20s %10s %7d %7d %7d  %s" % (
            index, started, _fmt_bytes(info.size), info.lines, info.events, info.tool_calls, state))
    print("-" * 92)
    print("contentful = >= %d tool call(s) or >= %d events; 'latest' skips 'empty' sessions"
          % (CONTENTFUL_MIN_TOOL_CALLS, CONTENTFUL_MIN_EVENTS))
    print("open one with:  python run.py --session index:N   (or --session <path>)")
    return 0


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024.0)
    return "%.1f MB" % (size / 1024.0 / 1024.0)


def _fmt_time(ts: float) -> str:
    import time

    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def main(argv=None) -> int:
    args = parse_args(argv)
    runtime_dir = os.path.abspath(args.runtime or default_runtime_dir())
    os.makedirs(runtime_dir, exist_ok=True)
    static_dir = resource_dir("static")
    state_path = os.path.join(runtime_dir, "state.json")

    if args.list_sessions:
        return print_sessions_table(args.sessions_root, args.limit)

    journal_path = None
    if not args.no_journal:
        journal_path = os.path.join(runtime_dir, "journal.jsonl")

    session_spec = args.session
    demo_path = None
    demo_writer = None

    if args.demo:
        # Optional synthetic session: write a header, then keep appending so the
        # graph grows in real time.  Real sessions remain the default source.
        from graff.demo import DemoWriter
        import random as _random

        demo_path = os.path.join(runtime_dir, "demo", "rollout-demo.jsonl")
        os.makedirs(os.path.dirname(demo_path), exist_ok=True)
        if args.demo_seed is not None:
            _random.seed(args.demo_seed)
        demo_writer = DemoWriter(demo_path, model="deepseek/deepseek-v3.1")
        demo_writer.session_meta()
        demo_writer.turn_context()
        demo_writer.task_started()
        demo_writer.user_message(
            "Демонстрационная сессия: живой граф рассуждений и действий Codex."
        )
        session_spec = demo_path

    engine = LiveEngine(
        session_spec=session_spec,
        sessions_root=args.sessions_root,
        journal_path=journal_path,
        snapshot_dir=os.path.join(runtime_dir, "snapshots"),
        node_cap=args.node_cap,
        poll_interval=args.poll,
        state_path=state_path,
        # A demo session is pinned and must never be auto-replaced.
        session_locked=(args.session is not None) or args.demo,
        follow=(False if args.demo else args.follow),
    )
    api = GraffApi(engine, static_dir=static_dir, runtime_dir=runtime_dir)

    use_fastapi = (args.backend == "fastapi") or (args.backend == "auto" and have_fastapi())
    if args.backend == "fastapi" and not have_fastapi():
        print("[graff] fastapi/uvicorn are not installed; falling back to the stdlib backend.")
        print("[graff] install them with: pip install -r requirements.txt")
        use_fastapi = False

    backend_name = "fastapi+uvicorn" if use_fastapi else "stdlib (no dependencies)"
    url = "http://%s:%d/" % (args.host, args.port)
    resolved = engine.tailer.path if engine.tailer else (session_spec or "latest")
    print("=" * 72)
    print(" GRAFF %s - live Codex reasoning graph" % __version__)
    print(" backend : %s" % backend_name)
    print(" url     : %s" % url)
    print(" session : %s" % resolved)
    print(" mode    : %s" % ("DEMO (synthetic session)" if args.demo else "real Codex sessions"))
    print(" runtime : %s" % runtime_dir)
    print(" state   : %s" % state_path)
    print(" follow  : %s" % ("on (auto-switch to new sessions)" if engine.get_follow() else "off (pinned)"))
    ai_status = engine.ai_status(explicit=args.enrich)
    if args.enrich:
        if ai_status.get("available"):
            print(" ai      : enabled (model=%s via %s)" % (ai_status.get("model"), ai_status.get("provider")))
        else:
            print(" ai      : requested but unavailable -> %s" % ai_status.get("error"))
    else:
        print(" ai      : disabled (enable with --enrich + ROUTERAI_API_KEY/AITUNNEL_API_KEY)")
    print("=" * 72)

    want_open = (args.open or is_frozen()) and not args.no_open
    if want_open:
        try:
            import threading
            import webbrowser

            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    if demo_writer is not None:
        _start_demo_feeder(demo_writer, args.demo_turns, args.demo_interval)

    try:
        if use_fastapi:
            from graff import server_fastapi

            server_fastapi.serve(args.host, args.port, engine, api, quiet=False)
        else:
            from graff import server_stdlib

            server_stdlib.serve(args.host, args.port, engine, api, quiet=False)
    finally:
        if demo_writer is not None:
            demo_writer.close()
    return 0


def _start_demo_feeder(writer, turns: int, interval: float) -> None:
    """Feed extra synthetic turns in the background so the graph grows live."""

    import threading
    import time
    import random as _random

    def loop() -> None:
        for index in range(turns):
            time.sleep(interval)
            try:
                writer.scenario(index)
            except Exception:
                break
        while True:
            time.sleep(interval)
            try:
                writer.scenario(_random.randint(0, 3))
            except Exception:
                break

    thread = threading.Thread(target=loop, name="graff-demo-feeder", daemon=True)
    thread.start()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[graff] stopped")
