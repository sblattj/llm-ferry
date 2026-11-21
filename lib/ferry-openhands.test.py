#!/usr/bin/env python3
"""Stdlib unittest for `ferry openhands` (issue #3).

Run:  python3 lib/ferry-openhands.test.py

Failure case from the issue: on v1.51.0 `ferry openhands` prints "Unknown
command", so the OpenHands CLI cannot use a ferry lane without hand-editing
LLM_BASE_URL / agent_settings.json.

The wiring under test: a ferry-owned persistence dir with one agent_settings.json
(model, base URL, X-Ferry headers, no key) and an `openhands-ferry` wrapper that
exports OPENHANDS_PERSISTENCE_DIR / LLM_API_KEY / LLM_BASE_URL / LLM_MODEL and runs
`openhands --override-with-envs`. The user's ~/.openhands is never touched.

The e2e runs the real `openhands` (PATH, or env FERRY_E2E_BIN_OPENHANDS) headless
against a stub gateway on an ephemeral port; it never talks to port 8090.
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
KEY = "sk-oh-test-secret"
SYSPATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

MODULE_RUN = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-openhands.zsh\n"
              'cmd_openhands "$@"\n')


def _rd(path):
    with open(path) as f:
        return f.read()


def clean_env(home, path=SYSPATH):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("XDG_") or k.startswith("LLM_")
                   or k.startswith("OPENHANDS_")
                   or k in ("FERRY_FLEET", "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY",
                            "FERRY_LANE", "CLIENT_NAME"))}
    env["HOME"] = home
    env["PATH"] = path
    return env


def run_ferry(home, *args):
    p = subprocess.run(["zsh", "-c", MODULE_RUN, "ferry-openhands", *args],
                       capture_output=True, text=True, timeout=60,
                       env=clean_env(home))
    return p.returncode, p.stdout + p.stderr


class Stub:
    """A stub gateway: /v1/models plus a canned chat completion. Records requests."""

    def __init__(self, lanes=("heavy", "flash")):
        self.lanes = list(lanes)
        self.seen = []
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
                outer.seen.append(("GET", self.path, dict(self.headers), None))
                self._json({"data": [{"id": l} for l in outer.lanes]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.seen.append(("POST", self.path, dict(self.headers), body))
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for ch in ({"index": 0, "delta": {"role": "assistant", "content": "pong"}},
                               {"index": 0, "delta": {}, "finish_reason": "stop"}):
                        evt = {"id": "c1", "object": "chat.completion.chunk",
                               "created": 1, "model": "heavy", "choices": [ch]}
                        self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self._json({"id": "c1", "object": "chat.completion", "created": 1,
                                "model": "heavy",
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant",
                                                         "content": "pong"}}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                          "total_tokens": 2}})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def find_binary():
    b = os.environ.get("FERRY_E2E_BIN_OPENHANDS")
    if b and os.access(b, os.X_OK):
        return b
    return shutil.which("openhands")


class WireOpenHands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        # The user's own OpenHands state, which wiring must never touch.
        self.theirs = os.path.join(self.home, ".openhands")
        os.makedirs(self.theirs)
        self.theirs_file = os.path.join(self.theirs, "agent_settings.json")
        with open(self.theirs_file, "w") as f:
            f.write('{"llm": {"model": "anthropic/mine", "api_key": "user-own"}}\n')
        self.theirs_before = _rd(self.theirs_file)
        self.pdir = os.path.join(self.home, ".config", "ferry", "openhands")
        self.settings = os.path.join(self.pdir, "agent_settings.json")
        self.wrap = os.path.join(self.home, ".local", "bin", "openhands-ferry")
        self.stub = Stub()

    def tearDown(self):
        self.stub.close()
        self.tmp.cleanup()

    def wire(self, *extra, port=None):
        rc, out = run_ferry(self.home, "--host", "127.0.0.1",
                            "--port", str(port or self.stub.port),
                            "--key", KEY, *extra)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_names_the_command(self):
        rc, out = run_ferry(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry openhands", out)

    def test_wire_writes_wrapper_and_owned_settings_and_leaves_user_config(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        # user config byte for byte
        self.assertEqual(_rd(self.theirs_file), self.theirs_before)
        self.assertEqual(os.listdir(self.theirs), ["agent_settings.json"])
        # wrapper
        body = _rd(self.wrap)
        self.assertIn("ferry-managed: openhands", body)
        self.assertTrue(os.access(self.wrap, os.X_OK))
        self.assertIn(f"export LLM_BASE_URL=http://127.0.0.1:{self.stub.port}/v1", body)
        self.assertIn('export LLM_API_KEY="$FERRY_KEY"', body)
        self.assertIn("OPENHANDS_PERSISTENCE_DIR=" + self.pdir, body)
        self.assertIn('LLM_MODEL="openai/${FERRY_LANE:-heavy}"', body)
        self.assertIn("--override-with-envs", body)
        self.assertNotIn(KEY, body, "key baked into wrapper")
        # owned settings: model, base url, headers, no key
        d = json.loads(_rd(self.settings))
        self.assertEqual(d["llm"]["model"], "openai/heavy")
        self.assertEqual(d["llm"]["base_url"], f"http://127.0.0.1:{self.stub.port}/v1")
        self.assertEqual(d["llm"]["extra_headers"]["X-Ferry-Client"], "host")
        self.assertNotIn("api_key", d["llm"])
        self.assertNotIn(KEY, _rd(self.settings))

    def test_key_only_in_0600_file(self):
        self.wire()
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(_rd(keyfile), KEY)
        self.assertEqual(os.stat(keyfile).st_mode & 0o777, 0o600)
        for root, _, files in os.walk(self.home):
            for fn in files:
                p = os.path.join(root, fn)
                with open(p, "rb") as fh:
                    holds = KEY.encode() in fh.read()
                if holds:
                    self.assertEqual(os.stat(p).st_mode & 0o077, 0,
                                     f"{p} holds the key and is not private")

    def test_fleet_header_when_set(self):
        env = clean_env(self.home)
        env["FERRY_FLEET"] = "lab-7"
        p = subprocess.run(["zsh", "-c", MODULE_RUN, "x", "--host", "127.0.0.1",
                            "--port", str(self.stub.port), "--key", KEY],
                           capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        h = json.loads(_rd(self.settings))["llm"]["extra_headers"]
        self.assertEqual(h["X-Ferry-Fleet"], "lab-7")
        self.assertIn("X-Ferry-Client", h)

    def test_lane_falls_back_to_a_served_lane(self):
        self.stub.lanes = ["flash"]
        out = self.wire("--model", "nonexistent")
        self.assertIn("does not serve lane 'nonexistent'", out)
        d = json.loads(_rd(self.settings))
        self.assertEqual(d["llm"]["model"], "openai/flash")
        self.assertIn("${FERRY_LANE:-flash}", _rd(self.wrap))
        self.assertTrue(any(s[1] == "/v1/models" for s in self.stub.seen))

    def test_unreachable_gateway_still_wires(self):
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", "9", "--key", KEY)
        self.assertEqual(rc, 0, out)
        self.assertIn("could not verify the lane catalogue", out)
        self.assertEqual(json.loads(_rd(self.settings))["llm"]["model"], "openai/heavy")

    def test_rewire_is_idempotent(self):
        self.wire()
        first = (_rd(self.settings), _rd(self.wrap))
        self.wire()
        self.assertEqual((_rd(self.settings), _rd(self.wrap)), first)
        rec = json.loads(_rd(os.path.join(self.home, ".config", "ferry", "openhands.json")))
        self.assertEqual(sorted(c["path"] for c in rec["created"]),
                         sorted([self.settings, self.wrap]))
        self.assertEqual(rec["edited"], [])

    def test_unwire_removes_only_ferrys_files(self):
        self.wire()
        rc, out = run_ferry(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(self.settings))
        self.assertFalse(os.path.exists(
            os.path.join(self.home, ".config", "ferry", "openhands.json")))
        self.assertEqual(_rd(self.theirs_file), self.theirs_before)

    def test_unwire_keeps_a_settings_file_the_user_changed(self):
        self.wire()
        with open(self.settings, "a") as f:
            f.write("\n")
        rc, out = run_ferry(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.settings))
        self.assertIn("changed since ferry wrote it", out)

    def test_refuses_to_replace_a_foreign_settings_file(self):
        os.makedirs(self.pdir)
        with open(self.settings, "w") as f:
            f.write('{"llm": {"model": "x"}}\n')
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.settings), '{"llm": {"model": "x"}}\n')


class E2EOpenHands(unittest.TestCase):
    """The real `openhands` headless, through the wrapper, against a stub."""

    def test_headless_task_hits_the_stub_with_bearer_and_client_header(self):
        binary = find_binary()
        if not binary:
            self.skipTest("openhands binary not on PATH and FERRY_E2E_BIN_OPENHANDS unset")
        stub = Stub()
        self.addCleanup(stub.close)
        with tempfile.TemporaryDirectory() as home:
            rc, out = run_ferry(home, "--host", "127.0.0.1", "--port", str(stub.port),
                                "--key", KEY)
            self.assertEqual(rc, 0, out)
            settings = os.path.join(home, ".config", "ferry", "openhands", "agent_settings.json")
            before = _rd(settings)
            wrap = os.path.join(home, ".local", "bin", "openhands-ferry")
            env = clean_env(home, os.path.dirname(os.path.abspath(binary)) + ":" + SYSPATH)
            env["OPENHANDS_SUPPRESS_BANNER"] = "1"
            work = os.path.join(home, "work")
            os.makedirs(work)
            p = subprocess.run([wrap, "--headless", "-t", "say pong"], cwd=work,
                               capture_output=True, text=True, timeout=180, env=env,
                               stdin=subprocess.DEVNULL)
            log = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, log[-2000:])
            self.assertNotIn(KEY, log)
            posts = [s for s in stub.seen if s[0] == "POST"]
            self.assertTrue(posts, f"stub saw no POST: {stub.seen}\n{log[-1500:]}")
            method, path, headers, body = posts[0]
            hl = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(hl.get("authorization"), f"Bearer {KEY}")
            self.assertEqual(hl.get("x-ferry-client"), "host")
            self.assertEqual(body.get("model"), "heavy")
            self.assertIn("pong", log)
            # the ferry-owned settings were not rewritten with a key
            self.assertEqual(_rd(settings), before)
            # nothing landed in the user's own ~/.openhands
            self.assertFalse(os.path.exists(os.path.join(home, ".openhands", "agent_settings.json")))


if __name__ == "__main__":
    unittest.main()
