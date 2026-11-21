#!/usr/bin/env python3
"""Stdlib unittest for `ferry qwen` (issue #3): the qwen-ferry wrapper.

Run:  python3 lib/ferry-qwen.test.py

Sourcing mode: the built `ferry` monolith does not know the command until the
orchestrator registers the module, so every run sources the lib files.
HOME is always a throwaway directory; nothing here touches the real ~/.qwen
and nothing points at the live gateway (stubs bind ephemeral loopback ports).
The e2e runs the real `qwen` binary (PATH, or FERRY_E2E_BIN_QWEN) against
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
KEY = "sk-qwen-test-secret-123"
SEED = '{\n  "ui": {"theme": "Qwen Dark"},\n  "security": {"auth": {"selectedType": "qwen-oauth"}}\n}\n'
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
SCRIPT = ("source lib/ferry-core.zsh; source lib/ferry-agents.zsh; "
          "source lib/ferry-qwen.zsh; cmd_qwen \"$@\"")


def _rd(path):
    with open(path) as f:
        return f.read()


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("XDG_", "QWEN_", "OPENAI_"))
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
    p = subprocess.run(["zsh", "-c", SCRIPT, "ferry-qwen", *args], cwd=REPO,
                       capture_output=True, text=True, timeout=90, env=env)
    return p.returncode, p.stdout + p.stderr


class Stub:
    """Loopback server: /v1/models catalogue + canned OpenAI chat/completions replies."""

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
                raw = self.rfile.read(n).decode()
                outer.seen.append((self.command, self.path, dict(self.headers)))
                reply = {"id": "c1", "object": "chat.completion", "model": "heavy",
                         "choices": [{"index": 0, "finish_reason": "stop", "message": {
                             "role": "assistant", "content": "FERRY-STUB-OK"}}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                   "total_tokens": 2}}
                if json.loads(raw or "{}").get("stream"):
                    chunk = {"id": "c1", "object": "chat.completion.chunk", "model": "heavy",
                             "choices": [{"index": 0, "finish_reason": None, "delta": {
                                 "role": "assistant", "content": "FERRY-STUB-OK"}}]}
                    done = dict(chunk, choices=[{"index": 0, "delta": {},
                                                 "finish_reason": "stop"}],
                                usage=reply["usage"])
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for c in (chunk, done):
                        self.wfile.write(("data: %s\n\n" % json.dumps(c)).encode())
                    self.wfile.write(b"data: [DONE]\n\n")
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


class QwenWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.user_cfg = os.path.join(self.home, ".qwen", "settings.json")
        os.makedirs(os.path.dirname(self.user_cfg))
        with open(self.user_cfg, "w") as f:
            f.write(SEED)
        self.stub = Stub()
        self.addCleanup(self.stub.close)
        self.addCleanup(self.tmp.cleanup)
        self.wrap = os.path.join(self.home, ".local", "bin", "qwen-ferry")
        self.settings = os.path.join(self.home, ".config", "ferry", "qwen-settings.json")
        self.record = os.path.join(self.home, ".config", "ferry", "qwen.json")
        self.base = f"http://127.0.0.1:{self.stub.port}/v1"

    def wire(self, *extra, key=KEY, env_extra=None):
        rc, out = run_wire(self.home, "--host", "127.0.0.1", "--port", str(self.stub.port),
                           "--key", key, *extra, env_extra=env_extra)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_mentions_ferry_qwen(self):
        rc, out = run_wire(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry qwen", out)

    def test_wire_writes_wrapper_settings_record_and_leaves_user_config(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        body = _rd(self.wrap)
        self.assertIn("ferry-managed: qwen", body)
        self.assertIn("OPENAI_API_KEY=\"$FERRY_KEY\"", body)
        self.assertIn(f"OPENAI_BASE_URL={self.base}", body)
        self.assertIn("OPENAI_MODEL=heavy", body)
        self.assertIn(f"QWEN_CODE_SYSTEM_SETTINGS_PATH={self.settings}", body)
        self.assertTrue(os.access(self.wrap, os.X_OK))
        doc = json.loads(_rd(self.settings))
        self.assertEqual(doc["security"]["auth"]["selectedType"], "openai")
        self.assertEqual(doc["model"]["name"], "heavy")
        entries = doc["modelProviders"]["openai"]
        self.assertEqual([e["id"] for e in entries], ["heavy", "flash"])
        for e in entries:
            self.assertEqual(e["baseUrl"], self.base)
            self.assertEqual(e["envKey"], "OPENAI_API_KEY")
            self.assertEqual(e["generationConfig"]["customHeaders"]["X-Ferry-Client"], "host")
        self.assertNotIn(KEY, _rd(self.settings))
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
        hdrs = json.loads(_rd(self.settings))["modelProviders"]["openai"][0][
            "generationConfig"]["customHeaders"]
        self.assertEqual(hdrs, {"X-Ferry-Client": "lap", "X-Ferry-Fleet": "fleet-7"})

    def test_lane_check_falls_back_to_a_served_lane(self):
        out = self.wire("--model", "zzz-not-a-lane")
        self.assertIn("does not serve lane 'zzz-not-a-lane'", out)
        self.assertEqual(json.loads(_rd(self.settings))["model"]["name"], "heavy")
        self.assertIn("OPENAI_MODEL=heavy", _rd(self.wrap))
        self.assertTrue(self.stub.headers_of("GET", "/v1/models"), "no /v1/models probe")

    def test_a_served_lane_is_kept(self):
        self.wire("--model", "flash")
        self.assertEqual(json.loads(_rd(self.settings))["model"]["name"], "flash")

    def test_device_key_is_fine_on_chat_completions(self):
        out = self.wire(key="fk-devicekey-abc")
        self.assertNotIn("WARNING", out)
        self.assertNotIn("fk-devicekey-abc", out)

    def test_wrapper_exports_and_execs_the_client(self):
        self.wire()
        fake = os.path.join(self.home, "fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "qwen"), "w") as f:
            f.write('#!/bin/sh\necho "KEY=$OPENAI_API_KEY BASE=$OPENAI_BASE_URL '
                    'MODEL=$OPENAI_MODEL SYS=$QWEN_CODE_SYSTEM_SETTINGS_PATH ARGS=$*"\n')
        os.chmod(os.path.join(fake, "qwen"), 0o755)
        p = subprocess.run([self.wrap, "-p", "hi"], capture_output=True, text=True,
                           timeout=30, env=clean_env(self.home, PATH=f"{fake}:{SAFE_PATH}"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"KEY={KEY} BASE={self.base} MODEL=heavy", p.stdout)
        self.assertIn(f"SYS={self.settings}", p.stdout)
        self.assertIn("ARGS=-p hi", p.stdout)

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


def _find_qwen():
    explicit = os.environ.get("FERRY_E2E_BIN_QWEN")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("qwen")


class QwenEndToEnd(unittest.TestCase):
    def test_real_qwen_through_qwen_ferry_hits_the_stub(self):
        binary = _find_qwen()
        if not binary:
            self.skipTest("qwen binary not found (PATH or FERRY_E2E_BIN_QWEN)")
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            stub = Stub()
            self.addCleanup(stub.close)
            seed_client(home, "e2e-host")
            rc, out = run_wire(home, "--host", "127.0.0.1", "--port", str(stub.port),
                               "--key", KEY)
            self.assertEqual(rc, 0, out)
            path = os.path.dirname(os.path.abspath(binary)) + os.pathsep + os.environ["PATH"]
            p = subprocess.run([os.path.join(home, ".local", "bin", "qwen-ferry"),
                                "-p", "say hi"], cwd=cwd, capture_output=True, text=True,
                               timeout=180, env=clean_env(home, PATH=path))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("FERRY-STUB-OK", p.stdout)
            gen = [(m, pth, h) for m, pth, h in stub.seen if pth.endswith("/chat/completions")]
            self.assertTrue(gen, f"stub saw no chat call: {[s[:2] for s in stub.seen]}")
            m, pth, h = gen[0]
            h = {k.lower(): v for k, v in h.items()}
            self.assertEqual((m, pth), ("POST", "/v1/chat/completions"))
            self.assertEqual(h.get("authorization"), f"Bearer {KEY}")
            self.assertEqual(h.get("x-ferry-client"), "e2e-host")
            user = os.path.join(home, ".qwen", "settings.json")
            if os.path.exists(user):
                self.assertNotIn("ferry", _rd(user).lower())


if __name__ == "__main__":
    unittest.main()
