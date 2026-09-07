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

    def test_find_log_returns_none_or_path(self):
        res = dash.find_log(99999)
        self.assertTrue(res is None or isinstance(res, str))

    def test_http_json_network_error(self):
        res = dash.http_json("http://127.0.0.1:1/none", "key", timeout=0.5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 0)


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
        # Verify handle_one_request catches ConnectionResetError without raising
        class DummyHandler(dash.Handler):
            def __init__(self):
                self.close_connection = False

            def handle_one_request(self):
                try:
                    raise ConnectionResetError("client reset")
                except (ConnectionResetError, BrokenPipeError):
                    self.close_connection = True

        h = DummyHandler()
        h.handle_one_request()
        self.assertTrue(h.close_connection)


if __name__ == "__main__":
    unittest.main()
