#!/usr/bin/env python3
"""Stdlib unittest for `ferry interpreter` (issue #3): Open Interpreter, the
Rust / Codex-based CLI (binary `interpreter`), wired by a ferry-owned wrapper
~/.local/bin/interpreter-ferry that carries `-c` config overrides. Nothing in
~/.openinterpreter is read or written by the wiring.

Run:  python3 lib/ferry-interpreter.test.py
The e2e class runs the real binary (PATH, or env FERRY_E2E_BIN_INTERPRETER)
against a local stub only: zero tokens, never the live gateway.
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
KEY = "sk-test-interp-0123456789"
SEED = '# my own config\nmodel = "gpt-x"\n\n[projects."/tmp/x"]\ntrust_level = "trusted"\n'

DROP_ENV = ("FERRY_FLEET", "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY", "CLIENT_NAME",
            "INTERPRETER_HOME", "OPEN_INTERPRETER_HOME", "OPEN_INTERPRETER_BRAND",
            "OPENAI_API_KEY", "FERRY_INTERPRETER_KEY", "CODEX_HOME")


def _rd(path):
    with open(path) as f:
        return f.read()


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in DROP_ENV and not k.startswith("XDG_")}
    env["HOME"] = home
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    env.update(extra)
    return env


def run_ferry(home, *args, **envx):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-interpreter.zsh\n"
              'cmd_interpreter "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-interpreter", *args],
                       capture_output=True, text=True, timeout=60,
                       env=clean_env(home, **envx))
    return p.returncode, p.stdout + p.stderr


class Stub:
    """GET /v1/models (lane catalogue) + POST /v1/chat/completions (canned)."""

    def __init__(self, lanes=("heavy", "flash")):
        self.lanes = list(lanes)
        self.seen = []          # (method, path, headers dict, body bytes)
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _record(self, body=b""):
                stub.seen.append((self.command, self.path,
                                  {k.lower(): v for k, v in self.headers.items()}, body))

            def do_GET(self):
                self._record()
                b = json.dumps({"data": [{"id": l} for l in stub.lanes]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._record(body)
                try:
                    req = json.loads(body or b"{}")
                except ValueError:
                    req = {}
                usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
                if req.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [
                        {"choices": [{"index": 0, "delta": {"role": "assistant",
                                                            "content": "canned "}}]},
                        {"choices": [{"index": 0, "delta": {"content": "reply"},
                                      "finish_reason": "stop"}], "usage": usage}]
                    for c in chunks:
                        self.wfile.write(("data: %s\n\n" % json.dumps(c)).encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    b = json.dumps({"id": "x", "object": "chat.completion",
                                    "model": req.get("model", "heavy"),
                                    "choices": [{"index": 0, "message": {
                                        "role": "assistant", "content": "canned reply"},
                                        "finish_reason": "stop"}],
                                    "usage": usage}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class WireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.oi = os.path.join(self.home, ".openinterpreter")
        os.makedirs(self.oi)
        self.cfg = os.path.join(self.oi, "config.toml")
        with open(self.cfg, "w") as f:
            f.write(SEED)
        self.wrap = os.path.join(self.home, ".local", "bin", "interpreter-ferry")
        self.rec = os.path.join(self.home, ".config", "ferry", "interpreter.json")
        self.stub = Stub()
        self.addCleanup(self.stub.close)
        self.addCleanup(self.tmp.cleanup)

    def wire(self, *extra, **envx):
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port",
                            str(self.stub.port), "--key", KEY, *extra, **envx)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_names_the_command(self):
        rc, out = run_ferry(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry interpreter", out)
        self.assertTrue(out.startswith("ferry interpreter - "), out[:60])
        self.assertIn("Common flags", out)

    def test_wire_writes_wrapper_and_leaves_user_config_alone(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        body = _rd(self.wrap)
        self.assertTrue(os.access(self.wrap, os.X_OK))
        self.assertIn("ferry-managed: interpreter", body)
        self.assertIn(f"model_providers.ferry.base_url=\"http://127.0.0.1:{self.stub.port}/v1\"", body)
        self.assertIn('model_providers.ferry.wire_api="chat"', body)
        self.assertIn('model_providers.ferry.env_key="FERRY_INTERPRETER_KEY"', body)
        self.assertIn('export FERRY_INTERPRETER_KEY="$FERRY_KEY"', body)
        self.assertIn('model="heavy"', body)
        self.assertIn("exec interpreter ", body)
        # user's own config and home: byte for byte, nothing new inside it
        self.assertEqual(_rd(self.cfg), SEED)
        self.assertEqual(sorted(os.listdir(self.oi)), ["config.toml"])
        rec = json.loads(_rd(self.rec))
        self.assertEqual([c["path"] for c in rec["created"]], [self.wrap])
        self.assertEqual(rec["edited"], [])

    def test_headers_present_and_fleet_forms(self):
        self.wire()
        body = _rd(self.wrap)
        self.assertIn('http_headers={"X-Ferry-Client"="host"}', body)
        self.assertIn('env_http_headers={"X-Ferry-Fleet"="FERRY_FLEET"}', body)
        self.wire(FERRY_FLEET="lab")
        body = _rd(self.wrap)
        self.assertIn('"X-Ferry-Fleet"="lab"', body)
        self.assertNotIn("env_http_headers", body)

    def test_key_only_in_0600_files_never_stdout_or_wrapper(self):
        out = self.wire()
        self.assertNotIn(KEY, out)
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(_rd(keyfile), KEY)
        holders = []
        for root, _d, files in os.walk(self.home):
            for fn in files:
                p = os.path.join(root, fn)
                with open(p, "rb") as f:
                    if KEY.encode() in f.read():
                        holders.append(p)
                        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)
        self.assertEqual(holders, [keyfile])
        self.assertNotIn(KEY, _rd(self.wrap))

    def test_lane_check_falls_back_to_a_served_lane(self):
        self.stub.lanes = ["flash", "medium"]
        out = self.wire()
        self.assertIn("does not serve lane 'heavy'", out)
        self.assertIn('model="flash"', _rd(self.wrap))
        gets = [s for s in self.stub.seen if s[0] == "GET"]
        self.assertTrue(gets and gets[0][1] == "/v1/models")
        self.assertEqual(gets[0][2].get("authorization"), "Bearer " + KEY)

    def test_lane_flag_is_honoured_when_served(self):
        self.stub.lanes = ["heavy", "medium"]
        self.wire("--model", "medium")
        self.assertIn('model="medium"', _rd(self.wrap))

    def test_rewire_is_idempotent(self):
        self.wire()
        first = _rd(self.wrap)
        rec1 = json.loads(_rd(self.rec))
        self.wire()
        self.assertEqual(_rd(self.wrap), first)
        rec2 = json.loads(_rd(self.rec))
        self.assertEqual(rec1["created"], rec2["created"])
        self.assertEqual(_rd(self.cfg), SEED)

    def test_unwire_removes_ferry_files_and_keeps_user_config(self):
        self.wire()
        rc, out = run_ferry(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(self.rec))
        self.assertEqual(_rd(self.cfg), SEED)
        self.assertEqual(sorted(os.listdir(self.oi)), ["config.toml"])

    def test_refuses_to_replace_a_user_file_at_the_wrapper_path(self):
        os.makedirs(os.path.dirname(self.wrap))
        with open(self.wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.wrap), "#!/bin/sh\necho mine\n")


def find_binary():
    cand = os.environ.get("FERRY_E2E_BIN_INTERPRETER")
    if cand:
        return cand if os.access(cand, os.X_OK) else None
    return shutil.which("interpreter")


class E2E(unittest.TestCase):
    def test_exec_through_the_wrapper_hits_the_stub(self):
        binary = find_binary()
        if not binary:
            self.skipTest("no `interpreter` binary on PATH and FERRY_E2E_BIN_INTERPRETER unset")
        ver = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
        if not ver.startswith("interpreter 0.0."):
            self.skipTest(f"`{binary}` is not the Rust Open Interpreter (version {ver!r})")
        stub = Stub()
        self.addCleanup(stub.close)
        with tempfile.TemporaryDirectory() as home:
            rc, out = run_ferry(home, "--host", "127.0.0.1", "--port", str(stub.port),
                                "--key", KEY)
            self.assertEqual(rc, 0, out)
            wrap = os.path.join(home, ".local", "bin", "interpreter-ferry")
            env = clean_env(home, PATH="%s:%s:/usr/bin:/bin" % (
                os.path.dirname(os.path.abspath(binary)), os.path.dirname(wrap)))
            work = os.path.join(home, "work")
            os.makedirs(work)
            p = subprocess.run([wrap, "exec", "--skip-git-repo-check", "say hi"],
                               capture_output=True, text=True, timeout=120, env=env,
                               cwd=work, stdin=subprocess.DEVNULL)
            blob = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, blob)
            self.assertIn("canned reply", blob)
            self.assertNotIn(KEY, blob)
            posts = [s for s in stub.seen if s[0] == "POST"]
            self.assertTrue(posts, "stub saw no POST: %r" % [s[:2] for s in stub.seen])
            method, path, hdrs, body = posts[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(hdrs.get("authorization"), "Bearer " + KEY)
            self.assertEqual(hdrs.get("x-ferry-client"), "host")
            self.assertEqual(json.loads(body).get("model"), "heavy")
            # the user-level config dir got no ferry provider
            cfg = os.path.join(home, ".openinterpreter", "config.toml")
            if os.path.exists(cfg):
                self.assertNotIn("ferry", _rd(cfg))


if __name__ == "__main__":
    unittest.main()
