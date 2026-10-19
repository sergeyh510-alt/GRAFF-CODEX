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
        # ?mode= still rejects float; river was added later and is allowed
        self.assertIn('if (m === "2d" || m === "3d" || m === "river") return m;', text)
        self.assertNotIn('m === "float"', text)

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


class RiverModeTests(unittest.TestCase):
    """The third view: «Русло» (a computed stream of time, no physics)."""

    @classmethod
    def _read(cls, *parts):
        with io.open(os.path.join(PROJECT_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    @classmethod
    def _river(cls):
        return cls._read("static", "js", "river.js")

    def test_index_exposes_three_modes(self):
        html = self._read("static", "index.html")
        for mode in ('data-mode="2d"', 'data-mode="3d"', 'data-mode="river"'):
            self.assertIn(mode, html, "index.html is missing the %s tab" % mode)
        self.assertIn("Русло", html)
        self.assertIn('id="river3d"', html)
        self.assertIn('id="panel-river"', html)
        self.assertIn('id="legend-river"', html)

    def test_river_panel_has_all_the_toggles(self):
        html = self._read("static", "index.html")
        for control in ('id="river-collapse"', 'id="river-follow"', 'id="river-links"',
                        'id="btn-river-fit"', 'id="btn-river-latest"',
                        'id="river-fps"', 'id="river-calls"', 'id="river-sparks"'):
            self.assertIn(control, html, "the river panel is missing %r" % control)
        # the "fit everything" button is required in every mode
        self.assertIn("Уместить всё", html)

    def test_river_mode_is_selectable_by_url(self):
        app = self._read("static", "js", "app.js")
        self.assertIn('if (m === "2d" || m === "3d" || m === "river") return m;', app)
        # the mode is persisted like the other two
        self.assertIn("mode: state.mode,", app)
        self.assertIn('if (p.mode === "2d" || p.mode === "3d" || p.mode === "river")', app)

    def test_river_layout_is_computed_and_append_only(self):
        river = self._river()
        # a slot maps to a fixed point of the snaked stream: no forces involved
        self.assertIn("function coordsFor(slot, depthAbs)", river)
        self.assertIn("BAND_CAP", river)
        self.assertIn("function packRuns(", river)
        self.assertIn("function routineUnits(", river)
        # branching spreads by the child ordinal only
        self.assertIn("function branchOffset(k)", river)
        # never a physics call anywhere in the river module
        for banned in ("applyPhysics", "physicsEnabled", "Math.random"):
            self.assertNotIn(banned, river, "river.js must stay deterministic: %r" % banned)

    def test_river_uses_instancing_lines_and_a_spark_pool(self):
        river = self._river()
        self.assertIn("new THREE_.InstancedMesh(", river)   # nodes: one draw call
        # links are one camera-facing ribbon mesh, so the WHOLE scene is exactly
        # two instanced passes plus the ribbon plus the spark pool: four calls,
        # and none of them per node or per edge.
        self.assertNotIn("LineSegments", river)
        self.assertIn("new THREE_.Mesh(linkGeo, linkMat)", river)  # links: one draw call
        self.assertIn("SPARK_POOL", river)                  # sparks: bounded
        self.assertIn("AdditiveBlending", river)
        # never a per-node DOM element or a per-node scene object
        self.assertNotIn("nodeThreeObject", river)
        self.assertNotIn("createElement(\"div\")", river)

    def test_river_animation_budget(self):
        river = self._river()
        self.assertIn("var FRESH_CAP =", river)             # only fresh nodes animate
        self.assertIn("SPARK_STAGGER_MS", river)            # a batch leaves one by one
        self.assertIn("sparkDuration: function (distance, speed)", river)  # long edge -> longer spark
        self.assertIn("ERROR_FLASH_MS", river)              # errors flash longest
        self.assertIn("function crest(", river)             # step finished -> crest
        blocked = re.search(r"function isPaused\(\)\s*\{[^}]*document\.hidden", river)
        self.assertIsNotNone(blocked, "river animation must stop on a hidden tab")
        self.assertIn("function recolour(", river)          # cooling is a colour property

    def test_river_palette_is_the_agreed_one(self):
        river = self._river()
        for color in ("#a855f7", "#e2f3ff", "#2b8cff", "#00e5a0", "#ffb020", "#ff2d55",
                      "#7dff5a", "#ffd60a"):
            self.assertIn(color, river, "river.js is missing the colour %s" % color)
        # old nodes go to a deep blue-violet, they do not turn grey
        self.assertIn("cold:", river)
        self.assertIn("COOL_MIX", river)
        # red stays reserved for errors
        self.assertIn('if (type === "error")', river)

    def test_river_keeps_the_shared_features(self):
        app = self._read("static", "js", "app.js")
        # the shared machinery is reached from the river branch, not reimplemented
        for needle in ("} else if (state.mode === \"river\") {",
                       "syncRiver();",
                       "if (state.mode === \"river\") syncRiver(true);",
                       "riverFocus(viewId);",
                       "riverCrest();",
                       "if (state.mode === \"river\") {"):
            self.assertIn(needle, app, "app.js river integration is missing %r" % needle)
        # export, search, filters and details are untouched by the new mode
        for needle in ("function exportGraph(", "function recomputeMatches(", "function showDetails(",
                       "function toggleType(", "function buildTypeFilters("):
            self.assertIn(needle, app)

    def test_river_leaves_the_other_two_modes_alone(self):
        app = self._read("static", "js", "app.js")
        # the old 3D and 2D code paths are still there, unchanged in shape
        self.assertIn("factory({ controlType: \"orbit\" })(box)", app)
        self.assertIn("function positionLanes()", app)
        self.assertIn("function layoutOptions()", app)
        # leaving a mode stops its loops: no timer from one view survives
        self.assertIn('if (was === "3d") stop3d();', app)
        self.assertIn('if (was === "river") stopRiver();', app)
        self.assertIn("function stopRiver()", app)
        self.assertIn("function stop3d()", app)

    def test_river_is_only_animated_while_visible(self):
        app = self._read("static", "js", "app.js")
        self.assertIn("function riverWanted()", app)
        vis = app.find("function onVisibilityChange()")
        self.assertGreater(vis, 0)
        body = app[vis:vis + 1200]
        self.assertIn("stopRiver();", body, "hiding the tab must freeze the river")
        self.assertIn("river.start();", body, "showing the tab must resume the river")

    def test_river_sparks_are_off_in_performance_mode(self):
        app = self._read("static", "js", "app.js")
        start = app.find("function syncRiver(")
        self.assertGreater(start, 0)
        body = app[start:start + 2600]
        self.assertIn("if (!state.perf) river.spawnSparks(res.indices);", body)

    def test_river_script_is_loaded_by_boot(self):
        boot = self._read("static", "js", "boot.js")
        self.assertIn("river.js", boot)
        self.assertIn("window.GRAFFRiver", self._river())
        # the static table must serve .js as javascript (already covered, asserted here again)
        from graff import static_files

        self.assertIn("javascript", static_files.content_type_for("x.js").lower())


class RiverLayoutTests(unittest.TestCase):
    """The computed layout itself: stable positions, snaking, grouping."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.river = os.path.join(PROJECT_ROOT, "static", "js", "river.js")

    def _run(self, script_name, body):
        if not self.node:
            self.skipTest("node is not available")
        script = os.path.join(PROJECT_ROOT, ".tmp", script_name)
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with io.open(script, "w", encoding="utf-8") as handle:
            handle.write(body)
        proc = subprocess.run([self.node, script], cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8", "replace"))

    def _harness(self, payload):
        return (
            "global.window = global;\n"
            "const fs = require('fs'), vm = require('vm');\n"
            "const ctx = { window: global, document: { createElement: () => ({ getContext: () => ({}) }) }, console };\n"
            "vm.createContext(ctx);\n"
            "vm.runInContext(fs.readFileSync(%r, 'utf8'), ctx);\n"
            "const R = ctx.window.GRAFFRiver;\n" % self.river
        ) + payload

    def test_existing_nodes_never_move(self):
        out = self._run("river_stability.js", self._harness(
            "function linear(n) { const T=['reason','message','tool_call','tool_result','file_edit','error'];\n"
            "  const o=[]; let p=null;\n"
            "  for (let i=0;i<n;i++){const t=T[i%6],id='n'+i;\n"
            "    o.push({id:id,source:{id:id,ts:1000+i,type:t,actor:'codex',label:'e'+i,parent:p,meta:{name:'exec_command',call_id:'c'+i}}});p=id;}\n"
            "  return o; }\n"
            "function snap(ns){const o={};ns.forEach(n=>{o[n.id]=[n.x,n.y,n.z,n.slot];});return o;}\n"
            "const a=snap(R.layout(linear(300), true).nodes);\n"
            "const b=snap(R.layout(linear(303), true).nodes);\n"
            "let moved=0,checked=0;\n"
            "Object.keys(a).forEach(id=>{if(!b[id])return;checked++;if(a[id].join()!==b[id].join())moved++;});\n"
            "console.log(JSON.stringify({checked:checked,moved:moved}));"))
        self.assertGreater(out["checked"], 0)
        self.assertEqual(out["moved"], 0,
                         "a node already on screen must never move when an event arrives")

    def test_routine_runs_collapse_into_groups(self):
        out = self._run("river_groups.js", self._harness(
            "function sess(n){const o=[];let p=null,i=0;\n"
            "  while(o.length<n){const cid='c'+i;\n"
            "    const call={id:'n'+i,ts:1000+i,type:'tool_call',actor:'codex',parent:p,meta:{name:'exec_command',call_id:cid}};\n"
            "    const res={id:'n'+i+'r',ts:1000+i+0.5,type:'tool_result',actor:'codex',parent:call.id,meta:{name:'exec_command',call_id:cid}};\n"
            "    o.push({id:call.id,source:call});o.push({id:res.id,source:res});p=res.id;i+=2;}\n"
            "  return o.slice(0,n);}\n"
            "const it=sess(600);\n"
            "const off=R.layout(it,false).nodes.length;\n"
            "const on=R.layout(it,true).nodes;\n"
            "console.log(JSON.stringify({without:off,with:on.length,biggest:Math.max.apply(null,on.map(n=>n.count))}));"))
        self.assertLess(out["with"], out["without"])
        self.assertGreater(out["biggest"], 1)

    def test_stream_snakes_into_bands(self):
        out = self._run("river_bands.js", self._harness(
            "const T=['reason','message','tool_call','tool_result','file_edit','error'];\n"
            "const it=[];let p=null;\n"
            "for(let i=0;i<500;i++){const t=T[i%6],id='n'+i;\n"
            "  it.push({id:id,source:{id:id,ts:1000+i,type:t,actor:'codex',label:'e',parent:p,meta:{name:'t'+(i%2)}}});p=id;}\n"
            "const ns=R.layout(it,false).nodes;\n"
            "const cap=R.constants.BAND_CAP;\n"
            "console.log(JSON.stringify({cap:cap,bands:new Set(ns.map(n=>n.band)).size,\n"
            "  first:ns[0].x, last_of_band:ns[cap-1].x, first_of_next:ns[cap].x,\n"
            "  second_of_next:ns[cap+1].x, z0:ns[0].z, z1:ns[cap].z}));"))
        self.assertGreater(out["bands"], 1, "a long stream must be folded into bands")
        # the snake turns at the band edge: the last slot of a band and the first
        # slot of the next one share the same x, and the next band sits deeper
        self.assertEqual(out["last_of_band"], out["first_of_next"])
        self.assertNotEqual(out["z0"], out["z1"])
        # band 0 runs left -> right, band 1 runs back right -> left
        self.assertEqual(out["first"], 0)
        self.assertLess(out["second_of_next"], out["first_of_next"])

    def test_branching_spreads_siblings(self):
        out = self._run("river_branch.js", self._harness(
            "const it=[{id:'root',source:{id:'root',ts:1,type:'message',actor:'codex',parent:null,meta:{}}},\n"
            "  {id:'step',source:{id:'step',ts:2,type:'reason',actor:'codex',parent:'root',meta:{}}}];\n"
            "for(let c=0;c<5;c++)it.push({id:'c'+c,source:{id:'c'+c,ts:3+c,type:'tool_call',actor:'codex',parent:'step',meta:{name:'exec',call_id:'k'+c}}});\n"
            "const ns=R.layout(it,false).nodes;\n"
            "console.log(JSON.stringify(ns.map(n=>[n.id,n.ord,n.y])));"))
        ys = {row[0]: row[2] for row in out}
        self.assertEqual(len(set(ys[k] for k in ys if k.startswith("r:c"))), 5,
                         "parallel calls of one step must spread sideways")

    def test_spark_duration_follows_edge_length(self):
        out = self._run("river_spark.js", self._harness(
            "console.log(JSON.stringify({short:R.sparkDuration(20),long:R.sparkDuration(900),cap:R.sparkDuration(1e6)}));"))
        self.assertLess(out["short"], out["long"])
        self.assertLessEqual(out["cap"], 600)
        self.assertGreaterEqual(out["short"], 300)


class RiverPaintTests(unittest.TestCase):
    """The river must really paint.  Regression for the black canvas in
    ?mode=river: the scene was built, but no frame was ever drawn."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.river = os.path.join(PROJECT_ROOT, "static", "js", "river.js")

    def _run(self, script_name, body):
        if not self.node:
            self.skipTest("node is not available")
        script = os.path.join(PROJECT_ROOT, ".tmp", script_name)
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with io.open(script, "w", encoding="utf-8") as handle:
            handle.write(body)
        proc = subprocess.run([self.node, script], cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8", "replace"))

    @staticmethod
    def _scene_harness_source(payload, river):
        return RiverPaintTests._harness_text(payload, river)

    def _scene_harness(self, payload):
        """Runs the real river.js against a three stub that behaves like three
        where it matters: an InstancedMesh starts with an all-zero matrix, and a
        zero matrix draws nothing at all, so an instance nobody wrote is
        invisible.  Time and requestAnimationFrame are controlled by the test,
        so the render loop is driven deterministically."""
        return self._harness_text(payload, self.river)

    @staticmethod
    def _harness_text(payload, river):
        return (
            "global.window = global;\n"
            "const fs = require('fs'), vm = require('vm');\n"
            "let scene = null;\n"
            "function scaleOf(e) { return Math.max(Math.abs(e[0]), Math.abs(e[5]), Math.abs(e[10])); }\n"
            "class Vec3 { constructor(x, y, z) { this.x = x || 0; this.y = y || 0; this.z = z || 0; }\n"
            "  set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; }\n"
            "  copy(v) { this.x = v.x; this.y = v.y; this.z = v.z; return this; }\n"
            "  project() { this.x = 0.1; this.y = 0.1; this.z = 0.9; return this; } }\n"
            "class Col { constructor(c) { this.r = 0.5; this.g = 0.5; this.b = 0.5; if (c !== undefined) this.set(c); }\n"
            "  set() { return this; } setRGB(r, g, b) { this.r = r; this.g = g; this.b = b; return this; }\n"
            "  lerp() { return this; } }\n"
            "class Obj3 { constructor() { this.children = []; this.position = new Vec3(); this.scale = new Vec3(1, 1, 1);\n"
            "  this.visible = true; this.frustumCulled = true; } add(o) { this.children.push(o); return o; }\n"
            "  remove(o) { return o; } lookAt() {} }\n"
            "class Geo { constructor() { this.attributes = {}; this.drawRange = { count: 0 }; }\n"
            "  setAttribute(n, a) { this.attributes[n] = a; return this; }\n"
            "  setDrawRange(s, c) { this.drawRange = { count: c }; } dispose() {} }\n"
            "class Attr { constructor(a, s) { this.array = a; this.itemSize = s; this.needsUpdate = false; } }\n"
            "class Mat { constructor(o) { Object.assign(this, o || {}); } dispose() {} }\n"
            "class Pts extends Obj3 { constructor(g, m) { super(); this.geometry = g; this.material = m; } }\n"
            "class Mesh extends Obj3 { constructor(g, m) { super(); this.geometry = g; this.material = m; } }\n"
            "class Lines extends Obj3 { constructor(g, m) { super(); this.geometry = g; this.material = m; } }\n"
            "class Inst extends Obj3 { constructor(g, m, n) { super(); this.geometry = g; this.material = m; this.count = 0;\n"
            "  this.cap = n; this.instanceMatrix = { array: new Float64Array(n * 16), setUsage() {}, needsUpdate: false };\n"
            "  this.instanceColor = null; }\n"
            "  setMatrixAt(i, m) { const o = i * 16; for (let k = 0; k < 16; k++) this.instanceMatrix.array[o + k] = m.elements[k]; }\n"
            "  setColorAt(i, c) { if (!this.instanceColor) { const a = new Float32Array(this.cap * 3); a.fill(1);\n"
            "      this.instanceColor = { array: a }; } this.instanceColor.array[i * 3] = c.r;\n"
            "    this.instanceColor.array[i * 3 + 1] = c.g; this.instanceColor.array[i * 3 + 2] = c.b; }\n"
            "  liveInstances() { let n = 0; for (let i = 0; i < this.count; i++)\n"
            "    if (scaleOf(this.instanceMatrix.array.slice(i * 16, i * 16 + 16)) > 1e-6) n++; return n; } }\n"
            "class V2 { constructor(x, y) { this.x = x || 0; this.y = y || 0; }\n"
            "  set(x, y) { this.x = x; this.y = y; return this; } }\n"
            "class M4 { constructor() { this.elements = new Float64Array(16); }\n"
            "  compose(p, q, s) { const e = this.elements; e[0] = s.x; e[5] = s.y; e[10] = s.z; e[15] = 1;\n"
            "    e[12] = p.x; e[13] = p.y; e[14] = p.z; return this; } }\n"
            "class Renderer { constructor() { this.domElement = { tagName: 'CANVAS', style: {}, parentNode: null,\n"
            "    addEventListener() {}, removeEventListener() {} }; this.info = { render: { calls: 0, triangles: 0 } }; }\n"
            "  setClearColor() {} setPixelRatio() {} setSize() {} dispose() {}\n"
            "  render(sc) { this.info.render.calls = 0; this.info.render.triangles = 0;\n"
            "    const walk = (o) => { if (!o.visible) return;\n"
            "      if (o instanceof Inst) { const live = o.liveInstances();\n"
            "        if (live > 0) { this.info.render.calls++; this.info.render.triangles += live * 36; } }\n"
            "      else if (o instanceof Mesh) { const c = o.geometry.drawRange.count;\n"
            "        if (c > 0) { this.info.render.calls++; this.info.render.triangles += c / 3; } }\n"
            "      else if (o instanceof Lines) { const c = o.geometry.drawRange.count;\n"
            "        if (c > 0) { this.info.render.calls++; this.info.render.triangles += c / 2; } }\n"
            "      else if (o instanceof Pts) { const pos = o.geometry.attributes.position;\n"
            "        const n = pos ? pos.array.length / 3 : 0; if (n > 0) { this.info.render.calls++; this.info.render.triangles += n; } }\n"
            "      else this.info.render.calls++; (o.children || []).forEach(walk); }; walk(sc); } }\n"
            "const THREE = { Scene: function () { scene = new Obj3(); scene.background = null; return scene; },\n"
            "  PerspectiveCamera: class extends Obj3 { updateProjectionMatrix() {} }, WebGLRenderer: Renderer,\n"
            "  Vector3: Vec3, Color: Col, Quaternion: class { set() { return this; } }, Matrix4: M4,\n"
            "  Points: Pts, LineSegments: Lines, Mesh: Mesh, InstancedMesh: Inst, BufferGeometry: Geo, BufferAttribute: Attr,\n"
            "  SphereGeometry: class extends Geo {}, MeshBasicMaterial: Mat, LineBasicMaterial: Mat, PointsMaterial: Mat,\n"
            "  ShaderMaterial: Mat, Vector2: V2, DoubleSide: 2, AdditiveBlending: 2,\n"
            "  CanvasTexture: class {}, AdditiveBlending: 2, DynamicDrawUsage: 35048, REVISION: '137-mock', Timer: null };\n"
            "global.THREE = THREE;\n"
            "// deterministic clock + frame queue: the test drives every frame itself\n"
            "let __now = 1000000, __tmpId = 1;\n"
            "let __queue = [];\n"
            "global.performance = { now: function () { return __now; } };\n"
            "global.requestAnimationFrame = function (cb) { const id = __tmpId++; __queue.push({ id: id, cb: cb }); return id; };\n"
            "global.cancelAnimationFrame = function (id) { __queue = __queue.filter(function (e) { return e.id !== id; }); };\n"
            "function pump(dt) { const q = __queue; __queue = []; __now += dt; q.forEach(function (e) { e.cb(__now); }); }\n"
            "function drain(frames, dt) { for (let i = 0; i < frames; i++) pump(dt == null ? 16 : dt); }\n"
            "const ctx2d = new Proxy({}, { get(t, p) { if (p in t) return t[p];\n"
            "  const f = () => ({ addColorStop() {} }); t[p] = f; return f; } });\n"
            "global.document = { hidden: false, visibilityState: 'visible',\n"
            "  createElement: (t) => t === 'canvas' ? { width: 0, height: 0, getContext: () => ctx2d }\n"
            "    : { style: {}, addEventListener() {}, removeEventListener() {} },\n"
            "  addEventListener() {}, removeEventListener() {} };\n"
            "const ctx = { window: global, document: global.document, console, THREE,\n"
            "  requestAnimationFrame: global.requestAnimationFrame, cancelAnimationFrame: global.cancelAnimationFrame,\n"
            "  performance: global.performance, setTimeout: setTimeout, clearTimeout: clearTimeout };\n"
            "vm.createContext(ctx); ctx.window = ctx; ctx.self = ctx;\n"
            "vm.runInContext(fs.readFileSync(%r, 'utf8'), ctx);\n"
            "const R = ctx.GRAFFRiver;\n"
            "const HOST = { clientWidth: 1280, clientHeight: 720, style: {},\n"
            "  addEventListener() {}, removeEventListener() {},\n"
            "  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1280, height: 720 }),\n"
            "  appendChild() {}, removeChild() {} };\n"
            "function chain(n) { const out = []; let prev = null;\n"
            "  const T = ['reason', 'message', 'tool_call', 'tool_result', 'file_edit', 'error'];\n"
            "  for (let i = 0; i < n; i++) { const id = 'n' + i;\n"
            "    out.push({ id: id, source: { id: id, ts: 1000 + i, type: T[i %% 6], actor: 'codex', parent: prev,\n"
            "      label: 'e' + i, meta: { name: 'tool' + i, call_id: 'c' + i } } }); prev = id; }\n"
            "  return out; }\n"
            "function feed(river, items) { const m = R.layout(items, true);\n"
            "  const ls = m.nodes.slice(1).map((n, i) => ({ from: m.nodes[i].id, to: n.id }));\n"
            "  river.setData(m.nodes, ls, {}); return m; }\n"
            % river
        ) + payload

    def test_empty_boot_then_data_paints(self):
        """Boot with no data, park the loop, then push events (the exact failure
        path of ?mode=river) and require a drawn, non-black frame."""
        out = self._run("river_paint_boot.js", self._scene_harness(
            "const river = R.create(HOST, {});\n"
            "river.renderOnce(); river.start();\n"
            "drain(3, 16);\n"
            "const empty = river.stats();\n"
            "river.stop();\n"
            "// the loop is now parked on an empty scene; data arrives afterwards\n"
            "feed(river, chain(300));\n"
            "drain(2, 16);   // the revived loop paints its own frames\n"
            "const after = river.stats();\n"
            "console.log(JSON.stringify({ emptyNodes: empty.nodes, emptyPainted: empty.painted,\n"
            "  nodes: after.nodes, running: after.running, painted: after.painted,\n"
            "  triangles: after.triangles, drawCalls: after.drawCalls, frames: after.frames,\n"
            "  written: river.writtenInstances() }));"))
        self.assertEqual(out["emptyNodes"], 0)
        self.assertFalse(out["emptyPainted"], "nothing to paint while the session is empty")
        self.assertGreater(out["nodes"], 0)
        self.assertTrue(out["running"], "a data update must bring the render loop back")
        self.assertTrue(out["painted"], "a real frame must be drawn once data arrives")
        self.assertGreater(out["triangles"], 0, "the canvas must not stay black")
        self.assertGreater(out["drawCalls"], 0)
        self.assertEqual(out["written"], out["nodes"],
                         "every node needs a matrix or its instance is invisible")

    def test_render_once_forces_a_frame(self):
        """renderOnce() is the outside hook a diagnostic check can call."""
        out = self._run("river_paint_once.js", self._scene_harness(
            "const river = R.create(HOST, {});\n"
            "feed(river, chain(120));\n"
            "river.stop();\n"
            "const before = river.stats();\n"
            "const drew = river.renderOnce();\n"
            "const after = river.stats();\n"
            "console.log(JSON.stringify({ drew: drew, beforeFrames: before.frames,\n"
            "  afterFrames: after.frames, triangles: after.triangles,\n"
            "  painted: after.painted, drawCalls: after.drawCalls }));"))
        self.assertTrue(out["drew"], "renderOnce must report that it drew")
        self.assertGreater(out["afterFrames"], out["beforeFrames"], "renderOnce must add a drawn frame")
        self.assertGreater(out["triangles"], 0, "a forced frame must produce geometry")
        self.assertTrue(out["painted"])

    def test_forced_frame_marks_every_instance(self):
        """A node written only by the steady pass must still be visible: three's
        InstancedMesh starts at an all-zero matrix, which draws nothing."""
        out = self._run("river_paint_instances.js", self._scene_harness(
            "const river = R.create(HOST, {});\n"
            "feed(river, chain(400));\n"
            "river.stop();\n"
            "river.renderOnce();\n"
            "const st = river.stats();\n"
            "console.log(JSON.stringify({ nodes: st.nodes, written: river.writtenInstances(),\n"
            "  painted: st.painted, triangles: st.triangles }));"))
        self.assertGreater(out["nodes"], 100)
        self.assertEqual(out["written"], out["nodes"], "an unwritten instance is an invisible node")
        self.assertTrue(out["painted"])
        self.assertGreater(out["triangles"], 0)

    def test_idle_loop_stays_live_but_throttles(self):
        """An idle scene keeps a correct canvas and stays live, but drops out of
        the 60 Hz loop instead of burning frames."""
        out = self._run("river_paint_idle.js", self._scene_harness(
            "const river = R.create(HOST, {});\n"
            "feed(river, chain(80));\n"
            "drain(90, 16);            // let the arrival burst settle (~1.5 s)\n"
            "const settled = river.stats();\n"
            "const f0 = settled.frames;\n"
            "drain(120, 100);          // 12 s of idle time\n"
            "const idle = river.stats();\n"
            "console.log(JSON.stringify({ settledIdle: settled.idle, settledPainted: settled.painted,\n"
            "  tri: idle.triangles, painted: idle.painted, running: idle.running, idle: idle.idle,\n"
            "  idleMs: idle.idleMs, sparkLimit: idle.sparkLimit,\n"
            "  idleFrames: idle.frames - f0, idlePumps: 120 }));"))
        self.assertTrue(out["settledPainted"], "the settled canvas must still hold a frame")
        self.assertGreater(out["tri"], 0)
        self.assertTrue(out["painted"])
        self.assertTrue(out["running"], "an idle river stays live (parked, not dead)")
        self.assertTrue(out["idle"], "nothing is animating, so the loop must go idle")
        self.assertGreater(out["idleMs"], 0, "an idle scene must be throttled, not looped at 60 Hz")
        self.assertLess(out["idleFrames"], out["idlePumps"],
                        "idle frames must be fewer than the frames that were pumped")
        self.assertLessEqual(out["sparkLimit"], 64, "the spark pool must stay bounded")

    def test_paint_surface_is_wired_into_the_loader_paths(self):
        river = RiverModeTests._read("static", "js", "river.js")
        app = RiverModeTests._read("static", "js", "app.js")
        # the module exposes the forced frame + the written-instance census
        self.assertIn("renderOnce: renderNow", river)
        self.assertIn("writtenInstances:", river)
        self.assertIn("function ensureMatrices(", river)
        self.assertIn("function writeSteady(", river)
        self.assertIn("IDLE_MS", river)
        # a data update revives the loop, and both entry paths paint a frame
        self.assertIn("if (!document.hidden) start();", river)
        self.assertIn("river.renderOnce();", app)
        self.assertIn("renderOnce: function () { return (river && river.renderOnce) ? river.renderOnce() : false; }", app)
        # a parked loop may not leave a stale canvas behind
        self.assertIn("if (drawFinal !== false && pendingFrame && !document.hidden) renderOnce(nowMs());", river)
        self.assertIn("stop(false);", river)   # dispose() must not paint


class RiverBrightnessTests(unittest.TestCase):
    """«Русло» must read on a dark background: a second additive halo pass and
    a ribbon per edge, both still one draw call each."""

    @classmethod
    def setUpClass(cls):
        cls.river = RiverModeTests._read("static", "js", "river.js")
        cls.app = RiverModeTests._read("static", "js", "app.js")
        cls.html = RiverModeTests._read("static", "index.html")

    def test_halo_is_a_second_instanced_pass(self):
        river = self.river
        # the halo shares the core matrix buffer and adds exactly one draw call
        self.assertIn("var halos = new THREE_.InstancedMesh(coreGeo, haloMat, MAX_NODES);", river)
        self.assertIn("halos.instanceMatrix = cores.instanceMatrix;", river)
        self.assertIn("halos.count = sceneNodes;", river)
        # it is additive and cheap - never a sprite per node
        self.assertIn("function haloMaterial(", river)
        self.assertIn("HALO_GROW", river)
        self.assertIn("HALO_OPACITY", river)
        self.assertNotIn("nodeThreeObject", river)

    def test_links_are_camera_facing_ribbons_in_one_geometry(self):
        river = self.river
        self.assertIn("LINK_VERTS", river)
        self.assertIn("attribute vec3 other;", river)
        self.assertIn("attribute vec2 corner;", river)
        self.assertIn("new THREE_.Mesh(linkGeo, linkMat)", river)
        # thickness is a uniform in pixels, never the ignored linewidth
        self.assertIn("uPx", river)
        # thickness is never delegated to the WebGL linewidth, which is ignored
        self.assertNotIn("linewidth:", river)
        # the ribbon shader declares its own colour attribute: a ShaderMaterial
        # does not get three's "attribute vec3 color" for free, and without it
        # the program fails to link and NO edge is drawn at all
        self.assertIn("attribute vec3 color;", river)
        marker = river.find("function linkMaterial(")
        self.assertIn("attribute vec3 color;", river[marker:marker + 1200])
        # the UI exposes the thickness and it is persisted
        self.assertIn('id="river-link-px"', self.html)
        self.assertIn("riverLinkPx", self.app)
        self.assertIn("linkWidth: linkWidth,", river)

    def test_brightness_is_a_table_not_a_per_frame_string(self):
        river = self.river
        # a saturated core: the palette is brightened by type, errors loudest
        self.assertIn("TYPE_GAIN", river)
        self.assertIn("COLOUR_BUCKETS", river)
        self.assertIn("buildColourTable", river)
        # old nodes cool into the deep violet, red stays with the errors
        self.assertIn("COLD_MIN", river)
        self.assertIn("cold:", river)
        self.assertIn('if (key === "error")', river)

    def test_no_allocation_inside_the_frame_loop(self):
        river = self.river
        for fn in ("writeNode", "flushDirty", "recolour", "updateSparks", "pruneActive", "loop", "renderOnce"):
            i = river.find("function " + fn + "(")
            self.assertGreater(i, 0, fn + " is missing")
            body = river[i:i + 3200]
            self.assertNotIn("new THREE_.Color(", body, fn + " must not allocate a Color")
            self.assertNotIn("new THREE_.Vector3(", body, fn + " must not allocate a Vector3")
            self.assertNotIn(".splice(", body, fn + " must not splice (allocation churn)")
            self.assertNotIn("coolColor(", body, fn + " must not rebuild a colour string")


class RiverPerfTests(unittest.TestCase):
    """The measured cost of the mode: nothing is re-uploaded unless it changed,
    an idle scene parks itself, and an upload is a partial range."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.river = RiverModeTests._read("static", "js", "river.js")

    def _river(self):
        return self.river

    def _run(self, name, body):
        if not self.node:
            self.skipTest("node is not available")
        script = os.path.join(PROJECT_ROOT, ".tmp", name)
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with io.open(script, "w", encoding="utf-8") as handle:
            handle.write(body)
        proc = subprocess.run([self.node, script], cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8", "replace"))

    def test_dirty_flags_gate_every_upload(self):
        river = self.river
        # one flag per buffer and a flush that respects them
        for flag in ("DIRTY_MATRIX", "DIRTY_COLOR", "DIRTY_LINK", "DIRTY_LINKCOLOR", "DIRTY_SPARK"):
            self.assertIn(flag, river)
        self.assertIn("function flushDirty()", river)
        self.assertIn("var d = renderDirty;", river)
        self.assertIn("if (!d) return;", river)
        # dead weight: the old code set every needsUpdate on every frame
        self.assertNotIn("cores.instanceMatrix.needsUpdate = true;\n      if (cores.instanceColor)", river)
        # a partial upload through three's updateRange
        self.assertIn("updateRange = ranged", river)

    def test_stats_carries_the_measurable_counters(self):
        river = self.river
        for key in ("uploadsPerSecond:", "frameMsMax:", "frameMsAvg:", "idleGap:",
                    "uploadMatrix:", "uploadColour:", "uploadSparks:", "uploadLinks:",
                    "uploadLinkGeometry:", "coolSettled:", "triangles:", "drawCalls:"):
            self.assertIn(key, river, "stats() must report " + key)
        self.assertIn("perfCounters", river)

    def test_geometry_is_light_and_the_scene_parks(self):
        out = self._run("river_perf_node.js", self._node_harness(self._perf_probe()))
        # triangles per node drop several-fold at the same look
        self.assertLess(out["trianglesPerNode"], 90, "the core sphere must be light")
        self.assertGreater(out["trianglesPerNode"], 10)
        self.assertIn("x", out["coreSegments"])
        # exactly: the node pass twice (cores + halos) plus the ribbon (plus the
        # spark pool when it holds live particles)
        self.assertLessEqual(out["drawCalls"], 5)
        self.assertGreaterEqual(out["drawCalls"], 3)
        self.assertEqual(out["haloCount"], out["nodes"], "every node gets a halo")
        self.assertGreater(out["haloGrow"], 1.2)
        # an idle scene parks for real: neither frames nor uploads
        self.assertEqual(out["idleFrames"], 0, "a parked loop must draw nothing")
        self.assertEqual(out["idleUploads"], 0, "an idle scene must upload nothing")
        # the mode stays live, but it stops ASKING for animation frames: no
        # frames, no uploads, nothing burning the CPU while the scene is settled
        self.assertTrue(out["idleRunning"], "a parked river is still a live mode")
        self.assertFalse(out["idleScheduled"], "after the grace window the loop stops asking for frames")
        self.assertGreater(out["idleGap"], 0)
        self.assertTrue(out["coolSettled"], "the palette must reach a fixed point")
        # Animated frames legitimately re-upload, but only the animated span:
        # the elements pushed stay far below "every frame rewrites every node".
        self.assertLess(out["burstMatrixElems"], out["burstFrames"] * out["nodes"] * 0.5,
                        "an animated frame must upload a partial range, not the whole scene")
        self.assertLess(out["uploadsPerSecond"], 200)
        self.assertTrue(out["painted"])

    def _node_harness(self, payload):
        # Reuse the proven scene harness: the real river.js plus a three stub
        # that behaves like three where it matters (an instance nobody wrote is
        # invisible).  The probe below drives it and reports the counters.
        # The scene harness is a plain function of the river source; reuse it
        # without instantiating the test case.
        # _harness_text reads the river through fs.readFileSync, so it needs the
        # path, not the source text.
        river_path = os.path.join(PROJECT_ROOT, "static", "js", "river.js")
        return RiverPaintTests._scene_harness_source(payload, river_path)

    def _perf_probe(self):
        """Measures the effect of the perf work from INSIDE the scene harness:
        the counter surface is the module's own stats()/perfCounters()."""
        return (
            "const river = R.create(HOST, {});\n"
            "const m = feed(river, chain(900));\n"
            "drain(80, 16);                     // the arrival burst settles\n"
            "const a = river.stats(), pa = river.perfCounters();\n"
            "drain(600, 16);                    // wind down: active nodes age out\n"
            "const b = river.stats(), pb = river.perfCounters();\n"
            "drain(400, 16);                    // parked: nothing left to animate\n"
            "const c = river.stats(), pc = river.perfCounters();\n"
            "console.log(JSON.stringify({\n"
            "  nodes: c.nodes, links: c.links, drawCalls: c.drawCalls, triangles: c.triangles,\n"
            "  trianglesPerNode: Math.round(c.triangles / c.nodes),\n"
            "  painted: c.painted, written: river.writtenInstances(),\n"
            "  burstFrames: a.frames, burstMatrixUploads: pa.uploadMatrix,\n"
            "  burstMatrixElems: pa.uploadMatrixElems,\n"
            "  idleFrames: c.frames - b.frames, idleUploads: (pc.uploadMatrix - pb.uploadMatrix) + (pc.uploadColour - pb.uploadColour) + (pc.uploadSparks - pb.uploadSparks),\n"
            "  idleRunning: c.running, idleScheduled: c.scheduled, idleGap: c.idleGap, coolSettled: c.coolSettled,\n"
            "  uploadsPerSecond: c.uploadsPerSecond, frameMsMax: c.frameMsMax,\n"
            "  haloCount: c.haloCount, haloGrow: c.haloGrow, haloOpacity: c.haloOpacity,\n"
            "  coreSegments: c.coreSegments, linkPx: c.linkPx, sparkPointsWritten: c.sparkPointsWritten\n"
            "}));"
        )



class RiverRobustnessTests(unittest.TestCase):
    """The crash that blanked the canvas must be impossible: a record the scene
    cannot resolve is COUNTED and skipped, never dereferenced, and a broken row
    is reported to the journal instead of throwing."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.river_src = RiverModeTests._read("static", "js", "river.js")
        cls.app = RiverModeTests._read("static", "js", "app.js")
        cls.html = RiverModeTests._read("static", "index.html")

    def _run(self, name, body):
        if not self.node:
            self.skipTest("node is not available")
        river_path = os.path.join(PROJECT_ROOT, "static", "js", "river.js")
        script = os.path.join(PROJECT_ROOT, ".tmp", name)
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with io.open(script, "w", encoding="utf-8") as handle:
            handle.write(RiverPaintTests._harness_text(body, river_path))
        proc = subprocess.run([self.node, script], cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8", "replace"))

    # ---- static: the fix is the one that was identified ----------------------

    def test_row_of_never_dereferences_an_unknown_node(self):
        src = self.river_src
        marker = src.find("function rowOf(n) {")
        self.assertGreater(marker, 0)
        body = src[marker:marker + 700]
        # the guard is the FIRST thing rowOf does: the old first line was the
        # crash itself ("Cannot read properties of undefined (reading
        # 'endOfStream')"), so it may not lead the function any more.
        self.assertIn('if (!n || typeof n !== "object")', body)
        self.assertNotRegex(body, r"^\s*if \(n\.endOfStream\) return rowEnd;")

    def test_unknown_type_falls_back_to_tool_call_not_message(self):
        src = self.river_src
        self.assertIn('typeSlot[n.actor === "user" ? "user" : "tool_call"]', src)
        self.assertNotIn('typeSlot[n.actor === "user" ? "user" : "message"]', src)

    def test_group_nodes_get_a_colourful_twin_row_never_grey(self):
        src = self.river_src
        self.assertIn("mutedBase", src)
        self.assertIn("if (n.count > 1 && mutedBase >= 0) return mutedBase + row;", src)
        # the twin rows are built from the type rows, pulled towards the cold hue
        self.assertIn("mutedBase = colourTable.length;", src)

    def test_cooling_is_measured_against_the_session_not_the_wall_clock(self):
        src = self.river_src
        self.assertIn("function sessionFrac(ts)", src)
        self.assertIn("function ageFracOf(ts)", src)
        self.assertEqual(src.count("fracAt("), 0, "the dead wall-clock helper must be gone")
        # sessionFrac reads only the oldest/newest timestamps of the session
        i = src.find("function sessionFrac(ts)")
        self.assertIn("newestTs - oldestTs", src[i:i + 400])

    def test_dangling_links_are_counted_not_thrown(self):
        src = self.river_src
        self.assertIn("dangling", src)
        self.assertIn("if (!a || !b) { dangling++; continue; }", src)
        self.assertIn("danglingLinks: dangling,", src)

    def test_the_pixel_self_check_is_exposed_to_the_app(self):
        src = self.river_src
        self.assertIn("function selfCheck()", src)
        self.assertIn("renderer.readRenderTargetPixels(", src)
        for key in ("nonBlack", "hues", "linePixels", "nodesInFrame"):
            self.assertIn(key, src, "selfCheck must report " + key)
        self.assertIn("selfCheck: selfCheck,", src)
        # it reaches the diagnostics panel and the test hook in app.js
        self.assertIn("river.selfCheck()", self.app)
        self.assertIn('id="river-check"', self.html)

    def test_a_broken_record_is_reported_to_the_app_journal(self):
        # the scene takes a warn hook, and app.js routes it to the same glLog the
        # error block shows
        self.assertIn("onWarn", self.river_src)
        self.assertIn("function warn(msg)", self.river_src)
        self.assertIn("onWarn: function (msg) { pushLog(", self.app)
        self.assertIn('id="river-warn"', self.html)

    # ---- harness: a broken record must still leave a painted canvas ----------

    def test_dangling_edge_is_skipped_and_counted(self):
        out = self._run("river_robust_dangling.js",
            "const river = R.create(HOST, {});\n"
            "const m = R.layout(chain(60), true);\n"
            "const ls = m.nodes.slice(1).map((n, i) => ({ from: m.nodes[i].id, to: n.id }));\n"
            "// a rotten edge with an endpoint that is not in the scene\n"
            "ls.push({ from: 'r:ghost', to: m.nodes[0].id });\n"
            "ls.push({ from: m.nodes[0].id, to: 'r:ghost' });\n"
            "river.setData(m.nodes, ls, {});\n"
            "river.renderOnce();\n"
            "const st = river.stats();\n"
            "console.log(JSON.stringify({ dangling: st.danglingLinks, nodes: st.nodes,\n"
            "  painted: st.painted, triangles: st.triangles, links: st.links }));")
        self.assertGreaterEqual(out["dangling"], 2, "both rotten edges must be counted")
        self.assertGreater(out["nodes"], 0)
        self.assertTrue(out["painted"], "a dangling edge must not blank the canvas")
        self.assertGreater(out["triangles"], 0)

    def test_undefined_chain_endpoint_does_not_throw(self):
        # the exact shape of the browser crash: a link whose 'to' is not a string
        out = self._run("river_robust_undefined.js",
            "const river = R.create(HOST, {});\n"
            "const m = R.layout(chain(40), true);\n"
            "const ls = [{ from: m.nodes[0].id, to: undefined },\n"
            "  { from: null, to: m.nodes[1].id }, { from: m.nodes[0].id, to: m.nodes[1].id }];\n"
            "river.setData(m.nodes, ls, {});\n"
            "river.renderOnce();\n"
            "const st = river.stats();\n"
            "console.log(JSON.stringify({ dangling: st.danglingLinks, nodes: st.nodes,\n"
            "  painted: st.painted, warned: st.warnings, last: st.lastWarning }));")
        self.assertGreaterEqual(out["dangling"], 2)
        self.assertTrue(out["painted"], "a non-string endpoint must not throw")
        self.assertGreaterEqual(out["warned"], 1, "the skip must be reported")

    def test_empty_and_null_data_do_not_throw(self):
        out = self._run("river_robust_empty.js",
            "const river = R.create(HOST, {});\n"
            "river.setData(null, null, {});\n"
            "river.setData([], [], {});\n"
            "river.renderOnce();\n"
            "const st = river.stats();\n"
            "console.log(JSON.stringify({ nodes: st.nodes, links: st.links }));")
        self.assertEqual(out["nodes"], 0)
        self.assertEqual(out["links"], 0)

    def test_self_check_is_supported_under_the_stub(self):
        # the stub renderer has no render targets: selfCheck must say so instead
        # of throwing, so the app can show a dash
        out = self._run("river_robust_selfcheck.js",
            "const river = R.create(HOST, {});\n"
            "feed(river, chain(50));\n"
            "const r = river.selfCheck();\n"
            "console.log(JSON.stringify({ obj: typeof r, supported: r.supported,\n"
            "  nonBlack: r.nonBlack, hues: r.hues, lines: r.linePixels }));")
        self.assertEqual(out["obj"], "object")
        self.assertIn("supported", out)


    def test_a_prototype_keyed_endpoint_is_skipped_not_dereferenced(self):
        # The one way an endpoint can slip past "=== undefined": the id -> index
        # map is a plain {}, so an id like "constructor" reads the INHERITED
        # Object.prototype.constructor -- a function, not undefined -- and the
        # old `nodeList[fromIdx]` then indexed by a non-number.  That is what put
        # an undefined into rowOf and threw
        # "Cannot read properties of undefined (reading endOfStream)".
        out = self._run("river_robust_prototype.js",
            "const river = R.create(HOST, {});\n"
            "const m = R.layout(chain(40), false);\n"
            "const ls = m.nodes.slice(1).map((n, i) => ({ from: m.nodes[i].id, to: n.id }));\n"
            "ls.push({ from: m.nodes[0].id, to: 'constructor' });\n"
            "ls.push({ from: 'toString', to: m.nodes[1].id });\n"
            "river.setData(m.nodes, ls, {});\n"
            "river.renderOnce();\n"
            "const st = river.stats();\n"
            "console.log(JSON.stringify({ dangling: st.danglingLinks, nodes: st.nodes,\n"
            "  links: st.links, painted: st.painted, warned: st.warnings }));")
        self.assertGreaterEqual(out["dangling"], 2, "both prototype-keyed edges must be counted")
        self.assertGreater(out["nodes"], 0)
        self.assertGreater(out["links"], 0)
        self.assertTrue(out["painted"], "a prototype-keyed endpoint must not blank the canvas")

    def test_the_link_guard_rejects_a_non_numeric_index(self):
        # the guard is on the MAPPED index, not on the string id: a value that
        # came back from the map must still be a number inside the scene
        src = self.river_src
        i = src.find("var fromIdx =")
        guard = src[i:i + 900]
        self.assertIn('typeof lk0.from === "string"', guard)
        self.assertIn("indexOfId[lk0.from]", guard)
        self.assertIn("if (!a || !b) { dangling++; continue; }", src)


class RiverIdentityMapTests(unittest.TestCase):
    """The crash was a PROTOTYPE KEY, not a missing node: indexOfId was a plain
    {}, so an id like "constructor" read back Object.prototype.constructor (a
    function), sailed past the `=== undefined` guard, and nodeList[fn] handed an
    undefined to rowOf().  Every id-keyed map is now null-prototype."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.river_src = RiverModeTests._read("static", "js", "river.js")
        cls.app = RiverModeTests._read("static", "js", "app.js")

    def _run(self, name, body):
        if not self.node:
            self.skipTest("node is not available")
        river_path = os.path.join(PROJECT_ROOT, "static", "js", "river.js")
        script = os.path.join(PROJECT_ROOT, ".tmp", name)
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with io.open(script, "w", encoding="utf-8") as handle:
            handle.write(RiverPaintTests._harness_text(body, river_path))
        proc = subprocess.run([self.node, script], cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8", "replace"))

    def test_every_id_map_is_null_prototype(self):
        for src in (self.river_src, self.app):
            self.assertIn("Object.create(null)", src)
        self.assertIn("function newIdMap() { return Object.create(null); }", self.river_src)
        self.assertIn("var indexOfId = newIdMap();", self.river_src)
        self.assertIn("var seenLink = newIdMap();", self.river_src)
        self.assertIn("var byRaw = newIdMap();", self.river_src)
        self.assertIn("function idMap() { return Object.create(null); }", self.app)
        self.assertIn("var rawNodes = idMap();", self.app)
        self.assertIn("var edgeIds = idMap();", self.app)

    def test_a_prototype_keyed_id_is_not_a_node(self):
        # "constructor" must read back as ABSENT: the edge is dropped and counted
        # instead of resolving to Object.prototype.constructor
        out = self._run("river_proto_map.js",
            "const river = R.create(HOST, {});\n"
            "const m = R.layout(chain(30), false);\n"
            "const ls = [{ from: m.nodes[0].id, to: 'constructor' },\n"
            "  { from: 'valueOf', to: m.nodes[1].id },\n"
            "  { from: m.nodes[0].id, to: m.nodes[1].id }];\n"
            "river.setData(m.nodes, ls, {});\n"
            "river.renderOnce();\n"
            "const st = river.stats();\n"
            "console.log(JSON.stringify({ nodes: st.nodes, links: st.links,\n"
            "  dangling: st.danglingLinks, painted: st.painted, tris: st.triangles }));")
        self.assertGreater(out["nodes"], 0)
        self.assertEqual(out["links"], 1, "only the real edge survives")
        self.assertGreaterEqual(out["dangling"], 2)
        self.assertTrue(out["painted"])
        self.assertGreater(out["tris"], 0)

    def test_a_prototype_keyed_node_id_is_not_a_dangling_parent(self):
        # the same key as a NODE id: it must be a real node, not read as absent
        out = self._run("river_proto_node.js",
            "const river = R.create(HOST, {});\n"
            "const m = R.layout(chain(12), false);\n"
            "m.nodes[5].id = 'toString';\n"
            "const ls = [{ from: m.nodes[4].id, to: 'toString' },\n"
            "  { from: 'toString', to: m.nodes[6].id }];\n"
            "river.setData(m.nodes, ls, {});\n"
            "river.renderOnce();\n"
            "const st = river.stats();\n"
            "console.log(JSON.stringify({ nodes: st.nodes, links: st.links,\n"
            "  dangling: st.danglingLinks, painted: st.painted }));")
        self.assertEqual(out["nodes"], 12)
        self.assertEqual(out["links"], 2, "both edges land on the real node")
        self.assertEqual(out["dangling"], 0)
        self.assertTrue(out["painted"])

    def test_the_newest_event_is_the_hot_end_of_the_session(self):
        # cooling is anchored at the newest event: the freshest node keeps its
        # own hue at full strength and the oldest node is the coolest.  The
        # reference is the session, never the wall clock.
        out = self._run("river_cool_dir.js",
            "const river = R.create(HOST, {});\n"
            "const items = [];\n"
            "for (var i = 0; i < 40; i++)\n"
            "  items.push({ id: 'e' + i, source: { id: 'e' + i, ts: 1000 + i * 10,\n"
            "    type: 'reason', actor: 'codex' } });\n"
            "const m = R.layout(items, false);\n"
            "const ls = m.nodes.slice(1).map(function (k, i) { return { from: m.nodes[i].id, to: k.id }; });\n"
            "river.setData(m.nodes, ls, {});\n"
            "const nodes = river.nodes();\n"
            "// the two landmarks carry their own rows; skip index 0 for the span\n"
            "const buckets = nodes.slice(1).map(function (n) { return n.bucket; });\n"
            "console.log(JSON.stringify({ oldest: nodes[1].bucket, newest: nodes[nodes.length - 1].bucket,\n"
            "  buckets: buckets, hot: Math.min.apply(null, buckets), cold: Math.max.apply(null, buckets) }));")
        # bucket 0 is the FRESHEST (its own hue at full strength); the largest
        # bucket is the oldest.  So newest < oldest, and the span is bounded, so
        # the oldest node still keeps most of its own colour.
        self.assertLess(out["newest"], out["oldest"], "the newest node must be the hottest")
        self.assertEqual(out["newest"], out["hot"], "the freshest node is bucket 0")
        self.assertEqual(out["oldest"], out["cold"], "the oldest plain node is the coolest")
        self.assertGreater(out["cold"], out["hot"])
        self.assertLess(out["cold"], 110, "even the oldest node must keep its own hue")


class RiverSceneSurvivalTests(unittest.TestCase):
    """No single bad record may blank the canvas: setData() is atomic, keeps the
    previous correct scene, and reports the failure to the journal."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.river_src = RiverModeTests._read("static", "js", "river.js")

    def _run(self, name, body):
        if not self.node:
            self.skipTest("node is not available")
        river_path = os.path.join(PROJECT_ROOT, "static", "js", "river.js")
        script = os.path.join(PROJECT_ROOT, ".tmp", name)
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with io.open(script, "w", encoding="utf-8") as handle:
            handle.write(RiverPaintTests._harness_text(body, river_path))
        proc = subprocess.run([self.node, script], cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8", "replace"))

    def test_set_data_is_transactional(self):
        src = self.river_src
        self.assertIn("function setDataInner(", src)
        self.assertIn("return setDataInner(riverNodes, links, opts2);", src)
        i = src.find("function setData(riverNodes, links, opts2) {")
        self.assertIn("try {", src[i:i + 200])
        self.assertIn("warn(", src[i:i + 600])

    def test_a_throwing_record_keeps_the_previous_scene(self):
        out = self._run("river_atomic.js",
            "const river = R.create(HOST, {});\n"
            "feed(river, chain(80));\n"
            "const before = river.stats();\n"
            "// a getter that throws halfway through the node pass\n"
            "const hostile = { id: 'boom', type: 'reason', actor: 'codex', ts: 1,\n"
            "  get x() { throw new Error('bad record'); } };\n"
            "const res = river.setData([hostile], [], {});\n"
            "river.renderOnce();\n"
            "const after = river.stats();\n"
            "console.log(JSON.stringify({ beforeNodes: before.nodes, afterNodes: after.nodes,\n"
            "  painted: after.painted, tris: after.triangles, warned: after.warnings,\n"
            "  last: after.lastWarning, err: res && res.error ? res.error : null,\n"
            "  resNodes: res.added }));")
        self.assertGreater(out["beforeNodes"], 0)
        self.assertGreater(out["afterNodes"], 0, "the previous scene must survive")
        self.assertTrue(out["painted"], "the canvas must still hold a frame")
        self.assertGreater(out["tris"], 0)
        self.assertGreaterEqual(out["warned"], 1, "the failure must reach the journal")
        self.assertIn("сборка сцены", out["last"])
        self.assertTrue(out["err"], "setData must report the failure, not throw")


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
