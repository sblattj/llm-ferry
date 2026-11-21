#!/usr/bin/env python3
"""Stdlib unittest for `ferry goose` (issue #3): a goose-ferry wrapper, no
Goose config file touched.

Run:  python3 lib/ferry-goose.test.py
Sourcing mode: the module is sourced next to ferry-core.zsh and
ferry-agents.zsh, the built `ferry` monolith need not know the command yet.
The e2e runs the real `goose` (PATH, or FERRY_E2E_BIN_GOOSE) headless through
the wrapper against a local stub; it never uses port 8090 and never touches
the real home, keychain or goose config.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
KEY = "sk-goose-test-secret"
LANES = ["heavy", "medium", "flash"]
SCRUB = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
         "FERRY_FLEET", "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY", "FERRY_MODEL")


def _rd(path):
    with open(path) as f:
        return f.read()


def _holds_key(path):
    try:
        with open(path, "rb") as f:
            return KEY.encode() in f.read()
    except OSError:
        return False


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in SCRUB and not k.startswith(("GOOSE_", "OPENAI_"))}
    env.update(HOME=home, PATH=BASE_PATH)
    env.update(extra)
    return env


def run_goose(home, *args, env_extra=None):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-goose.zsh\n"
              'cmd_goose "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-goose", *args],
                       capture_output=True, text=True, timeout=60,
                       env=clean_env(home, **(env_extra or {})))
    return p.returncode, p.stdout + p.stderr


class Stub:
    """Serves /v1/models (the lane catalogue) and canned chat/completions,
    streaming and not. Records every request."""

    def __init__(self):
        self.seen = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _rec(self, body=b""):
                stub.seen.append({"method": self.command, "path": self.path,
                                  "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "body": body})

            def do_GET(self):
                self._rec()
                if self.path.split("?")[0].rstrip("/").endswith("/models"):
                    out = json.dumps({"object": "list", "data": [
                        {"id": l, "object": "model"} for l in LANES]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    self.send_error(404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                self._rec(body)
                try:
                    req = json.loads(body or b"{}")
                except Exception:
                    req = {}
                model = req.get("model", "heavy")
                if not self.path.split("?")[0].endswith("/chat/completions"):
                    self.send_error(404)
                    return
                usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
                if req.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [
                        {"id": "c1", "object": "chat.completion.chunk", "model": model,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "FERRY-STUB-OK"},
                                      "finish_reason": None}]},
                        {"id": "c1", "object": "chat.completion.chunk", "model": model,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                        {"id": "c1", "object": "chat.completion.chunk", "model": model,
                         "choices": [], "usage": usage},
                    ]
                    for c in chunks:
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    out = json.dumps({"id": "c1", "object": "chat.completion", "model": model,
                                      "choices": [{"index": 0, "finish_reason": "stop",
                                                   "message": {"role": "assistant",
                                                               "content": "FERRY-STUB-OK"}}],
                                      "usage": usage}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class GooseWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.cfgdir = os.path.join(self.home, ".config", "goose")
        os.makedirs(self.cfgdir)
        self.user_cfg = os.path.join(self.cfgdir, "config.yaml")
        self.seed = "GOOSE_PROVIDER: anthropic\nGOOSE_MODEL: mine\n# my own comment\n"
        with open(self.user_cfg, "w") as f:
            f.write(self.seed)
        self.wrap = os.path.join(self.home, ".local", "bin", "goose-ferry")
        self.stub = Stub()

    def tearDown(self):
        self.stub.close()
        self.tmp.cleanup()

    def wire(self, *extra, env_extra=None, port=None):
        rc, out = run_goose(self.home, "--host", "127.0.0.1",
                            "--port", str(port or self.stub.port),
                            "--key", KEY, *extra, env_extra=env_extra)
        self.assertEqual(rc, 0, out)
        return out

    def all_files(self):
        for d, _, fs in os.walk(self.home):
            for f in fs:
                yield os.path.join(d, f)

    def test_help_names_the_command(self):
        rc, out = run_goose(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry goose", out)
        self.assertIn("goose-ferry", out)

    def test_wire_writes_wrapper_record_key_and_leaves_user_config_alone(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        self.assertEqual(_rd(self.user_cfg), self.seed, "user config.yaml changed")
        self.assertEqual([f for f in os.listdir(self.cfgdir)], ["config.yaml"],
                         "ferry wrote something into the goose config dir")
        body = _rd(self.wrap)
        self.assertIn("ferry-managed: goose", body)
        self.assertIn(f"OPENAI_BASE_URL=http://127.0.0.1:{self.stub.port}/v1", body)
        self.assertIn("GOOSE_PROVIDER=openai", body)
        self.assertIn("GOOSE_MODEL=\"${FERRY_MODEL:-heavy}\"", body)
        self.assertIn('OPENAI_API_KEY="$FERRY_KEY"', body)
        self.assertIn("unset OPENAI_HOST OPENAI_BASE_PATH", body)
        self.assertIn("X-Ferry-Client=host", body)
        self.assertNotIn(KEY, body, "key baked into the wrapper")
        self.assertTrue(os.access(self.wrap, os.X_OK))
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(_rd(keyfile), KEY)
        self.assertEqual(os.stat(keyfile).st_mode & 0o777, 0o600)
        for p in self.all_files():
            if p in (self.wrap,):
                continue
            if _holds_key(p):
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600,
                                 f"{p} holds the key but is not 0600")
        rec = json.loads(_rd(os.path.join(self.home, ".config", "ferry", "goose.json")))
        self.assertEqual([c["path"] for c in rec["created"]], [self.wrap])
        self.assertEqual(rec["edited"], [])

    def test_fleet_header_when_set(self):
        self.wire(env_extra={"FERRY_FLEET": "pod-7"})
        body = _rd(self.wrap)
        self.assertIn("X-Ferry-Client=host,X-Ferry-Fleet=pod-7", body)

    def test_unsafe_header_characters_are_neutralised(self):
        self.wire(env_extra={"FERRY_FLEET": "a,b=c"})
        self.assertIn("X-Ferry-Fleet=a_b_c", _rd(self.wrap))

    def test_lane_check_falls_back_to_a_served_lane(self):
        out = self.wire("--model", "nope")
        self.assertIn("does not serve lane 'nope'", out)
        self.assertIn("${FERRY_MODEL:-heavy}", _rd(self.wrap))
        rec = json.loads(_rd(os.path.join(self.home, ".config", "ferry", "goose.json")))
        self.assertEqual(rec["lanes"], LANES)
        self.assertTrue(any(s["path"] == "/v1/models" for s in self.stub.seen))

    def test_served_lane_is_kept(self):
        self.wire("--model", "flash")
        self.assertIn("${FERRY_MODEL:-flash}", _rd(self.wrap))

    def test_unreachable_host_still_wires(self):
        out = self.wire(port=9)
        self.assertIn("could not verify the lane catalogue", out)
        self.assertTrue(os.path.exists(self.wrap))

    def test_rewire_is_idempotent_and_unwire_restores_everything(self):
        self.wire()
        first = _rd(self.wrap)
        self.wire()
        self.assertEqual(_rd(self.wrap), first)
        rc, out = run_goose(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_rd(self.user_cfg), self.seed)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "goose.json")))
        self.assertEqual(os.listdir(self.cfgdir), ["config.yaml"])

    def test_refuses_to_replace_a_user_file_at_the_wrapper_path(self):
        os.makedirs(os.path.dirname(self.wrap))
        with open(self.wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_goose(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.wrap), "#!/bin/sh\necho mine\n")


def find_goose():
    explicit = os.environ.get("FERRY_E2E_BIN_GOOSE")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("goose") or (
        "/opt/homebrew/bin/goose" if os.access("/opt/homebrew/bin/goose", os.X_OK) else None)


class GooseEndToEnd(unittest.TestCase):
    """The real goose, headless, through goose-ferry, HOME = a throwaway dir."""

    def setUp(self):
        self.goose = find_goose()
        if not self.goose:
            self.skipTest("goose binary not found (PATH or FERRY_E2E_BIN_GOOSE)")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.stub = Stub()

    def tearDown(self):
        if getattr(self, "stub", None):
            self.stub.close()
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def run_wrapper(self, *args, env_extra=None):
        rc, out = run_goose(self.home, "--host", "127.0.0.1", "--port",
                            str(self.stub.port), "--key", KEY)
        self.assertEqual(rc, 0, out)
        wrap = os.path.join(self.home, ".local", "bin", "goose-ferry")
        env = clean_env(
            self.home,
            PATH=os.path.dirname(self.goose) + ":" + BASE_PATH,
            # test-only isolation: no keychain, no telemetry, no real dirs
            GOOSE_DISABLE_KEYRING="1", GOOSE_TELEMETRY_OFF="1",
            XDG_CONFIG_HOME=os.path.join(self.home, "xdg", "config"),
            XDG_DATA_HOME=os.path.join(self.home, "xdg", "data"),
            XDG_STATE_HOME=os.path.join(self.home, "xdg", "state"),
            **(env_extra or {}))
        self.stub.seen.clear()
        return subprocess.run([wrap, *args], capture_output=True, text=True,
                              timeout=120, env=env, stdin=subprocess.DEVNULL,
                              cwd=self.home)

    def test_headless_run_hits_the_stub_with_key_and_ferry_headers(self):
        p = self.run_wrapper("run", "-t", "say hi", "--no-session")
        out = p.stdout + p.stderr
        posts = [s for s in self.stub.seen if s["method"] == "POST"]
        self.assertTrue(posts, f"goose never POSTed to the stub (rc={p.returncode}): {out[-800:]}")
        post = posts[0]
        self.assertEqual(post["path"], "/v1/chat/completions")
        self.assertEqual(post["headers"].get("authorization"), f"Bearer {KEY}")
        self.assertEqual(post["headers"].get("x-ferry-client"), "host")
        req = json.loads(post["body"])
        self.assertEqual(req["model"], "heavy")
        self.assertIn("FERRY-STUB-OK", out, f"canned reply not shown: {out[-800:]}")
        self.assertEqual(p.returncode, 0, out[-800:])
        # the wrapper must not have created goose config that carries the key
        for d, _, fs in os.walk(self.home):
            for f in fs:
                path = os.path.join(d, f)
                if path.endswith(("goose-ferry",)):
                    continue
                if _holds_key(path):
                    self.assertEqual(os.stat(path).st_mode & 0o777, 0o600,
                                     f"{path} holds the key but is not 0600")

    def test_ferry_model_picks_another_lane(self):
        p = self.run_wrapper("run", "-t", "say hi", "--no-session",
                             env_extra={"FERRY_MODEL": "flash"})
        posts = [s for s in self.stub.seen if s["method"] == "POST"]
        self.assertTrue(posts, (p.stdout + p.stderr)[-800:])
        self.assertEqual(json.loads(posts[0]["body"])["model"], "flash")

    def test_ambient_openai_host_cannot_redirect_the_wrapper(self):
        p = self.run_wrapper("run", "-t", "say hi", "--no-session",
                             env_extra={"OPENAI_HOST": "http://127.0.0.1:9",
                                        "OPENAI_BASE_PATH": "nope/chat/completions"})
        posts = [s for s in self.stub.seen if s["method"] == "POST"]
        self.assertTrue(posts, (p.stdout + p.stderr)[-800:])
        self.assertEqual(posts[0]["path"], "/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
