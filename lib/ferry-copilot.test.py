#!/usr/bin/env python3
"""Stdlib unittest for `ferry copilot` (issue #3): GitHub Copilot CLI through
its BYOK env vars, via a `copilot-ferry` wrapper.

Run:  python3 lib/ferry-copilot.test.py     (sourcing mode, no build needed)

Failure case from the issue: `ferry copilot` is not a command on v1.51.0.
The e2e needs the real `copilot` binary (PATH, or FERRY_E2E_BIN_COPILOT) and
runs it with HOME and COPILOT_HOME in a tempdir against a local stub: no
GitHub login, no tokens, never port 8090.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "sk-test-copilot-secret"
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
STRIP = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
         "FERRY_FLEET", "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY", "CLIENT_NAME",
         "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_HOME")


def _rd(p):
    with open(p) as f:
        return f.read()


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in STRIP and not k.startswith("COPILOT_")}
    env.update(HOME=home, **extra)
    return env


def run_copilot(home, *args, **extra):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-copilot.zsh\n"
              'cmd_copilot "$@"\n')
    env = clean_env(home, PATH=SAFE_PATH, **extra)
    p = subprocess.run(["zsh", "-c", script, "ferry-copilot", *args],
                       capture_output=True, text=True, timeout=60, env=env)
    return p.returncode, p.stdout + p.stderr


class Stub:
    """Serves GET /v1/models and a canned OpenAI chat reply, records requests."""

    def __init__(self, lanes=("heavy", "flash")):
        self.lanes, self.seen = list(lanes), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.seen.append(("GET", self.path, dict(self.headers), b""))
                self._send(200, json.dumps(
                    {"data": [{"id": l} for l in outer.lanes]}).encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.seen.append(("POST", self.path, dict(self.headers), body))
                try:
                    stream = bool(json.loads(body).get("stream"))
                except Exception:
                    stream = False
                if stream:
                    def ch(d, f=None):
                        return "data: " + json.dumps({
                            "id": "c1", "object": "chat.completion.chunk",
                            "created": 1, "model": "heavy",
                            "choices": [{"index": 0, "delta": d,
                                         "finish_reason": f}]}) + "\n\n"
                    out = (ch({"role": "assistant", "content": ""})
                           + ch({"content": "FERRY-STUB-OK"}) + ch({}, "stop")
                           + "data: [DONE]\n\n").encode()
                    self._send(200, out, "text/event-stream")
                else:
                    self._send(200, json.dumps({
                        "id": "c1", "object": "chat.completion", "created": 1,
                        "model": "heavy",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant",
                                                 "content": "FERRY-STUB-OK"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}).encode())

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class CopilotWire(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.addCleanup(self.stub.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.cdir = os.path.join(self.home, ".copilot")
        os.makedirs(self.cdir)
        self.user_cfg = os.path.join(self.cdir, "config.json")
        self.user_bytes = '{"model":"mine","trusted_folders":["/x"]}\n'
        with open(self.user_cfg, "w") as f:
            f.write(self.user_bytes)
        self.wrap = os.path.join(self.home, ".local", "bin", "copilot-ferry")
        self.keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")

    def tearDown(self):
        self.tmp.cleanup()

    def wire(self, *extra, **env):
        rc, out = run_copilot(self.home, "--host", "127.0.0.1",
                              "--port", str(self.stub.port),
                              "--key", KEY, *extra, **env)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_names_the_command_and_exits_zero(self):
        rc, out = run_copilot(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry copilot", out)
        self.assertIn("BYOK", out)

    def test_wire_writes_wrapper_and_record_and_leaves_user_config(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        body = _rd(self.wrap)
        self.assertIn("ferry-managed: copilot", body)
        self.assertIn("COPILOT_PROVIDER_BASE_URL=http://127.0.0.1:%d/v1" % self.stub.port, body)
        self.assertIn("COPILOT_PROVIDER_TYPE=openai", body)
        self.assertIn("COPILOT_PROVIDER_WIRE_API=completions", body)
        self.assertIn("COPILOT_MODEL=heavy", body)
        self.assertIn('COPILOT_PROVIDER_API_KEY="$FERRY_KEY"', body)
        self.assertIn("X-Ferry-Client: host", body)
        self.assertIn("exec copilot", body)
        self.assertNotIn(KEY, body, "key baked into wrapper")
        self.assertTrue(os.access(self.wrap, os.X_OK))
        self.assertEqual(_rd(self.user_cfg), self.user_bytes)
        self.assertEqual(_rd(self.keyfile), KEY)
        self.assertEqual(os.stat(self.keyfile).st_mode & 0o777, 0o600)
        rec = json.loads(_rd(os.path.join(self.home, ".config", "ferry", "copilot.json")))
        self.assertEqual([c["path"] for c in rec["created"]], [self.wrap])
        self.assertEqual(rec["edited"], [])
        # the key is in no file except the 0600 key file
        for root, _, files in os.walk(self.home):
            for fn in files:
                p = os.path.join(root, fn)
                with open(p, "rb") as fh:
                    leaked = KEY.encode() in fh.read()
                if leaked:
                    self.assertEqual(p, self.keyfile)
                    self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_fleet_header_is_added_when_set(self):
        self.wire(FERRY_FLEET="lab-7")
        body = _rd(self.wrap)
        self.assertIn("X-Ferry-Fleet: lab-7", body)
        self.assertIn("X-Ferry-Client: host", body)

    def test_lane_check_falls_back_to_a_served_lane(self):
        stub = Stub(lanes=("flash", "medium"))
        try:
            rc, out = run_copilot(self.home, "--host", "127.0.0.1",
                                  "--port", str(stub.port), "--key", KEY,
                                  "--model", "nope")
        finally:
            stub.close()
        self.assertEqual(rc, 0, out)
        self.assertIn("does not serve lane 'nope'", out)
        self.assertIn("COPILOT_MODEL=flash", _rd(self.wrap))
        gets = [s for s in stub.seen if s[0] == "GET"]
        self.assertTrue(gets and gets[0][1] == "/v1/models")
        self.assertEqual(gets[0][2].get("Authorization"), f"Bearer {KEY}")

    def test_rewire_is_idempotent_and_unwire_restores(self):
        self.wire()
        first = _rd(self.wrap)
        self.wire()
        self.assertEqual(_rd(self.wrap), first)
        rc, out = run_copilot(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(
            os.path.join(self.home, ".config", "ferry", "copilot.json")))
        self.assertEqual(_rd(self.user_cfg), self.user_bytes)

    def test_unreachable_ferry_still_wires_the_default_lane(self):
        # Port 9 (discard) is never a ferry: the lane check must not be fatal
        # and the wired lane must stay the full word 'heavy'.
        rc, out = run_copilot(self.home, "--host", "127.0.0.1", "--port", "9",
                              "--key", KEY)
        self.assertEqual(rc, 0, out)
        self.assertIn("COPILOT_MODEL=heavy\n", _rd(self.wrap))

    def test_refuses_to_replace_a_user_wrapper(self):
        os.makedirs(os.path.dirname(self.wrap))
        with open(self.wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_copilot(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.wrap), "#!/bin/sh\necho mine\n")

    def test_wrapper_drops_inherited_competing_credentials(self):
        # A fake `copilot` that dumps its env: the wrapper must win over a
        # bearer token / key command / providers file the user exported.
        self.wire()
        fakebin = os.path.join(self.home, "fakebin")
        os.makedirs(fakebin)
        fake = os.path.join(fakebin, "copilot")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\nenv | grep ^COPILOT_ | sort\necho "ARGS: $*"\n')
        os.chmod(fake, 0o755)
        env = clean_env(self.home, PATH=f"{fakebin}:{SAFE_PATH}",
                        COPILOT_PROVIDER_BEARER_TOKEN="user-bearer",
                        COPILOT_PROVIDER_API_KEY_COMMAND="echo user-key",
                        COPILOT_MODEL="gpt-user")
        p = subprocess.run([self.wrap, "--model", "flash"], capture_output=True,
                           text=True, timeout=30, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("user-bearer", p.stdout)
        self.assertNotIn("API_KEY_COMMAND", p.stdout)
        self.assertIn(f"COPILOT_PROVIDER_API_KEY={KEY}", p.stdout)
        self.assertIn("COPILOT_PROVIDER_BASE_URL=http://127.0.0.1:%d/v1" % self.stub.port, p.stdout)
        self.assertIn("COPILOT_MODEL=heavy", p.stdout)
        self.assertIn("ARGS: --model flash", p.stdout)


def find_copilot():
    return os.environ.get("FERRY_E2E_BIN_COPILOT") or shutil.which("copilot")


class CopilotE2E(unittest.TestCase):
    def test_real_copilot_talks_to_the_stub_through_the_wrapper(self):
        binp = find_copilot()
        if not binp or not os.path.exists(binp):
            self.skipTest("copilot binary not found (PATH or FERRY_E2E_BIN_COPILOT)")
        stub = Stub()
        with tempfile.TemporaryDirectory() as home:
            try:
                rc, out = run_copilot(home, "--host", "127.0.0.1",
                                      "--port", str(stub.port), "--key", KEY)
                self.assertEqual(rc, 0, out)
                wrap = os.path.join(home, ".local", "bin", "copilot-ferry")
                stub.seen.clear()
                bindir = os.path.dirname(os.path.abspath(binp))
                env = clean_env(
                    home, PATH=f"{bindir}:{SAFE_PATH}",
                    COPILOT_HOME=os.path.join(home, ".copilot"),
                    COPILOT_AUTO_UPDATE="false")
                p = subprocess.run(
                    [wrap, "-p", "say hi", "-s", "--allow-all-tools",
                     "--no-auto-update"],
                    capture_output=True, text=True, timeout=120, env=env,
                    cwd=home)
            finally:
                stub.close()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("FERRY-STUB-OK", p.stdout)
        posts = [s for s in stub.seen if s[0] == "POST"]
        self.assertTrue(posts, "stub saw no POST")
        method, path, hdrs, body = posts[0]
        low = {k.lower(): v for k, v in hdrs.items()}
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(low.get("authorization"), f"Bearer {KEY}")
        self.assertEqual(low.get("x-ferry-client"), "host")
        self.assertEqual(json.loads(body)["model"], "heavy")
        self.assertTrue(json.loads(body).get("stream"), "expected a streamed call")


if __name__ == "__main__":
    unittest.main(verbosity=2)
