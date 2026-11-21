#!/usr/bin/env python3
"""Stdlib unittest for `ferry kimi` (issue #3): wires Kimi Code CLI
(npm @moonshot-ai/kimi-code, binary `kimi`) through a `kimi-ferry` wrapper and
a ferry-owned KIMI_CODE_HOME, leaving the user's ~/.kimi-code alone.

Run:  python3 lib/ferry-kimi.test.py     (sourcing mode, no build needed)
e2e:  runs the real client when found on PATH or at $FERRY_E2E_BIN_KIMI.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "sk-kimi-test-0123456789"
SYS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
SEED = 'default_model = "mine/x"\n\n[providers.mine]\ntype = "kimi"\napi_key = "mine-key"\n'


def _rb(path):
    with open(path, "rb") as f:
        return f.read()


def _rd(path):
    with open(path) as f:
        return f.read()


def clean_env(home, path=SYS_PATH):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("XDG_") or k.startswith("KIMI_")
                   or k in ("FERRY_FLEET", "CLIENT_MASTER_KEY",
                            "FERRY_MASTER_KEY", "CLIENT_NAME", "FERRY_KEY"))}
    env["HOME"] = home
    env["PATH"] = path
    return env


def run_kimi(home, *args):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-kimi.zsh\n"
              'cmd_kimi "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-kimi", *args],
                       capture_output=True, text=True, timeout=60,
                       env=clean_env(home))
    return p.returncode, p.stdout + p.stderr


class Stub(ThreadingHTTPServer):
    """GET /v1/models lists two lanes; POST /v1/chat/completions answers a
    canned reply (SSE when the client asks to stream). Records what it saw."""

    def __init__(self):
        self.seen = []
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def port(self):
        return self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _rec(self, body=None):
        self.server.seen.append({"method": self.command, "path": self.path,
                                 "headers": {k.lower(): v for k, v in self.headers.items()},
                                 "body": body})

    def do_GET(self):
        self._rec()
        b = json.dumps({"data": [{"id": "heavy"}, {"id": "flash"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        self._rec(body)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "pong-from-stub"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            b = json.dumps({"id": "x", "object": "chat.completion", "model": body.get("model", "heavy"),
                            "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": "pong-from-stub"}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)


def start_stub():
    srv = Stub()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def tree(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(_rb(p)).hexdigest()
    return out


class KimiWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, ".kimi-code"))
        self.user_cfg = os.path.join(self.home, ".kimi-code", "config.toml")
        with open(self.user_cfg, "w") as f:
            f.write(SEED)
        self.stub = start_stub()
        self.addCleanup(self.stub.shutdown)
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = os.path.join(self.home, ".config", "ferry", "kimi", "home", "config.toml")
        self.wrap = os.path.join(self.home, ".local", "bin", "kimi-ferry")

    def wire(self, *extra, port=None):
        rc, out = run_kimi(self.home, "--host", "127.0.0.1",
                           "--port", str(port or self.stub.port), "--key", KEY, *extra)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_names_the_command_and_the_limits(self):
        rc, out = run_kimi(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry kimi", out)
        self.assertIn("kimi-ferry", out)
        self.assertIn("MCP servers", out)
        self.assertNotIn("—", out)

    def test_wire_writes_owned_home_wrapper_and_leaves_user_config_alone(self):
        before = tree(os.path.join(self.home, ".kimi-code"))
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        self.assertEqual(tree(os.path.join(self.home, ".kimi-code")), before)
        self.assertEqual(_rd(self.user_cfg), SEED, "user config changed")
        cfg = tomllib.loads(_rd(self.cfg))
        self.assertEqual(cfg["default_model"], "ferry/heavy")
        self.assertFalse(cfg["telemetry"])
        prov = cfg["providers"]["ferry"]
        self.assertEqual(prov["type"], "openai")
        self.assertEqual(prov["base_url"], f"http://127.0.0.1:{self.stub.port}/v1")
        self.assertEqual(prov["api_key_env"], "FERRY_KIMI_API_KEY")
        self.assertNotIn("api_key", prov)
        self.assertEqual(prov["custom_headers"]["X-Ferry-Client"], "host")
        self.assertEqual(sorted(cfg["models"]), ["ferry/flash", "ferry/heavy"])
        for alias, m in cfg["models"].items():
            self.assertEqual(m["provider"], "ferry")
            self.assertEqual("ferry/" + m["model"], alias)
        self.assertIn("ferry-managed: kimi", _rd(self.cfg))
        body = _rd(self.wrap)
        self.assertIn("ferry-managed: kimi", body)
        self.assertIn("KIMI_CODE_HOME=" + os.path.dirname(self.cfg), body)
        self.assertIn('FERRY_KIMI_API_KEY="$FERRY_KEY"', body)
        self.assertIn("unset KIMI_MODEL_NAME", body)
        self.assertTrue(os.access(self.wrap, os.X_OK))
        self.assertTrue(body.rstrip().splitlines()[-1].startswith("exec kimi "))

    def test_key_only_in_a_0600_file(self):
        self.wire()
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(_rd(keyfile), KEY)
        for d, _, files in os.walk(self.home):
            for f in files:
                p = os.path.join(d, f)
                if KEY.encode() in _rb(p):
                    self.assertEqual(os.stat(p).st_mode & 0o777, 0o600, f"{p} holds the key")

    def test_fleet_header_when_set(self):
        script = (f"source {REPO}/lib/ferry-core.zsh\nsource {REPO}/lib/ferry-agents.zsh\n"
                  f"source {REPO}/lib/ferry-kimi.zsh\nexport FERRY_FLEET=crew1\ncmd_kimi \"$@\"\n")
        p = subprocess.run(["zsh", "-c", script, "x", "--host", "127.0.0.1", "--port",
                            str(self.stub.port), "--key", KEY],
                           capture_output=True, text=True, timeout=60, env=clean_env(self.home))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        hdr = tomllib.loads(_rd(self.cfg))["providers"]["ferry"]["custom_headers"]
        self.assertEqual(hdr["X-Ferry-Fleet"], "crew1")

    def test_lane_check_falls_back_to_a_served_lane(self):
        out = self.wire("--model", "no-such-lane")
        self.assertIn("does not serve lane 'no-such-lane'", out)
        self.assertEqual(tomllib.loads(_rd(self.cfg))["default_model"], "ferry/heavy")
        self.assertTrue(any(s["path"] == "/v1/models" for s in self.stub.seen))

    def test_explicit_served_lane_is_the_default(self):
        self.wire("--model", "flash")
        self.assertEqual(tomllib.loads(_rd(self.cfg))["default_model"], "ferry/flash")

    def test_unreachable_host_still_wires_with_a_warning(self):
        out = self.wire(port=9)
        self.assertIn("could not verify the lane catalogue", out)
        cfg = tomllib.loads(_rd(self.cfg))
        self.assertEqual(list(cfg["models"]), ["ferry/heavy"])

    def test_rewire_is_idempotent(self):
        self.wire()
        first = (_rd(self.cfg), _rd(self.wrap))
        self.wire()
        self.assertEqual((_rd(self.cfg), _rd(self.wrap)), first)
        rec = json.loads(_rd(os.path.join(self.home, ".config", "ferry", "kimi.json")))
        self.assertEqual(sorted(c["path"] for c in rec["created"]), sorted([self.cfg, self.wrap]))
        self.assertEqual(rec["edited"], [])

    def test_unwire_removes_ferry_files_and_keeps_user_config(self):
        self.wire()
        rc, out = run_kimi(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.cfg))
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "kimi.json")))
        self.assertEqual(_rd(self.user_cfg), SEED)

    def test_unwire_keeps_a_config_the_client_rewrote(self):
        self.wire()
        with open(self.cfg, "a") as f:
            f.write("# edited later\n")
        rc, out = run_kimi(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.cfg))
        self.assertIn("changed since ferry wrote it", out)

    def test_refuses_a_foreign_wrapper(self):
        os.makedirs(os.path.dirname(self.wrap))
        with open(self.wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_kimi(self.home, "--host", "127.0.0.1", "--port", "9", "--key", KEY)
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.wrap), "#!/bin/sh\necho mine\n")


def find_kimi():
    cand = os.environ.get("FERRY_E2E_BIN_KIMI") or shutil.which("kimi")
    return cand if cand and os.path.exists(cand) else None


class KimiE2E(unittest.TestCase):
    def test_kimi_ferry_talks_to_the_stub(self):
        binp = find_kimi()
        if not binp:
            self.skipTest("kimi binary not found (set FERRY_E2E_BIN_KIMI or put kimi on PATH)")
        bindir = os.path.dirname(binp)
        node = shutil.which("node")
        if not node:
            self.skipTest("node not found; Kimi Code CLI needs it for the npm install")
        path = os.pathsep.join([bindir, os.path.dirname(node), SYS_PATH])
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
            probe = subprocess.run([binp, "--version"], capture_output=True, text=True,
                                   timeout=60, env=clean_env(home, path), cwd=work)
            if "no longer maintained" in probe.stdout + probe.stderr:
                self.skipTest("found the archived Python kimi-cli, not Kimi Code")
            if probe.returncode != 0:
                self.skipTest(f"kimi --version failed: {probe.stdout}{probe.stderr}"[:200])
            stub = start_stub()
            self.addCleanup(stub.server_close)
            self.addCleanup(stub.shutdown)
            os.makedirs(os.path.join(home, ".kimi-code"))
            user_cfg = os.path.join(home, ".kimi-code", "config.toml")
            with open(user_cfg, "w") as f:
                f.write(SEED)
            env = clean_env(home, path)
            script = (f"source {REPO}/lib/ferry-core.zsh\nsource {REPO}/lib/ferry-agents.zsh\n"
                      f"source {REPO}/lib/ferry-kimi.zsh\ncmd_kimi \"$@\"\n")
            w = subprocess.run(["zsh", "-c", script, "x", "--host", "127.0.0.1", "--port",
                                str(stub.port), "--key", KEY],
                               capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(w.returncode, 0, w.stdout + w.stderr)
            stub.seen.clear()
            # (the wiring step's own system python may drop a Library/Caches pyc
            # cache in HOME; only what the client adds on top is checked)
            home_before = set(os.listdir(home))
            # Hostile env: these would route around ferry if the wrapper kept them.
            env.update(KIMI_MODEL_NAME="evil-model", KIMI_MODEL_API_KEY="evil-key",
                       KIMI_MODEL_BASE_URL="http://127.0.0.1:9/v1",
                       KIMI_MODEL_PROVIDER_TYPE="openai")
            wrapper = os.path.join(home, ".local", "bin", "kimi-ferry")
            r = subprocess.run([wrapper, "-p", "say pong"], capture_output=True, text=True,
                               timeout=180, env=env, cwd=work)
            out = r.stdout + r.stderr
            posts = [s for s in stub.seen if s["method"] == "POST"]
            self.assertTrue(posts, f"stub saw no POST (rc={r.returncode}): {out[:500]}")
            req = posts[0]
            self.assertEqual(req["path"], "/v1/chat/completions")
            self.assertEqual(req["headers"].get("authorization"), f"Bearer {KEY}")
            self.assertEqual(req["headers"].get("x-ferry-client"), "host")
            self.assertEqual(req["body"].get("model"), "heavy")
            self.assertTrue(req["body"].get("stream"))
            self.assertIn("pong-from-stub", out)
            self.assertNotIn(KEY, out, "key printed")
            # The user's own profile is byte-identical and nothing else landed in HOME.
            self.assertEqual(_rd(user_cfg), SEED)
            self.assertEqual(os.listdir(os.path.join(home, ".kimi-code")), ["config.toml"])
            extra = set(os.listdir(home)) - home_before
            self.assertEqual(extra, set(), f"unexpected entries in HOME: {extra}")
            # Unwire still removes ferry's own files after a real run.
            rc, uo = run_kimi(home, "--unwire")
            self.assertEqual(rc, 0, uo)
            self.assertFalse(os.path.exists(wrapper))


if __name__ == "__main__":
    unittest.main()
