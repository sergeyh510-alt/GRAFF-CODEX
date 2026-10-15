"""End-to-end tests for GRAFF against a REAL Codex rollout.

Run with a single command from the project root::

    python -m unittest discover -s tests -v

The tests look for the reference session that ships on this machine
(``rollout-2026-10-13T21-53-35-*.jsonl``).  When it is not present they are
skipped rather than failed, so the suite stays useful on other machines.
"""
from __future__ import annotations

import contextlib
import glob
import re
import io
import json
import shutil
import uuid
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from graff.engine import LiveEngine
from graff.enrich import Enricher
from graff.rollout import (
    DEFAULT_SESSIONS_ROOT,
    latest_session,
    resolve_session,
    scan_session,
    summarise_sessions,
)

REFERENCE_PATTERN = "rollout-2026-10-13T21-53-35-*.jsonl"
EMPTY_PATTERN = "rollout-2026-10-13T21-55-00-*.jsonl"


def find_session(pattern: str):
    matches = glob.glob(os.path.join(DEFAULT_SESSIONS_ROOT, "**", pattern), recursive=True)
    return matches[0] if matches else None


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextlib.contextmanager
def workspace_tmp(prefix: str = "graff-test-"):
    """A temp directory inside the project (``.tmp/``).

    The system temp directory is not always writable, so tests keep their
    scratch data under the workspace, which is also in ``.gitignore``.
    """

    base = os.path.join(PROJECT_ROOT, ".tmp")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, prefix + uuid.uuid4().hex[:10])
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ParserTests(unittest.TestCase):
    """(а) the parser yields a rich graph on the reference session."""

    def setUp(self):
        self.path = find_session(REFERENCE_PATTERN)
        if not self.path:
            self.skipTest("reference session %s not found" % REFERENCE_PATTERN)

    def test_reference_session_has_many_nodes(self):
        engine = LiveEngine(session_spec=self.path, journal_path=None,
                            poll_interval=0.2, flush_interval=0.1)
        engine.start()
        try:
            deadline = time.time() + 15
            nodes = 0
            while time.time() < deadline:
                nodes = len(engine.snapshot()["nodes"])
                if nodes > 100:
                    break
                time.sleep(0.4)
            self.assertGreater(nodes, 100, "expected > 100 nodes, got %d" % nodes)
            counts = engine.status()["counts"]
            self.assertGreater(counts["tool_call"], 0, "expected tool calls in graph")
            self.assertGreater(counts["tool_result"], 0, "expected tool results in graph")
        finally:
            engine.stop()

    def test_scan_counts_tool_calls(self):
        info = scan_session(self.path)
        self.assertGreater(info.tool_calls, 0)
        self.assertTrue(info.contentful, "reference session must be contentful")


class SessionSelectionTests(unittest.TestCase):
    """(б) --session latest never returns a degenerate session."""

    def test_latest_is_not_the_empty_session(self):
        empty = find_session(EMPTY_PATTERN)
        chosen = latest_session()
        self.assertIsNotNone(chosen, "no session selected")
        if empty:
            self.assertNotEqual(os.path.basename(chosen), os.path.basename(empty),
                                "latest selected the degenerate session")
        info = scan_session(chosen)
        self.assertTrue(info.contentful, "latest picked a session with no content")

    def test_index_and_path_specs(self):
        chosen = latest_session()
        self.assertIsNotNone(chosen)
        by_index = resolve_session("index:0")
        self.assertEqual(os.path.abspath(by_index), os.path.abspath(chosen))
        by_path = resolve_session(chosen)
        self.assertEqual(os.path.abspath(by_path), os.path.abspath(chosen))

    def test_sessions_list_reports_stats(self):
        infos = summarise_sessions(limit=10)
        self.assertTrue(infos, "no sessions found")
        for info in infos:
            payload = info.to_dict()
            for key in ("path", "size", "events", "tool_calls", "contentful"):
                self.assertIn(key, payload)
        self.assertTrue(any(info.contentful for info in infos),
                        "expected at least one contentful session")


class HttpApiTests(unittest.TestCase):
    """(в) the running server returns a non-empty graph and switches sessions."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        env = dict(os.environ)
        env.pop("ROUTERAI_API_KEY", None)
        env.pop("AITUNNEL_API_KEY", None)
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", "run.py", "--port", str(cls.port), "--no-journal"],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                cls._get("/api/status")
                return
            except Exception:
                time.sleep(0.3)
        cls.proc.terminate()
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=6)
        except Exception:
            cls.proc.kill()

    @classmethod
    def _get(cls, path):
        with urllib.request.urlopen(cls.base + path, timeout=30) as response:
            return json.load(response)

    @classmethod
    def _post(cls, path, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(cls.base + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def test_graph_is_not_empty(self):
        graph = self._get("/api/graph")
        self.assertGreater(len(graph["nodes"]), 100,
                           "expected a non-empty graph, got %d nodes" % len(graph["nodes"]))
        self.assertGreater(len(graph["edges"]), 0)

    def test_status_reports_counts(self):
        status = self._get("/api/status")
        self.assertGreater(status["events"], 0)
        self.assertEqual(status["parse_errors"], 0, "parser should not error on real data")

    def test_sessions_have_statistics(self):
        sessions = self._get("/api/sessions?limit=8")["sessions"]
        self.assertTrue(sessions)
        self.assertTrue(all("events" in item and "tool_calls" in item for item in sessions))

    def test_can_switch_session_over_http(self):
        result = self._post("/api/session", {"spec": "latest"})
        self.assertTrue(result["ok"], result)
        time.sleep(0.8)
        self.assertGreater(len(self._get("/api/graph")["nodes"]), 0)

    def test_ai_degrades_without_key(self):
        ai = self._get("/api/ai")
        self.assertFalse(ai["available"], "no key should be set in the test environment")
        result = self._post("/api/enrich", {"limit": 2})
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("error"))


class StaticServingTests(unittest.TestCase):
    """Static assets must keep correct MIME types (the .js -> text/plain bug)."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        env = dict(os.environ)
        env.pop("ROUTERAI_API_KEY", None)
        env.pop("AITUNNEL_API_KEY", None)
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", "run.py", "--port", str(cls.port), "--no-journal"],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.base + "/api/status", timeout=5).read()
                return
            except Exception:
                time.sleep(0.3)
        cls.proc.terminate()
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=6)
        except Exception:
            cls.proc.kill()

    @classmethod
    def _head(cls, path):
        """Return (content_type, cache_control, x_content_type_options, body)."""

        with urllib.request.urlopen(cls.base + path, timeout=30) as response:
            headers = response.headers
            body = response.read()
        return (headers.get("Content-Type") or "",
                headers.get("Cache-Control") or "",
                headers.get("X-Content-Type-Options") or "",
                body)

    def test_js_has_javascript_content_type(self):
        ctype, cache, nosniff, body = self._head("/static/vendor/vis-network.min.js")
        self.assertIn("javascript", ctype.lower(),
                      "vis-network.min.js must be served as javascript, got %r" % ctype)
        self.assertNotIn("text/plain", ctype.lower())
        self.assertGreater(len(body), 1000)
        self.assertIn("no-store", cache.lower())
        self.assertEqual(nosniff.lower(), "nosniff")

    def test_app_js_has_javascript_content_type(self):
        ctype, cache, _nosniff, body = self._head("/static/js/app.js")
        self.assertIn("javascript", ctype.lower(), "app.js must be javascript, got %r" % ctype)
        self.assertNotIn("text/plain", ctype.lower())
        self.assertIn("no-store", cache.lower())
        self.assertGreater(len(body), 100)

    def test_boot_js_has_javascript_content_type(self):
        ctype, cache, _nosniff, _body = self._head("/static/js/boot.js")
        self.assertIn("javascript", ctype.lower(), "boot.js must be javascript, got %r" % ctype)
        self.assertIn("no-store", cache.lower())

    def test_css_has_css_content_type(self):
        ctype, cache, _nosniff, _body = self._head("/static/css/styles.css")
        self.assertIn("text/css", ctype.lower(), "styles.css must be css, got %r" % ctype)
        self.assertIn("no-store", cache.lower())

    def test_index_has_html_content_type(self):
        ctype, cache, _nosniff, body = self._head("/")
        self.assertIn("text/html", ctype.lower(), "index must be html, got %r" % ctype)
        self.assertIn("no-store", cache.lower())
        self.assertIn(b"boot.js", body)

    def test_index_has_no_document_write_loader(self):
        _ctype, _cache, _nosniff, body = self._head("/")
        self.assertNotIn(b"document.write", body,
                         "index.html must not use document.write for the loader")

    def test_index_exposes_both_modes(self):
        _ctype, _cache, _nosniff, body = self._head("/")
        html = body.decode("utf-8", "replace")
        for mode in ('data-mode="2d"', 'data-mode="3d"'):
            self.assertIn(mode, html, "index.html is missing the %s mode tab" % mode)
        # the removed "floating balls" mode must be gone entirely
        self.assertNotIn('data-mode="float"', html)
        self.assertNotIn("Плавающие шарики", html)
        self.assertIn('id="stage-canvas"', html)
        self.assertIn('id="gl3d"', html)
        self.assertIn("boot.js", html)

    def test_vendor_libraries_are_served(self):
        for path in ("/static/vendor/vis-network.min.js",
                     "/static/vendor/three.min.js",
                     "/static/vendor/3d-force-graph.min.js"):
            ctype, _cache, _nosniff, body = self._head(path)
            self.assertIn("javascript", ctype.lower(),
                          "%s must be javascript, got %r" % (path, ctype))
            self.assertGreater(len(body), 10000, "%s looks too small" % path)
        _c, _ca, _n, appjs = self._head("/static/js/app.js")
        text = appjs.decode("utf-8", "replace")
        self.assertIn("/static/vendor/three.min.js", text)
        self.assertIn("/static/vendor/3d-force-graph.min.js", text)
        self.assertIn("ForceGraph3D", text)

    def test_3d_loader_uses_load_library_and_hides_on_success(self):
        """3D libraries go through loadLibrary; a successful load hides the box.

        Regression: 3d-force-graph 1.80.1 uses THREE.Timer (added in three r138)
        while the vendored three.min.js is r137.5, so the bundle used to throw at
        load time and the error card appeared with no explanation.
        """
        _c, _ca, _n, appjs = self._head("/static/js/app.js")
        text = appjs.decode("utf-8", "replace")

        for needle in (
            "function loadLibrary(",
            "function loadViaFetch(",
            "loadLibrary([",
            "\"/static/vendor/three.min.js?v=137.5\",",
            "\"/static/vendor/3d-force-graph.min.js?v=1.80.1\",",
            "], graph3dReady, \"3d-force-graph\")",
            "new Function(",
            "cache: \"no-store\"",
            "function hide3dError(",
            'window.addEventListener("error"',
            "unhandledrejection",
            "function getForceGraph3DFactory(",
        ):
            self.assertIn(needle, text, "app.js is missing %r" % needle)

        # the success path hides the card and starts the graph
        self.assertIn("hide3dError();", text)
        self.assertIn("init3d();", text)

    def test_3d_timer_shim_present(self):
        _c, _ca, _n, appjs = self._head("/static/js/app.js")
        text = appjs.decode("utf-8", "replace")
        self.assertIn("function installThreeShims(", text)
        self.assertIn("THREE.Timer = ", text)

    def test_3d_timer_shim_matches_real_contract(self):
        """The THREE.Timer shim must honour the real (three r138+) contract.

        Regression: the previous shim returned ``true`` from ``update()`` while
        3d-force-graph 1.80.1 calls ``timer.update(...).getDelta()`` in its render
        loop, so the chain broke with
        "e.timer.update(...).getDelta is not a function".
        """
        _c, _ca, _n, appjs = self._head("/static/js/app.js")
        text = appjs.decode("utf-8", "replace")

        # Faithful internals: milliseconds kept, seconds returned, chainable.
        for needle in (
            "function makeThreeTimer(",
            "this._previousTime",
            "this._currentTime",
            "this._startTime",
            "this._elapsed",
            "this._timescale",
            "return this._delta / 1000",       # getDelta() -> seconds
            "return this._elapsed / 1000",     # getElapsed() -> seconds
            "setTimescale",
            "getTimescale",
            "this.reset()",
            "return this;",                    # update()/setTimescale()/reset() chainable
        ):
            self.assertIn(needle, text, "app.js Timer is missing %r" % needle)

        # The self-test that also lands in the user-visible log.
        self.assertIn("function timerWorks(", text)
        self.assertIn("update(perfNow())", text)
        self.assertIn("typeof r.getDelta() === \"number\"", text)
        self.assertIn("• самотест three:", text)

    def test_3d_timer_shim_is_conditional(self):
        """The shim must not clobber a native, working THREE.Timer."""
        _c, _ca, _n, appjs = self._head("/static/js/app.js")
        text = appjs.decode("utf-8", "replace")
        # Bail out when the native Timer already behaves.
        self.assertIn("if (timerWorks(window.THREE.Timer)) return ", text)
        # Only replace after the check failed.
        self.assertIn("window.THREE.Timer = makeThreeTimer();", text)

    def test_3d_diagnostics_expose_timer_state(self):
        _c, _ca, _n, appjs = self._head("/static/js/app.js")
        text = appjs.decode("utf-8", "replace")
        # The error card must show the Timer state so one screenshot is enough.
        self.assertIn('", THREE.Timer: "', text)
        self.assertIn("timer: function ()", text)

    def test_3d_mode_url_override(self):
        _c, _ca, _n, appjs = self._head("/static/js/app.js")
        text = appjs.decode("utf-8", "replace")
        self.assertIn("function modeFromUrl(", text)
        self.assertIn('get("mode")', text)

    def test_content_types_table_maps_js_to_javascript(self):
        from graff.static_files import CONTENT_TYPES, content_type_for

        self.assertIn(".js", CONTENT_TYPES)
        self.assertIn("javascript", content_type_for("app.js").lower())
        for ext in (".css", ".html", ".json", ".svg"):
            self.assertIn(ext, CONTENT_TYPES)
        self.assertEqual(content_type_for("styles.css"), "text/css; charset=utf-8")

    def test_css_hides_hidden_overlays(self):
        """The [hidden] attribute must still hide overlays that set display.

        ``.missing-vis`` uses ``display: grid``; without a ``[hidden]`` guard it
        overrides the UA default and the "vis-network did not load" card is
        visible on every page load.
        """

        css_path = os.path.join(PROJECT_ROOT, "static", "css", "styles.css")
        with open(css_path, encoding="utf-8") as handle:
            css = handle.read()

        # A global guard so the hidden attribute can never be overridden.
        self.assertRegex(
            css, r"\[hidden\]\s*\{[^}]*display\s*:\s*none",
            "styles.css must contain a global [hidden] { display: none } rule",
        )
        # And an explicit rule for the specific overlay.
        self.assertRegex(
            css, r"\.missing-vis\[hidden\]\s*\{[^}]*display\s*:\s*none",
            "styles.css must hide .missing-vis when it has the hidden attribute",
        )


class StateFileTests(unittest.TestCase):
    """(3) the chosen session + follow flag survive a restart via state.json."""

    def test_round_trip_and_atomic_write(self):
        from graff.state import StateFile

        with workspace_tmp() as tmp:
            path = os.path.join(tmp, "state.json")
            st = StateFile(path)
            self.assertIsNone(st.get_session())
            self.assertTrue(st.get_follow(), "follow defaults to on")

            st.set_session("C:/tmp/rollout-x.jsonl")
            st.set_follow(False)
            self.assertTrue(os.path.isfile(path))

            again = StateFile(path)          # fresh read == restart
            self.assertEqual(again.get_session(), "C:/tmp/rollout-x.jsonl")
            self.assertFalse(again.get_follow())

    def test_corrupt_file_falls_back_to_defaults(self):
        from graff.state import StateFile

        with workspace_tmp() as tmp:
            path = os.path.join(tmp, "state.json")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json ")
            st = StateFile(path)             # must not raise
            self.assertIsNone(st.get_session())
            self.assertTrue(st.get_follow())


class EngineSessionPersistenceTests(unittest.TestCase):
    """The engine remembers the opened session unless it is CLI-pinned."""

    def setUp(self):
        self.path = find_session(REFERENCE_PATTERN)
        if not self.path:
            self.skipTest("reference session not found")

    def test_locked_session_does_not_overwrite_state(self):
        from graff.state import StateFile

        with workspace_tmp() as tmp:
            state_path = os.path.join(tmp, "state.json")
            engine = LiveEngine(session_spec=self.path, journal_path=None,
                                poll_interval=0.2, flush_interval=0.1,
                                state_path=state_path, session_locked=True)
            engine.start()
            try:
                self.assertEqual(os.path.abspath(engine.store.session_path),
                                 os.path.abspath(self.path))
            finally:
                engine.stop()
            # A pinned (CLI --session) launch must not overwrite the choice.
            self.assertIsNone(StateFile(state_path).get_session())

    def test_open_session_is_remembered_and_restored(self):
        from graff.state import StateFile

        with workspace_tmp() as tmp:
            sessions_root = os.path.join(tmp, "sessions")
            os.makedirs(sessions_root)
            copy = os.path.join(sessions_root, "rollout-2026-10-13T21-53-35-copy.jsonl")
            with io.open(self.path, "rb") as src_handle:
                payload = src_handle.read()
            with io.open(copy, "wb") as dst_handle:
                dst_handle.write(payload)

            state_path = os.path.join(tmp, "state.json")
            first = LiveEngine(session_spec=None, sessions_root=sessions_root,
                               journal_path=None, poll_interval=0.2, flush_interval=0.1,
                               state_path=state_path)
            first.start()
            try:
                first.open_session(copy, remember=True, reason="тест")
                self.assertEqual(os.path.abspath(first.store.session_path),
                                 os.path.abspath(copy))
            finally:
                first.stop()

            self.assertEqual(os.path.abspath(StateFile(state_path).get_session()),
                             os.path.abspath(copy))

            # A second engine with no --session must restore the remembered path.
            second = LiveEngine(session_spec=None, sessions_root=sessions_root,
                                journal_path=None, poll_interval=0.2, flush_interval=0.1,
                                state_path=state_path)
            second.start()
            try:
                self.assertEqual(os.path.abspath(second.store.session_path),
                                 os.path.abspath(copy),
                                 "state.json was not restored on restart")
            finally:
                second.stop()

    def test_follow_switches_to_newest_contentful(self):
        from graff.demo import DemoWriter

        with workspace_tmp() as tmp:
            sessions_root = os.path.join(tmp, "sessions")
            day = os.path.join(sessions_root, "2026", "10", "14")
            os.makedirs(day)

            old = os.path.join(day, "rollout-2026-10-14T10-00-00-old.jsonl")
            writer = DemoWriter(old, model="deepseek/deepseek-v3.1")
            writer.session_meta(); writer.turn_context(); writer.task_started()
            for i in range(3):
                writer.scenario(i)
            writer.close()

            engine = LiveEngine(session_spec=old, sessions_root=sessions_root,
                                journal_path=None, poll_interval=0.2, flush_interval=0.1,
                                state_path=os.path.join(tmp, "state.json"),
                                session_locked=False, follow=True)
            engine.start()
            try:
                self.assertEqual(os.path.abspath(engine.store.session_path), os.path.abspath(old))

                # A newer contentful rollout appears -> follow must switch to it.
                fresh = os.path.join(day, "rollout-2026-10-14T11-30-00-new.jsonl")
                writer = DemoWriter(fresh, model="deepseek/deepseek-v3.1")
                writer.session_meta(); writer.turn_context(); writer.task_started()
                for i in range(3):
                    writer.scenario(i)
                writer.close()
                os.utime(fresh, (time.time() + 5, time.time() + 5))

                engine._check_follow()
                self.assertEqual(os.path.abspath(engine.store.session_path),
                                 os.path.abspath(fresh),
                                 "follow mode did not switch to the newer session")

                # With follow off the engine must hold the current session.
                engine.set_follow(False)
                newer = os.path.join(day, "rollout-2026-10-14T12-00-00-newer.jsonl")
                writer = DemoWriter(newer, model="deepseek/deepseek-v3.1")
                writer.session_meta(); writer.turn_context(); writer.task_started()
                for i in range(3):
                    writer.scenario(i)
                writer.close()
                os.utime(newer, (time.time() + 10, time.time() + 10))
                engine._check_follow()
                self.assertEqual(os.path.abspath(engine.store.session_path),
                                 os.path.abspath(fresh),
                                 "follow-off must pin the session")
            finally:
                engine.stop()


class PerformanceWiringTests(unittest.TestCase):
    """(1) 3D performance work is wired into the served frontend."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        env = dict(os.environ)
        env.pop("ROUTERAI_API_KEY", None)
        env.pop("AITUNNEL_API_KEY", None)
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", "run.py", "--port", str(cls.port), "--no-journal"],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.base + "/api/status", timeout=5).read()
                return
            except Exception:
                time.sleep(0.3)
        cls.proc.terminate()
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=6)
        except Exception:
            cls.proc.kill()

    @classmethod
    def _text(cls, path):
        with urllib.request.urlopen(cls.base + path, timeout=30) as response:
            return response.read().decode("utf-8", "replace")

    def test_app_js_has_performance_mode(self):
        text = self._text("/static/js/app.js")
        for needle in (
            "GL_SPRITE_LIMIT", "GL_SYNC_LIMIT", "GL_REFRESH_MS", "FPS_SAMPLE_MS",
            "function apply3dPerf(", "function spritesWanted(", "function glRealCount(",
            "function glNodeList(", "function sync3d(force)",
            "nodeLabel(state.perf ? null", "setPixelRatio", "pauseAnimation",
            "function applyPerfMode(", "function estimateFps(",
        ):
            self.assertIn(needle, text, "app.js is missing %r" % needle)
        # 3D defaults to collapsed tool chains and a hard node cap.
        self.assertIn('state.mode === "3d") state.collapse = true', text)

    def test_index_exposes_performance_and_follow_toggles(self):
        html = self._text("/")
        for needle in (
            'id="perf-mode"', 'id="perf-note"', 'id="follow-sessions"',
            'id="session-log"', 'id="session-select"',
        ):
            self.assertIn(needle, html, "index.html is missing %r" % needle)

    def test_app_js_persists_perf_and_follow(self):
        text = self._text("/static/js/app.js")
        self.assertIn("perf: state.perf", text)
        self.assertIn("followSessions: state.followSessions", text)
        self.assertIn("function setFollowOnServer(", text)
        self.assertIn("/api/follow", text)

    def test_sessions_show_nodes_calls_and_active_marker(self):
        sessions = json.loads(self._text("/api/sessions?limit=8"))["sessions"]
        self.assertTrue(sessions)
        for item in sessions:
            self.assertIn("nodes", item)
            self.assertIn("tool_calls", item)
            self.assertIn("current", item)

    def test_state_and_follow_endpoints(self):
        state = json.loads(self._text("/api/state"))
        self.assertIn("session", state)
        self.assertIn("follow", state)
        self.assertIn("state_path", state)
        # The toggle round-trips through the engine.
        payload = json.dumps({"follow": False}).encode("utf-8")
        request = urllib.request.Request(self.base + "/api/follow", data=payload,
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            toggled = json.load(response)
        self.assertEqual(toggled.get("follow"), False)
        back = json.dumps({"follow": True}).encode("utf-8")
        request = urllib.request.Request(self.base + "/api/follow", data=back,
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            json.load(response)

    def test_status_exposes_follow_and_log(self):
        status = json.loads(self._text("/api/status"))
        self.assertIn("follow", status)
        self.assertIn("log", status)
        self.assertIsInstance(status["log"], list)


class D3DrawCallTests(unittest.TestCase):
    """3D draw-call budget: links as one LineSegments, not per-link meshes.

    Regression: non-zero linkWidth turns every link into its own cylinder Mesh
    and a directional arrow into another Mesh, so a ~3900-link session used to
    push >10k draw calls (CPU bound).  The defaults must now be line links with
    no arrows, thick/curved links opt-in and gated by the link count.
    """

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        env = dict(os.environ)
        env.pop("ROUTERAI_API_KEY", None)
        env.pop("AITUNNEL_API_KEY", None)
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", "run.py", "--port", str(cls.port), "--no-journal"],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.base + "/api/status", timeout=5).read()
                return
            except Exception:
                time.sleep(0.3)
        cls.proc.terminate()
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=6)
        except Exception:
            cls.proc.kill()

    @classmethod
    def _app_js(cls):
        with urllib.request.urlopen(cls.base + "/static/js/app.js", timeout=30) as response:
            return response.read().decode("utf-8", "replace")

    def test_links_default_to_lines(self):
        text = self._app_js()
        # The whole-graph link style defaults must be the cheap ones.
        self.assertIn(".linkWidth(0)", text)
        self.assertIn(".linkDirectionalArrowLength(0)", text)
        self.assertIn(".linkCurvature(0)", text)

    def test_link_styles_are_gated_by_the_link_count(self):
        text = self._app_js()
        self.assertIn("GL_BIG_LINK", text)
        self.assertIn("GL_CURVE_LIMIT", text)
        self.assertIn("GL_ARROW_LIMIT", text)
        self.assertIn("wantThick", text)
        self.assertIn("wantCurve", text)
        self.assertIn("links <= GL_BIG_LINK", text)

    def test_nodes_use_builtin_instanced_path(self):
        text = self._app_js()
        self.assertIn("gl.nodeVal(function (n) { return n.val; })", text)
        self.assertIn("gl.nodeColor(function (n) { return n.color; })", text)
        # sprites never combined with extend
        self.assertIn("gl.nodeThreeObjectExtend(false)", text)
        self.assertIn("GL_NODE_RES_BIG", text)
        self.assertIn("nodeResolution", text)

    def test_rotation_uses_orbit_autorate(self):
        text = self._app_js()
        # controlType must request orbit controls (the ones with autoRotate)
        self.assertIn('controlType: "orbit"', text)
        self.assertIn("autoRotateSpeed", text)
        self.assertIn("GL_AUTO_ROTATE_SPEED", text)
        # the accessor must be controls(), not the non-existent control()
        self.assertIn('gl.controls()', text)
        self.assertNotIn('gl.control()', text)

    def test_live_3d_uses_the_real_bundle_api(self):
        """Cross-check app.js' live 3D calls against the vendored bundle API."""
        bundle = os.path.join(PROJECT_ROOT, "static", "vendor", "3d-force-graph.min.js")
        if not os.path.exists(bundle):
            self.skipTest("3d-force-graph bundle not present")
        with io.open(bundle, encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        # The public accessors the live code relies on must really exist.
        self.assertIn("controls:e=>", source, "bundle has no controls() accessor")
        self.assertIn("controlType", source, "bundle lost its controlType option")
        self.assertIn("autoRotateSpeed", source, "bundle lost OrbitControls autoRotate")

    def test_diagnostics_panel_and_renderer_info(self):
        text = self._app_js()
        for needle in ("function rendererInfo(", "renderer.info", "draw calls",
                       "gl-calls", "gl-tris", "gl-counts", "lastFrameMs"):
            self.assertIn(needle, text, "app.js is missing %r" % needle)
        with urllib.request.urlopen(self.base + "/", timeout=30) as response:
            html = response.read().decode("utf-8", "replace")
        for needle in ('id="gl-calls"', 'id="gl-tris"', 'id="gl-fps"',
                       'id="gl-diagnostics"', 'id="gl-thick-links"', 'id="gl-curved-links"'):
            self.assertIn(needle, html, "index.html is missing %r" % needle)

    def test_demo_generator_supports_node_target(self):
        with io.open(os.path.join(PROJECT_ROOT, "scripts", "make_demo_session.py"),
                     encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("--nodes", text)
        self.assertIn("EVENTS_PER_TURN", text)


class RemovedFloatModeTests(unittest.TestCase):
    """The "floating balls" mode was deleted -- no code may reference it.

    It only overlapped with the lanes look and added timers that disturbed 3D,
    so it is gone for good: no floatMode(), no drift loop, no ?mode=float.
    """

    @classmethod
    def _app_js(cls):
        with io.open(os.path.join(PROJECT_ROOT, "static", "js", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_no_float_mode_code(self):
        text = self._app_js()
        for needle in ("floatMode", "DRIFT_", "startDrift", "stopDrift", "pauseDrift",
                       "driftStep", "scheduleDrift", "updateFloatUi", "scatterNodes",
                       "visibleNodeCount", "driftTimer", "driftActive", "hoverNodeId"):
            self.assertNotIn(needle, text, "app.js still references %r" % needle)
        # ?mode= no longer offers float, and no layout branch keys off it
        self.assertNotIn("mode=\"float\"", text)

    def test_switch_mode_has_no_drift_side_effects(self):
        text = self._app_js()
        start = text.find("function switchMode(")
        self.assertGreater(start, 0)
        body = text[start:start + 1400]
        for needle in ("drift", "Drift", "float", "scatter"):
            self.assertNotIn(needle, body,
                             "switchMode must not touch the removed %r mode" % needle)

    def test_float_saved_state_migrates_to_2d(self):
        text = self._app_js()
        self.assertIn('if (p.mode === "float") {', text)
        self.assertIn('state.mode = "2d";', text)
        # the normalized value is written back right away
        self.assertIn("savePrefs();", text)
        # ?mode= accepts only 2d/3d now
        self.assertIn('if (m === "2d" || m === "3d") return m;', text)

    def test_diagnostics_have_no_drift_row(self):
        with io.open(os.path.join(PROJECT_ROOT, "static", "index.html"),
                     encoding="utf-8") as handle:
            html = handle.read()
        self.assertNotIn("gl-drift", html)
        for needle in ('id="gl-mode"', 'id="gl-layout"', 'id="gl-physics"'):
            self.assertIn(needle, html, "index.html is missing %r" % needle)


class AboutModalTests(unittest.TestCase):
    """The in-app help: an About modal reachable by button and ?about=1."""

    @classmethod
    def _read(cls, *parts):
        with io.open(os.path.join(PROJECT_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_index_has_about_button_and_modal(self):
        html = self._read("static", "index.html")
        self.assertIn('id="about-btn"', html)
        self.assertIn('id="about-modal"', html)
        self.assertIn('id="about-close"', html)
        self.assertIn('id="about-version"', html)
        start = html.find('id="about-modal"')
        self.assertIn("hidden", html[start:start + 200],
                      "the About modal must start hidden")
        self.assertIn('role="dialog"', html)

    def test_about_modal_shows_the_required_content(self):
        html = self._read("static", "index.html")
        for needle in (
            "GRAFF \u2014 \u0436\u0438\u0432\u043e\u0439 \u0433\u0440\u0430\u0444 \u0440\u0430\u0441\u0441\u0443\u0436\u0434\u0435\u043d\u0438\u0439 \u0438 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0439 Codex",
            "Sergey Chekryzhov",
            "sergeyh510@gmail.com",
            "sergeyh510-alt",
            "https://github.com/sergeyh510-alt/GRAFF-CODEX",
            "www.linkedin.com/in/sergey-chekryzhov-a38778217",
            "@SergeyChekryzhov",
            "LICENSE",
        ):
            self.assertIn(needle, html, "the About modal is missing %r" % needle)
        self.assertIn("\u043f\u043e \u043b\u0438\u0446\u0435\u043d\u0437\u0438\u0438 MIT", html)

    def test_about_links_are_clickable_and_safe(self):
        html = self._read("static", "index.html")
        start = html.find('id="about-modal"')
        modal = html[start:]
        for url in ("https://github.com/sergeyh510-alt",
                    "https://github.com/sergeyh510-alt/GRAFF-CODEX",
                    "https://www.linkedin.com/in/sergey-chekryzhov-a38778217",
                    "https://t.me/SergeyChekryzhov",
                    "https://github.com/sergeyh510-alt/GRAFF-CODEX/blob/main/LICENSE"):
            self.assertIn('href="%s"' % url, modal, "missing link %s" % url)
        self.assertIn('href="mailto:sergeyh510@gmail.com"', modal)
        self.assertGreaterEqual(modal.count('target="_blank"'), 5)
        self.assertGreaterEqual(modal.count('rel="noopener"'), 5)
        self.assertEqual(modal.count('target="_blank"'), modal.count('rel="noopener"'))

    def test_about_has_no_external_resources_or_trackers(self):
        html = self._read("static", "index.html")
        start = html.find('id="about-modal"')
        end = html.find("<!-- boot.js loads")
        modal = html[start:end if end > start else len(html)]
        for needle in ("<img", "<iframe", "http://", "google-analytics", "gtag(",
                       'rel="preload"', "<script"):
            self.assertNotIn(needle, modal,
                             "the About modal must not pull external resources: %r" % needle)

    def test_about_js_wires_open_close_and_url(self):
        text = self._read("static", "js", "app.js")
        self.assertIn("function aboutOpen(", text)
        self.assertIn("function aboutClose(", text)
        self.assertIn("function aboutFromUrl(", text)
        self.assertIn('new URLSearchParams(location.search).get("about")', text)
        self.assertIn('$("about-btn").addEventListener("click"', text)
        self.assertIn('$("about-close").addEventListener("click", aboutClose)', text)
        self.assertIn('if (e.target === $("about-modal")) aboutClose();', text)
        self.assertIn("if (aboutIsOpen()) { aboutClose(); return; }", text)
        self.assertIn("if (aboutFromUrl()) aboutOpen();", text)

    def test_about_version_comes_from_the_status_payload(self):
        app = self._read("static", "js", "app.js")
        self.assertIn("if (s.version) applyVersion(s.version);", app)
        self.assertIn('el.textContent = "\u0432\u0435\u0440\u0441\u0438\u044f " + version;', app)
        engine = self._read("graff", "engine.py")
        self.assertIn("from . import __version__", engine)
        self.assertIn('"version": __version__,', engine)
        import graff

        self.assertTrue(graff.__version__)

    def test_about_modal_is_hidden_by_default_in_css(self):
        css = self._read("static", "css", "styles.css")
        self.assertIn(".about-overlay[hidden] { display: none; }", css)
        self.assertIn("[hidden] { display: none !important; }", css)
        start = css.find(".about-overlay {")
        self.assertGreater(start, 0)
        self.assertIn("z-index: 60;", css[start:start + 600])


class AboutStatusVersionTests(unittest.TestCase):
    """/api/status must expose the version the About modal prints."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        env = dict(os.environ)
        env.pop("ROUTERAI_API_KEY", None)
        env.pop("AITUNNEL_API_KEY", None)
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", "run.py", "--port", str(cls.port), "--no-journal"],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.base + "/api/status", timeout=5).read()
                return
            except Exception:
                time.sleep(0.3)
        cls.proc.terminate()
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=6)
        except Exception:
            cls.proc.kill()

    def test_status_reports_the_package_version(self):
        import graff

        with urllib.request.urlopen(self.base + "/api/status", timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload.get("version"), graff.__version__)

    def test_index_serves_the_about_modal(self):
        with urllib.request.urlopen(self.base + "/", timeout=20) as response:
            html = response.read().decode("utf-8", "replace")
        self.assertIn('id="about-modal"', html)
        self.assertIn("Sergey Chekryzhov", html)


class PackagingTests(unittest.TestCase):
    """(2) the exe build bundles static/ and keeps state outside the bundle."""

    def test_spec_bundles_static(self):
        with io.open(os.path.join(PROJECT_ROOT, "GRAFF.spec"), encoding="utf-8") as handle:
            spec = handle.read()
        # Quote/space insensitive: PyInstaller specs may use either quoting.
        normalised = re.sub(r"\s+", "", spec).replace("'", '"')
        self.assertIn('("static","static")', normalised)
        self.assertIn("console=True", spec)
        self.assertIn("run.py", spec)

    def test_build_script_adds_static_data(self):
        with io.open(os.path.join(PROJECT_ROOT, "build.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("--add-data", text)
        self.assertIn("static", text)
        self.assertIn("--onefile", text)
        self.assertIn("--console", text)

    def test_run_py_uses_bundle_paths(self):
        with io.open(os.path.join(PROJECT_ROOT, "run.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('resource_dir("static")', text)
        self.assertIn("default_runtime_dir()", text)
        self.assertIn("is_frozen()", text)

    def test_paths_module_handles_meipass(self):
        from graff import paths

        self.assertTrue(callable(paths.bundle_dir))
        self.assertTrue(callable(paths.resource_dir))
        self.assertTrue(callable(paths.default_runtime_dir))
        self.assertIn("runtime", paths.default_runtime_dir())


class EnrichPlanTests(unittest.TestCase):
    """The enrichment planner works offline (no network)."""

    def test_plan_groups_messages_with_tool_calls(self):
        path = find_session(REFERENCE_PATTERN)
        if not path:
            self.skipTest("reference session not found")
        engine = LiveEngine(session_spec=path, journal_path=None,
                            poll_interval=0.2, flush_interval=0.1)
        engine.start()
        try:
            time.sleep(2.0)
            plan = Enricher.build_plan(engine.store.events_snapshot())
            self.assertTrue(plan, "expected at least one assistant step with tools")
            self.assertTrue(all(step["children"] for step in plan))
            self.assertTrue(all(step["message"].type == "message" for step in plan))
        finally:
            engine.stop()


class ChainMarkerTests(unittest.TestCase):
    """(Task 1) 3D chain start/end markers: exactly two scene objects.

    The whole point is that the markers must NOT go through nodeThreeObject
    (which would add an object per node); they are two standalone meshes whose
    positions are copied from graphData() every frame.
    """

    @classmethod
    def _app_js(cls):
        with io.open(os.path.join(PROJECT_ROOT, "static", "js", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_marker_module_is_two_meshes(self):
        text = self._app_js()
        for needle in (
            "var MARKER_GREEN", "var MARKER_YELLOW",
            "function markerMesh(", "function ensureMarkers(",
            "scene.add(markerStart.mesh)", "scene.add(markerEnd.mesh)",
            "function updateMarkers(", "function chainEnds(",
            "function startMarkerTimer(", "function stopMarkerTimer(",
            ".lookAt(camPos.x, camPos.y, camPos.z)",
        ):
            self.assertIn(needle, text, "app.js is missing %r" % needle)

    def test_markers_never_use_node_three_object(self):
        text = self._app_js()
        # nodeThreeObject must still be reserved for the (opt-in) glow sprites
        # and never receive a marker factory.
        self.assertIn("gl.nodeThreeObject(sprites ? makeGlowObject : null)", text)
        for needle in ("nodeThreeObject(marker", "nodeThreeObject(makeMarker",
                       "nodeThreeObject(function (n) { return marker"):
            self.assertNotIn(needle, text)

    def test_marker_loop_is_gated_by_tab_visibility(self):
        text = self._app_js()
        # pulsation only when the 3D tab is active and the window is visible
        self.assertIn("function markerWanted() {", text)
        self.assertIn('state.mode === "3d" && !!gl && !document.hidden', text)

    def test_marker_toggle_is_persisted(self):
        text = self._app_js()
        self.assertIn("markers: state.markers,", text)      # savePrefs
        self.assertIn("state.markers = p.markers;", text)   # loadPrefs

    def test_index_exposes_marker_toggle_and_legend(self):
        with io.open(os.path.join(PROJECT_ROOT, "static", "index.html"),
                     encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('id="gl-markers"', html)
        self.assertIn("начало цепочки", html)
        self.assertIn("конец цепочки", html)
        self.assertIn("#3ddc84", html)  # green
        self.assertIn("#ffce38", html)  # yellow


class LaneShiftTests(unittest.TestCase):
    """(Task 2) 2D lane drag: a horizontal node shift survives reload."""

    @classmethod
    def _app_js(cls):
        with io.open(os.path.join(PROJECT_ROOT, "static", "js", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_lane_nodes_are_free_horizontally(self):
        text = self._app_js()
        # Pinned vertically only -- a fixed x would block vis's drag entirely.
        self.assertIn("fixed: { x: false, y: true }", text)
        self.assertNotIn("fixed: { x: true, y: true }", text)

    def test_drag_handlers_record_the_shift(self):
        text = self._app_js()
        for needle in (
            'network.on("dragStart", function (params) {',
            'network.on("dragging", function (params) {',
            "laneBaseX[id]", "setLaneShift(id, dx, false)",
            "function reflowLanes(", "function positionLanes(", "function laneShiftOf(",
        ):
            self.assertIn(needle, text, "app.js is missing %r" % needle)

    def test_shift_is_applied_and_persisted(self):
        text = self._app_js()
        # positionLanes adds the saved shift on top of the auto slot
        self.assertIn("var baseX = LANE_X0 + m * step;", text)
        self.assertIn("var shift = laneShiftOf(v.id);", text)
        self.assertIn("x: baseX + shift", text)
        # and it round-trips through localStorage
        self.assertIn("laneShifts: state.laneShifts", text)          # savePrefs
        self.assertIn("state.laneShifts[sid] = p.laneShifts[sid];", text)  # loadPrefs

    def test_reset_button_clears_shifts(self):
        text = self._app_js()
        self.assertIn("function resetLaneShifts(", text)
        self.assertIn("state.laneShifts = {};", text)
        self.assertIn('$("btn-reset-shifts").addEventListener("click", resetLaneShifts)', text)
        with io.open(os.path.join(PROJECT_ROOT, "static", "index.html"),
                     encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('id="btn-reset-shifts"', html)

    def test_manual_shift_preserved_by_auto_layout(self):
        text = self._app_js()
        # positionLanes must not wipe the offsets; it re-applies them.
        start = text.find("function positionLanes(")
        body = text[start:start + 1200]
        self.assertIn("laneShiftOf(v.id)", body)
        self.assertIn("laneBaseX[v.id] = baseX;", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
