#!/usr/bin/env python3
"""Stdlib unittest for the `ferry-dash` HTTP server, CLI entrypoint, and helpers.

Run:  python3 lib/ferry-dashserver.test.py
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load ferry-dash as a module by path
_spec = importlib.util.spec_from_loader(
    "ferrydash_server",
    importlib.machinery.SourceFileLoader("ferrydash_server", os.path.join(REPO, "ferry-dash")),
)
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)


class MainCliTests(unittest.TestCase):
    def test_help_flag_exits_zero(self):
        with io.StringIO() as buf, unittest.mock.patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as ctx:
                dash.main(["--help"])
            self.assertEqual(ctx.exception.code, 0)

    def test_invalid_flag_exits_two(self):
        with io.StringIO() as buf, unittest.mock.patch("sys.stderr", buf):
            with self.assertRaises(SystemExit) as ctx:
                dash.main(["--nonexistent-flag"])
            self.assertEqual(ctx.exception.code, 2)

    def test_main_serve_false_binds_ephemeral_port(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as cfg:
            cfg.write("model_list: []\n")
            cfg.flush()
            srv = dash.main(["--port", "0", "--config", cfg.name], serve=False)
            try:
                self.assertIsNotNone(srv)
                port = srv.server_address[1]
                self.assertGreater(port, 0)
            finally:
                srv.server_close()

    def test_main_open_flag_does_not_crash(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as cfg:
            cfg.write("model_list: []\n")
            cfg.flush()
            with unittest.mock.patch("webbrowser.open", return_value=True) as mock_open:
                srv = dash.main(["--port", "0", "--open", "--config", cfg.name], serve=False)
                try:
                    self.assertIsNotNone(srv)
                    mock_open.assert_called_once()
                finally:
                    srv.server_close()

    def test_main_open_exception(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as cfg:
            cfg.write("model_list: []\n")
            cfg.flush()
            with unittest.mock.patch("webbrowser.open", side_effect=Exception("browser fail")):
                srv = dash.main(["--port", "0", "--open", "--config", cfg.name], serve=False)
                try:
                    self.assertIsNotNone(srv)
                finally:
                    srv.server_close()

    def test_main_serve_keyboard_interrupt(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as cfg:
            cfg.write("model_list: []\n")
            cfg.flush()
            with unittest.mock.patch.object(dash.ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt):
                with io.StringIO() as buf, unittest.mock.patch("sys.stdout", buf):
                    code = dash.main(["--port", "0", "--config", cfg.name], serve=True)
                    self.assertEqual(code, 0)
                    self.assertIn("ferry-dash stopped", buf.getvalue())

    def test_bind_failure_raises_system_exit(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            with self.assertRaises(SystemExit) as ctx:
                dash.main(["--port", str(port)], serve=False)
            self.assertIn("cannot bind", str(ctx.exception))
        finally:
            sock.close()


class HelperTests(unittest.TestCase):
    def test_lan_ip_returns_string(self):
        ip = dash.lan_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(len(ip) > 0)

    def test_lan_ip_exception_fallback(self):
        with unittest.mock.patch("socket.socket", side_effect=Exception("socket error")):
            self.assertEqual(dash.lan_ip(), "127.0.0.1")

    def test_find_log_returns_none_or_path(self):
        res = dash.find_log(99999)
        self.assertTrue(res is None or isinstance(res, str))

    def test_http_json_network_error(self):
        res = dash.http_json("http://127.0.0.1:1/none", "key", timeout=0.5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 0)

    def test_http_json_options_and_error_handling(self):
        class MockResp:
            status = 200
            headers = [("content-type", "application/json")]
            def read(self):
                return b"{\"success\": true}"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with unittest.mock.patch("urllib.request.urlopen", return_value=MockResp()):
            res = dash.http_json("http://mock/api", "mykey", method="POST", body={"test": 1}, headers={"X-Header": "Val"})
            self.assertTrue(res["ok"])
            self.assertEqual(res["json"], {"success": True})

        http_err = urllib.error.HTTPError("http://mock/api", 400, "Bad Request", [("content-type", "text/plain")], io.BytesIO(b"Non-json error text"))
        with unittest.mock.patch("urllib.request.urlopen", side_effect=http_err):
            res2 = dash.http_json("http://mock/api", "mykey")
            self.assertFalse(res2["ok"])
            self.assertEqual(res2["status"], 400)
            self.assertEqual(res2["text"], "Non-json error text")

    def test_http_stream_success_and_errors(self):
        class MockStream:
            status = 200
            headers = [("content-type", "text/event-stream")]
            def read(self):
                return b"data: test\n\n"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with unittest.mock.patch("urllib.request.urlopen", return_value=MockStream()):
            res = dash.http_stream("http://mock/stream", "key", body={"stream": True})
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], 200)

        err = urllib.error.HTTPError("http://mock/stream", 500, "Server Error", [("content-type", "text/plain")], io.BytesIO(b"Backend exploded"))
        with unittest.mock.patch("urllib.request.urlopen", side_effect=err):
            res2 = dash.http_stream("http://mock/stream", "key")
            self.assertFalse(res2["ok"])
            self.assertEqual(res2["status"], 500)
            self.assertEqual(res2["error"], "Backend exploded")

        with unittest.mock.patch("urllib.request.urlopen", side_effect=OSError("network drop")):
            res3 = dash.http_stream("http://mock/stream", "key")
            self.assertFalse(res3["ok"])
            self.assertEqual(res3["status"], 0)
            self.assertIn("network drop", res3["error"])

    def test_short_err(self):
        self.assertEqual(dash._short_err({"json": {"error": {"message": "msg text"}}}), "msg text")
        self.assertEqual(dash._short_err({"json": {"error": {}}}), None)
        self.assertEqual(dash._short_err({"error": "top level error"}), "top level error")
        self.assertEqual(dash._short_err({"text": "plain text error"}), "plain text error")
        self.assertIsNone(dash._short_err({}))

    def test_module_loaders_fallback(self):
        orig_live = dash._LIVE
        orig_cat = dash._CATALOG
        orig_prov = dash._PROVIDER_RULE
        try:
            dash._LIVE = None
            dash._CATALOG = None
            dash._PROVIDER_RULE = None
            with unittest.mock.patch("importlib.util.spec_from_loader", side_effect=Exception("load err")):
                self.assertIsNone(dash._live())
                self.assertIsNone(dash._catalog())
                rule = dash._provider_rule()
                self.assertEqual(rule("openai/gpt-4", "http://base"), "")
        finally:
            dash._LIVE = orig_live
            dash._CATALOG = orig_cat
            dash._PROVIDER_RULE = orig_prov

    def test_activity_and_classify_branches(self):
        self.assertIsNone(dash._classify_tap_line({}, "200 OK message", live=None))

        with unittest.mock.patch("os.path.getsize", side_effect=OSError("getsize fail")):
            act = dash.Activity("/tmp/mock.log")
            self.assertEqual(act.offset, 0)

        act_none = dash.Activity(None)
        act_none.poll()

        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("line 1\n")
            p = f.name
        try:
            act = dash.Activity(p)
            act.offset = 1000
            act.poll()
            self.assertEqual(act.offset, os.path.getsize(p))

            with unittest.mock.patch("builtins.open", side_effect=OSError("read err")):
                act.poll()
        finally:
            os.unlink(p)

    def test_lane_chains_error_branches(self):
        orig_live = dash._LIVE
        try:
            dash._LIVE = False
            self.assertEqual(dash._lane_chains(), {})

            mock_live = unittest.mock.MagicMock()
            mock_live.chains.side_effect = Exception("chains fail")
            dash._LIVE = mock_live
            self.assertEqual(dash._lane_chains(), {})
        finally:
            dash._LIVE = orig_live


CONFIG = """\
model_list:
  - model_name: heavy
    litellm_params:
      model: anthropic/k3
    model_info:
      public: true
      id: kimi-1

  - model_name: heavy-glm
    litellm_params:
      model: zai/glm-5.3
    model_info:
      id: glm-1

  - model_name: flash
    litellm_params:
      model: zai/glm-5.3-flash
    model_info:
      public: true
      id: flash-1

  - model_name: flash-or
    litellm_params:
      model: openrouter/some/model
    model_info:
      id: or-1

router_settings:
  routing_strategy: usage-based-routing-v2
  fallbacks: [{"heavy": ["heavy-glm"]}, {"flash": ["flash-or"]}]
  cooldown_time: 5
"""


class HttpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.cfg_path = os.path.join(cls.tmp_dir.name, "litellm.yaml")
        with open(cls.cfg_path, "w") as f:
            f.write(CONFIG)
        cls.events_path = os.path.join(cls.tmp_dir.name, "events.ndjson")
        with open(cls.events_path, "w") as f:
            f.write(json.dumps({"lane": "heavy", "event": "ok", "time": "2026-09-07T12:00:00Z"}) + "\n")

        cls.srv = dash.main([
            "--port", "0",
            "--config", cls.cfg_path,
            "--events", cls.events_path,
        ], serve=False)
        cls.port = cls.srv.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp_dir.cleanup()

    def _get(self, path):
        req = urllib.request.Request(f"{self.base_url}{path}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get_content_type(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get_content_type(), e.read()

    def _post(self, path, body_dict):
        data = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get_content_type(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get_content_type(), e.read()

    def test_root_returns_html(self):
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html")
        self.assertIn(b"<!doctype html>", body)
        self.assertIn(b"ferry", body)

    def test_status_endpoint(self):
        status, ctype, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        data = json.loads(body.decode())
        self.assertIn("topology", data)

    def test_probe_endpoint(self):
        with unittest.mock.patch.object(dash, "probe_backends", return_value={"mock": True}):
            status, ctype, body = self._get("/probe")
            self.assertEqual(status, 200)
            self.assertEqual(ctype, "application/json")
            data = json.loads(body.decode())
            self.assertEqual(data, {"mock": True})

    def test_openrouter_models_endpoint(self):
        status, ctype, body = self._get("/api/models/openrouter")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        data = json.loads(body.decode())
        self.assertIn("models", data)

    def test_unknown_get_returns_404(self):
        status, ctype, body = self._get("/api/nonexistent")
        self.assertEqual(status, 404)
        self.assertEqual(ctype, "text/plain")

    def test_order_preview_valid(self):
        payload = {
            "order": {
                "heavy": ["heavy", "heavy-glm"],
                "flash": ["flash", "flash-or"]
            }
        }
        status, ctype, body = self._post("/api/routes/order/preview", payload)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        data = json.loads(body.decode())
        self.assertIn("diff", data)
        self.assertEqual(data["errors"], [])

    def test_order_preview_invalid_body(self):
        req = urllib.request.Request(
            f"{self.base_url}/api/routes/order/preview",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
        self.assertEqual(status, 400)
        self.assertIn(b"bad request", body)

    def test_promote_preview_invalid_types(self):
        payload = {"lane": 123, "hop": None}
        status, ctype, body = self._post("/api/routes/promote/preview", payload)
        self.assertEqual(status, 400)

    def test_unknown_post_returns_404(self):
        status, ctype, body = self._post("/api/unsupported", {"some": "data"})
        self.assertEqual(status, 404)

    def test_events_stream_sse(self):
        def append_later():
            time.sleep(0.3)
            with open(self.events_path, "a") as f:
                f.write(json.dumps({
                    "lane": "heavy",
                    "resp_bytes": 1000,
                    "total_duration_ms": 100,
                    "time": "2026-09-07T12:01:00Z"
                }) + "\n")

        t = threading.Thread(target=append_later)
        t.start()
        req = urllib.request.Request(f"{self.base_url}/api/events")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers.get_content_type(), "text/event-stream")
            line = r.readline()
            self.assertTrue(line.startswith(b"data: "))
        t.join()

    def test_events_stream_exceeds_max_limit(self):
        orig_max = dash.MAX_STREAMS
        orig_count = dash.CFG.get("streams", 0)
        try:
            dash.MAX_STREAMS = 1
            dash.CFG["streams"] = 1
            status, ctype, body = self._get("/api/events")
            self.assertEqual(status, 503)
            self.assertIn(b"too many event streams", body)
        finally:
            dash.MAX_STREAMS = orig_max
            dash.CFG["streams"] = orig_count

    def test_fleet_endpoint_post(self):
        with unittest.mock.patch.object(dash, "forward_fleet", return_value=(200, {"fleet": "domestic"})):
            status, ctype, body = self._post("/api/fleet", {"fleet": "domestic"})
            self.assertEqual(status, 200)
            data = json.loads(body.decode())
            self.assertEqual(data["fleet"], "domestic")

    def test_promote_preview_valid(self):
        with unittest.mock.patch.object(dash, "diff_promote", return_value=("--- diff", [])):
            with unittest.mock.patch.object(dash, "hotswap_promote", return_value="live ok"):
                status, ctype, body = self._post("/api/routes/promote/preview", {"lane": "heavy", "hop": "heavy-glm"})
                self.assertEqual(status, 200)
                data = json.loads(body.decode())
                self.assertEqual(data["diff"], "--- diff")
                self.assertEqual(data["live"], "live ok")

    def test_promote_apply_valid(self):
        with unittest.mock.patch.object(dash, "apply_promote", return_value=("snap1", "--- diff")):
            with unittest.mock.patch.object(dash, "hotswap_promote", return_value="live ok"):
                status, ctype, body = self._post("/api/routes/promote/apply", {"lane": "heavy", "hop": "heavy-glm"})
                self.assertEqual(status, 200)
                data = json.loads(body.decode())
                self.assertTrue(data["ok"])
                self.assertIn("Config written", data["note"])

    def test_order_apply_valid(self):
        with unittest.mock.patch.object(dash, "apply_order", return_value=("snap2", "--- diff2")):
            with unittest.mock.patch.object(dash, "hotswap_reorder", return_value="live swapped"):
                payload = {
                    "order": {
                        "heavy": ["heavy", "heavy-glm"],
                        "flash": ["flash", "flash-or"]
                    }
                }
                status, ctype, body = self._post("/api/routes/order/apply", payload)
                self.assertEqual(status, 200)
                data = json.loads(body.decode())
                self.assertTrue(data["ok"])
                self.assertIn("live swapped", data["note"])

    def test_handle_one_request_reset_handling(self):
        class DummyHandler(dash.Handler):
            def __init__(self):
                self.close_connection = False

        h = DummyHandler()
        with unittest.mock.patch("http.server.BaseHTTPRequestHandler.handle_one_request", side_effect=ConnectionResetError):
            dash.Handler.handle_one_request(h)
            self.assertTrue(h.close_connection)

    def test_openrouter_catalog_unavailable_and_error(self):
        with unittest.mock.patch.object(dash, "_catalog", return_value=None):
            status, ctype, body = self._get("/api/models/openrouter")
            self.assertEqual(status, 200)
            data = json.loads(body.decode())
            self.assertTrue(data["stale"])
            self.assertIn("unavailable", data["error"])

        mock_cat = unittest.mock.MagicMock()
        mock_cat.get_catalog.side_effect = RuntimeError("fetch failure")
        with unittest.mock.patch.object(dash, "_catalog", return_value=mock_cat):
            status, ctype, body = self._get("/api/models/openrouter?refresh=1")
            self.assertEqual(status, 200)
            data = json.loads(body.decode())
            self.assertIn("fetch failure", data["error"])

    def test_events_tap_unavailable(self):
        orig_events = dash.CFG.get("events_path")
        try:
            dash.CFG["events_path"] = None
            status, ctype, body = self._get("/api/events")
            self.assertEqual(status, 503)
            self.assertIn(b"event tap unavailable", body)
        finally:
            dash.CFG["events_path"] = orig_events

    def test_fleet_post_edge_cases(self):
        req = urllib.request.Request(
            f"{self.base_url}/api/fleet",
            data=b"invalid-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
        self.assertEqual(status, 400)

        with unittest.mock.patch.object(dash, "forward_fleet", return_value=(200, {"ok": True})):
            status, ctype, body = self._post("/api/fleet", [1, 2])
            self.assertEqual(status, 200)

    def test_order_apply_splice_and_generic_errors(self):
        status, ctype, body = self._post("/api/routes/order/preview", {"order": "not a dict"})
        self.assertEqual(status, 400)

        with unittest.mock.patch.object(dash, "apply_order", side_effect=dash.SpliceError("conflict")):
            status, ctype, body = self._post("/api/routes/order/apply", {"order": {}})
            self.assertEqual(status, 409)
            self.assertIn(b"conflict", body)

        with unittest.mock.patch.object(dash, "apply_order", side_effect=RuntimeError("unexpected err")):
            status, ctype, body = self._post("/api/routes/order/apply", {"order": {}})
            self.assertEqual(status, 500)
            self.assertIn(b"unexpected err", body)

    def test_events_keepalive_branch(self):
        class MockHandler(dash.Handler):
            def __init__(self):
                self.wfile = io.BytesIO()
                self.wfile.flush = unittest.mock.MagicMock(side_effect=[None, RuntimeError("exit stream")])
            def send_response(self, *a): pass
            def send_header(self, *a): pass
            def end_headers(self): pass

        orig_time = dash.time.time
        calls = [0]
        def fake_time():
            calls[0] += 1
            if calls[0] >= 2:
                return orig_time() + 30
            return orig_time()

        h = MockHandler()
        with unittest.mock.patch.object(dash.time, "time", side_effect=fake_time):
            h._stream_events()
        self.assertIn(b": keepalive\n\n", h.wfile.getvalue())


class SpliceExtraTests(unittest.TestCase):
    def test_topology_and_splice_branches(self):
        topo = dash.parse_topology_text("router_settings:\n  fallbacks: invalid-json\n")
        self.assertEqual(topo["fallbacks"], {})

        with self.assertRaises(dash.SpliceError) as ctx:
            dash.splice_fallbacks("router_settings:\n  fallbacks: [invalid]\n", {})
        self.assertIn("existing fallbacks line is not parseable JSON", str(ctx.exception))

        with self.assertRaises(dash.SpliceError) as ctx:
            dash.splice_fallbacks("router_settings:\n  fallbacks: []\n  context_window_fallbacks: [invalid]\n", {})
        self.assertIn("existing context_window_fallbacks line is not parseable JSON", str(ctx.exception))

        with self.assertRaises(dash.SpliceError) as ctx:
            dash._deploy_blocks(["no anchors here"])
        self.assertIn("no `model_name:` anchors found", str(ctx.exception))

        with self.assertRaises(dash.SpliceError) as ctx:
            dash._deploy_blocks(["  - model_name: a", "    - model_name: b"])
        self.assertIn("deploy anchors at 2 indents", str(ctx.exception))

        text_no_mi = "model_list:\n  - model_name: a\n    litellm_params:\n      model: m1\n  - model_name: b\n    litellm_params:\n      model: m2\n    model_info:\n      id: b1\n"
        with self.assertRaises(dash.SpliceError) as ctx:
            dash.swap_primaries(text_no_mi, "a", "b", "a2", "b2")
        self.assertIn("has no model_info:", str(ctx.exception))

        text_no_id = "model_list:\n  - model_name: a\n    litellm_params:\n      model: m1\n    model_info:\n      public: true\n  - model_name: b\n    litellm_params:\n      model: m2\n    model_info:\n      id: b1\n"
        with self.assertRaises(dash.SpliceError) as ctx:
            dash.swap_primaries(text_no_id, "a", "b", "a2", "b2")
        self.assertIn("has no id: line under model_info:", str(ctx.exception))

        orig_match = dash.re.match
        calls = [0]
        def fake_match(pattern, string, *flags):
            if pattern == r"^\s*id:\s*\S+":
                calls[0] += 1
                if calls[0] >= 3:
                    return None
            return orig_match(pattern, string, *flags)

        text_move_id = "model_list:\n  - model_name: a\n    litellm_params:\n      model: m1\n    model_info:\n      id: a1\n  - model_name: b\n    litellm_params:\n      model: m2\n    model_info:\n      id: b1\n"
        with unittest.mock.patch("re.match", side_effect=fake_match):
            with self.assertRaises(dash.SpliceError) as ctx:
                dash.swap_primaries(text_move_id, "a", "b", "a2", "b2")
            self.assertIn("params swap moved the id: line", str(ctx.exception))

        errs = dash.validate_promote_file({"order": ["heavy", "flash"], "groups": {"heavy": {}, "flash": {}}}, "heavy", "heavy")
        self.assertEqual(errs, ["lane 'heavy' is already its own primary"])

        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(CONFIG)
            p = f.name
        try:
            diff, errs = dash.diff_promote(p, "heavy", "heavy-glm", "fresh-1", "fresh-2")
            self.assertIn("proposed", diff)
            self.assertEqual(errs, [])

            with unittest.mock.patch.object(dash, "swap_primaries", side_effect=dash.SpliceError("simulated splice err")):
                diff_err, errs_splice = dash.diff_promote(p, "heavy", "heavy-glm", "f1", "f2")
                self.assertEqual(diff_err, "")
                self.assertEqual(errs_splice, ["simulated splice err"])

            diff_bad, errs_bad = dash.diff_promote(p, "heavy", "nonexistent", "f1", "f2")
            self.assertEqual(diff_bad, "")
            self.assertTrue(len(errs_bad) > 0)
        finally:
            os.unlink(p)

        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(CONFIG)
            p = f.name
        try:
            with unittest.mock.patch.object(dash, "swap_primaries", side_effect=dash.SpliceError("mock swap err")):
                with self.assertRaises(dash.SpliceError):
                    dash.apply_promote(p, "heavy", "heavy-glm", "a1", "b1")
        finally:
            os.unlink(p)

        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(CONFIG)
            p = f.name
        try:
            diff, errs = dash.diff_chains(p, {"heavy": ["unknown_hop"]})
            self.assertEqual(diff, "")
            self.assertTrue(len(errs) > 0)
        finally:
            os.unlink(p)

        with unittest.mock.patch.object(dash, "http_json", return_value={"ok": False, "status": 503, "json": {"errors": ["proxy down"]}}):
            msg = dash.hotswap_promote("http://base", "key", "heavy", "heavy-glm")
            self.assertIn("live hot-swap unavailable (proxy down)", msg)


class MainEntrypointTest(unittest.TestCase):
    def test_run_module_as_main(self):
        import runpy
        with unittest.mock.patch("sys.argv", ["ferry-dash", "--help"]):
            with io.StringIO() as buf, unittest.mock.patch("sys.stdout", buf):
                with self.assertRaises(SystemExit) as ctx:
                    runpy.run_path(os.path.join(REPO, "ferry-dash"), run_name="__main__")
                self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
