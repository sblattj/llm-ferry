#!/usr/bin/env python3
"""Stdlib unittest for `ferry gemini` (issue #3): the gemini-ferry wrapper.

Run:  python3 lib/ferry-gemini.test.py

Sourcing mode: the built `ferry` monolith does not know the command until the
orchestrator registers the module, so every run sources the lib files.
HOME is always a throwaway directory; nothing here touches the real ~/.gemini
and nothing points at the live gateway (stubs bind ephemeral loopback ports).
The e2e runs the real `gemini` binary (PATH, or FERRY_E2E_BIN_GEMINI) against
a stub; no token is spent.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "sk-gemini-test-secret-123"
SEED = '{\n  "theme": "Dracula",\n  "security": {"auth": {"selectedType": "oauth-personal"}}\n}\n'
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
SCRIPT = ("source lib/ferry-core.zsh; source lib/ferry-agents.zsh; "
          "source lib/ferry-gemini.zsh; cmd_gemini \"$@\"")


def _rd(path):
    with open(path) as f:
        return f.read()


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("XDG_", "GEMINI_", "GOOGLE_"))
                   or k in ("FERRY_FLEET", "CLIENT_MASTER_KEY",
                            "FERRY_MASTER_KEY", "CLIENT_NAME"))}
    env["HOME"] = home
    env.update(extra)
    return env


def seed_client(home, name):
    """CLIENT_NAME comes from ~/.config/ferry/client.json (ferry-core.zsh:296)."""
    d = os.path.join(home, ".config", "ferry")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "client.json"), "w") as f:
        json.dump({"name": name}, f)


def run_wire(home, *args, env_extra=None):
    env = clean_env(home, PATH=SAFE_PATH, **(env_extra or {}))
    p = subprocess.run(["zsh", "-c", SCRIPT, "ferry-gemini", *args], cwd=REPO,
                       capture_output=True, text=True, timeout=90, env=env)
    return p.returncode, p.stdout + p.stderr


class Stub:
    """Loopback server: /v1/models catalogue + canned Gemini / OpenAI replies."""

    def __init__(self, lanes=("heavy", "flash")):
        self.lanes, self.seen = list(lanes), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                outer.seen.append((self.command, self.path, dict(self.headers)))
                self._json({"data": [{"id": l} for l in outer.lanes]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                outer.seen.append((self.command, self.path, dict(self.headers)))
                reply = {"candidates": [{"content": {"role": "model", "parts": [
                    {"text": "FERRY-STUB-OK"}]}, "finishReason": "STOP", "index": 0}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1,
                                      "totalTokenCount": 2}}
                if "streamGenerateContent" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(("data: %s\r\n\r\n" % json.dumps(reply)).encode())
                else:
                    self._json(reply)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def headers_of(self, method, path_prefix):
        return [{k.lower(): v for k, v in h.items()}
                for m, p, h in self.seen if m == method and p.startswith(path_prefix)]


class GeminiWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.user_cfg = os.path.join(self.home, ".gemini", "settings.json")
        os.makedirs(os.path.dirname(self.user_cfg))
        with open(self.user_cfg, "w") as f:
            f.write(SEED)
        self.stub = Stub()
        self.addCleanup(self.stub.close)
        self.addCleanup(self.tmp.cleanup)
        self.wrap = os.path.join(self.home, ".local", "bin", "gemini-ferry")
        self.settings = os.path.join(self.home, ".config", "ferry", "gemini-settings.json")
        self.record = os.path.join(self.home, ".config", "ferry", "gemini.json")

    def wire(self, *extra, key=KEY, env_extra=None):
        rc, out = run_wire(self.home, "--host", "127.0.0.1", "--port", str(self.stub.port),
                           "--key", key, *extra, env_extra=env_extra)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_mentions_ferry_gemini(self):
        rc, out = run_wire(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry gemini", out)
        self.assertIn("device key", out)

    def test_wire_writes_wrapper_settings_record_and_leaves_user_config(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        body = _rd(self.wrap)
        self.assertIn("ferry-managed: gemini", body)
        self.assertIn(f"GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:{self.stub.port}", body)
        self.assertIn("GEMINI_MODEL=heavy", body)
        self.assertIn("GEMINI_API_KEY=\"$FERRY_KEY\"", body)
        self.assertIn(f"GEMINI_CLI_SYSTEM_SETTINGS_PATH={self.settings}", body)
        self.assertIn("X-Ferry-Client: host", body)
        self.assertTrue(os.access(self.wrap, os.X_OK))
        sysd = json.loads(_rd(self.settings))
        self.assertEqual(sysd["security"]["auth"]["selectedType"], "gemini-api-key")
        rec = json.loads(_rd(self.record))
        self.assertEqual(sorted(c["path"] for c in rec["created"]),
                         sorted([self.wrap, self.settings]))
        self.assertEqual(_rd(self.user_cfg), SEED, "the user's settings.json changed")
        self.assertEqual(os.listdir(os.path.dirname(self.user_cfg)), ["settings.json"])

    def test_key_only_in_a_0600_file(self):
        self.wire()
        hits = []
        for root, _, files in os.walk(self.home):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as fh:
                    found = KEY.encode() in fh.read()
                if found:
                    hits.append((path, stat.S_IMODE(os.stat(path).st_mode)))
        self.assertEqual(hits, [(os.path.join(self.home, ".config", "ferry", "agents.key"),
                                 0o600)])

    def test_headers_carry_client_name_and_fleet(self):
        seed_client(self.home, "lap")
        self.wire(env_extra={"FERRY_FLEET": "fleet-7"})
        body = _rd(self.wrap)
        self.assertIn("X-Ferry-Client: lap", body)
        self.assertIn("X-Ferry-Fleet: fleet-7", body)

    def test_lane_check_falls_back_to_a_served_lane(self):
        out = self.wire("--model", "zzz-not-a-lane")
        self.assertIn("does not serve lane 'zzz-not-a-lane'", out)
        self.assertIn("GEMINI_MODEL=heavy", _rd(self.wrap))
        self.assertTrue(self.stub.headers_of("GET", "/v1/models"), "no /v1/models probe")

    def test_a_served_lane_is_kept(self):
        self.wire("--model", "flash")
        self.assertIn("GEMINI_MODEL=flash", _rd(self.wrap))

    def test_device_key_notes_the_host_version_and_wires(self):
        out = self.wire(key="fk-devicekey-abc")
        self.assertNotIn("WARNING", out)
        self.assertIn("device key", out)
        self.assertIn("v1.52.0", out)
        self.assertIn("/v1beta", out)
        self.assertTrue(os.path.exists(self.wrap))
        self.assertNotIn("fk-devicekey-abc", out)

    def test_master_key_does_not_warn(self):
        self.assertNotIn("device key", self.wire())

    def test_wrapper_exports_and_stays_quiet_for_a_device_key_at_run_time(self):
        self.wire(key="fk-devicekey-abc")
        fake = os.path.join(self.home, "fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "gemini"), "w") as f:
            f.write('#!/bin/sh\necho "BASE=$GOOGLE_GEMINI_BASE_URL MODEL=$GEMINI_MODEL '
                    'HDR=$GEMINI_CLI_CUSTOM_HEADERS SYS=$GEMINI_CLI_SYSTEM_SETTINGS_PATH ARGS=$*"\n')
        os.chmod(os.path.join(fake, "gemini"), 0o755)
        env = clean_env(self.home, PATH=f"{fake}:{SAFE_PATH}",
                        GEMINI_CLI_CUSTOM_HEADERS="X-Mine: 1")
        p = subprocess.run([self.wrap, "-p", "hi"], capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"BASE=http://127.0.0.1:{self.stub.port}", p.stdout)
        self.assertIn("MODEL=heavy", p.stdout)
        self.assertIn("HDR=X-Mine: 1,X-Ferry-Client: host", p.stdout)
        self.assertIn(f"SYS={self.settings}", p.stdout)
        self.assertIn("ARGS=-p hi", p.stdout)
        self.assertNotIn("WARNING", p.stderr)

    def test_rewire_is_idempotent(self):
        self.wire()
        first = (_rd(self.wrap), _rd(self.settings))
        self.wire()
        self.assertEqual((_rd(self.wrap), _rd(self.settings)), first)
        self.assertEqual(_rd(self.user_cfg), SEED)

    def test_unwire_restores_user_config_and_removes_ferry_files(self):
        self.wire()
        self.wire()
        rc, out = run_wire(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_rd(self.user_cfg), SEED)
        for p in (self.wrap, self.settings, self.record):
            self.assertFalse(os.path.exists(p), f"{p} survived --unwire")

    def test_refuses_to_replace_a_user_file_at_the_wrapper_path(self):
        os.makedirs(os.path.dirname(self.wrap))
        with open(self.wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_wire(self.home, "--host", "127.0.0.1", "--port", str(self.stub.port))
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.wrap), "#!/bin/sh\necho mine\n")


def _find_gemini():
    explicit = os.environ.get("FERRY_E2E_BIN_GEMINI")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("gemini")


class GeminiEndToEnd(unittest.TestCase):
    def test_real_gemini_through_gemini_ferry_hits_the_stub(self):
        binary = _find_gemini()
        if not binary:
            self.skipTest("gemini binary not found (PATH or FERRY_E2E_BIN_GEMINI)")
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            stub = Stub()
            self.addCleanup(stub.close)
            seed_client(home, "e2e-host")
            rc, out = run_wire(home, "--host", "127.0.0.1", "--port", str(stub.port),
                               "--key", KEY)
            self.assertEqual(rc, 0, out)
            path = os.path.dirname(os.path.abspath(binary)) + os.pathsep + os.environ["PATH"]
            env = clean_env(home, PATH=path, GEMINI_CLI_TRUST_WORKSPACE="true")
            p = subprocess.run([os.path.join(home, ".local", "bin", "gemini-ferry"),
                                "-p", "say hi"], cwd=cwd, capture_output=True, text=True,
                               timeout=180, env=env)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("FERRY-STUB-OK", p.stdout)
            gen = [(m, pth, h) for m, pth, h in stub.seen if "generatecontent" in pth.lower()]
            self.assertTrue(gen, f"stub saw no generate call: {[s[:2] for s in stub.seen]}")
            m, pth, h = gen[0]
            h = {k.lower(): v for k, v in h.items()}
            self.assertEqual(m, "POST")
            self.assertTrue(pth.startswith("/v1beta/models/heavy:"), pth)
            self.assertEqual(h.get("x-goog-api-key"), KEY)
            self.assertEqual(h.get("x-ferry-client"), "e2e-host")
            self.assertFalse(os.path.exists(os.path.join(home, ".gemini", "settings.json"))
                             and "ferry" in _rd(os.path.join(home, ".gemini", "settings.json")))


if __name__ == "__main__":
    unittest.main()
