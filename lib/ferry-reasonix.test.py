#!/usr/bin/env python3
"""Stdlib unittest for `ferry reasonix` (issue #3).

Run:  python3 lib/ferry-reasonix.test.py   (sourcing mode: sources the lib
files directly, the built `ferry` monolith does not know the command yet)

Pins: the ferry-owned REASONIX_HOME and wrapper, the user's own ~/.reasonix
surviving byte for byte, the key only in 0600 files and never in stdout or the
wrapper, the headers, the lane fallback, idempotent re-wire, exact unwire and
(when a real `reasonix` binary exists) an e2e through `reasonix-ferry` against
a stub that answers /v1/chat/completions.
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
KEY = "sk-test-reasonix-secret"
USER_CONFIG = '# my own reasonix config\ndefault_model = "deepseek"\n\n[ui]\ntheme = "dark"\n'
USER_ENV = "DEEPSEEK_API_KEY=user-own-key\n"
CLEAN_ENV_DROP = ("FERRY_FLEET", "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY",
                  "CLIENT_NAME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                  "XDG_STATE_HOME", "XDG_CACHE_HOME")


def _rd(path):
    with open(path) as f:
        return f.read()


def _clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in CLEAN_ENV_DROP and not k.startswith("REASONIX_")}
    env["HOME"] = home
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    env.update(extra)
    return env


def run_reasonix(home, *args, **extra_env):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-reasonix.zsh\n"
              'cmd_reasonix "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-reasonix", *args],
                       capture_output=True, text=True, timeout=60,
                       env=_clean_env(home, **extra_env))
    return p.returncode, p.stdout + p.stderr


def find_binary():
    cand = os.environ.get("FERRY_E2E_BIN_REASONIX")
    if cand and os.access(cand, os.X_OK):
        return cand
    return shutil.which("reasonix")


class Stub:
    """/v1/models plus a canned /v1/chat/completions; records every request."""

    def __init__(self, lanes=("heavy", "flash")):
        self.lanes = list(lanes)
        self.seen = []
        stub = self

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
                stub.seen.append(("GET", self.path, dict(self.headers), None))
                self._json({"object": "list",
                            "data": [{"id": l, "object": "model"} for l in stub.lanes]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.seen.append(("POST", self.path, dict(self.headers), body))
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [
                        {"choices": [{"index": 0, "delta": {"role": "assistant",
                                                            "content": "pong-from-stub"}}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                                   "total_tokens": 5}},
                    ]
                    for c in chunks:
                        self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self._json({"id": "x", "object": "chat.completion", "model": "heavy",
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant",
                                                         "content": "pong-from-stub"}}],
                                "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                                          "total_tokens": 5}})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ReasonixWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.udir = os.path.join(self.home, ".reasonix")
        os.makedirs(self.udir)
        self.ucfg = os.path.join(self.udir, "config.toml")
        self.uenv = os.path.join(self.udir, ".env")
        with open(self.ucfg, "w") as f:
            f.write(USER_CONFIG)
        with open(self.uenv, "w") as f:
            f.write(USER_ENV)
        self.rhome = os.path.join(self.home, ".config", "ferry", "reasonix", "home")
        self.wrap = os.path.join(self.home, ".local", "bin", "reasonix-ferry")
        self.rec = os.path.join(self.home, ".config", "ferry", "reasonix.json")
        self.stub = Stub()

    def tearDown(self):
        self.stub.close()
        self.tmp.cleanup()

    def wire(self, *extra, port=None, **env):
        rc, out = run_reasonix(self.home, "--host", "127.0.0.1",
                               "--port", str(port or self.stub.port),
                               "--key", KEY, *extra, **env)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_names_the_command(self):
        rc, out = run_reasonix(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry reasonix", out)
        self.assertTrue(out.startswith("ferry reasonix - "), out[:60])

    def test_wire_writes_home_wrapper_and_leaves_the_users_reasonix_alone(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        # the user's own config and credentials: byte for byte
        self.assertEqual(_rd(self.ucfg), USER_CONFIG)
        self.assertEqual(_rd(self.uenv), USER_ENV)
        self.assertEqual(sorted(os.listdir(self.udir)), [".env", "config.toml"],
                         "ferry wrote into ~/.reasonix")
        cfg = _rd(os.path.join(self.rhome, "config.toml"))
        self.assertIn("ferry-managed: reasonix", cfg)
        self.assertIn('default_model = "ferry"', cfg)
        self.assertIn('kind        = "openai"', cfg)
        self.assertIn(f'base_url    = "http://127.0.0.1:{self.stub.port}/v1"', cfg)
        self.assertIn('models      = ["heavy", "flash"]', cfg)
        self.assertIn('default     = "heavy"', cfg)
        self.assertIn('api_key_env = "FERRY_API_KEY"', cfg)
        self.assertIn('"X-Ferry-Client" = "host"', cfg)
        self.assertNotIn(KEY, cfg, "key written into config.toml")
        wrap = _rd(self.wrap)
        self.assertIn("ferry-managed: reasonix", wrap)
        self.assertIn(f"export REASONIX_HOME={self.rhome}", wrap)
        self.assertIn("export REASONIX_CREDENTIALS_STORE=file", wrap)
        self.assertIn("exec reasonix", wrap)
        self.assertNotIn(KEY, wrap, "key baked into the wrapper")
        self.assertTrue(os.access(self.wrap, os.X_OK))
        rec = json.loads(_rd(self.rec))
        self.assertIn(self.wrap, [c["path"] for c in rec["created"]])
        self.assertEqual(rec["edited"][0]["kind"], "reasonix-env")

    def test_key_is_only_in_0600_files(self):
        self.wire()
        envfile = os.path.join(self.rhome, ".env")
        self.assertEqual(_rd(envfile), f"FERRY_API_KEY={KEY}\n")
        holders = []
        for root, _dirs, files in os.walk(self.home):
            for name in files:
                p = os.path.join(root, name)
                with open(p, "rb") as fh:
                    hit = KEY.encode() in fh.read()
                if hit:
                    holders.append(p)
                    self.assertEqual(os.stat(p).st_mode & 0o777, 0o600,
                                     f"{p} holds the key and is not 0600")
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(sorted(holders), sorted([envfile, keyfile]))
        self.assertIn(envfile, holders)
        self.assertIn(keyfile, holders)
        self.assertNotIn(self.wrap, holders)

    def test_fleet_header_when_fleet_is_set(self):
        self.wire(FERRY_FLEET="crew-7")
        cfg = _rd(os.path.join(self.rhome, "config.toml"))
        self.assertIn('"X-Ferry-Fleet" = "crew-7"', cfg)

    def test_lane_fallback_when_the_host_does_not_serve_the_lane(self):
        out = self.wire("--model", "nonexistent-lane")
        self.assertIn("does not serve lane 'nonexistent-lane'", out)
        cfg = _rd(os.path.join(self.rhome, "config.toml"))
        self.assertIn('default     = "heavy"', cfg)
        self.assertNotIn("nonexistent-lane", cfg)

    def test_every_served_lane_is_a_model(self):
        self.stub.lanes[:] = ["heavy", "medium", "flash"]
        self.wire("--model", "medium")
        cfg = _rd(os.path.join(self.rhome, "config.toml"))
        self.assertIn('models      = ["heavy", "medium", "flash"]', cfg)
        self.assertIn('default     = "medium"', cfg)

    def test_rewire_is_idempotent_and_unwire_restores_exactly(self):
        self.wire()
        first = _rd(os.path.join(self.rhome, "config.toml"))
        first_wrap = _rd(self.wrap)
        self.wire()
        self.assertEqual(_rd(os.path.join(self.rhome, "config.toml")), first)
        self.assertEqual(_rd(self.wrap), first_wrap)
        rc, out = run_reasonix(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_rd(self.ucfg), USER_CONFIG)
        self.assertEqual(_rd(self.uenv), USER_ENV)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(self.rec))
        self.assertFalse(os.path.exists(self.rhome), "ferry home left behind")
        self.assertEqual(sorted(os.listdir(self.udir)), [".env", "config.toml"])

    def test_unwire_removes_env_even_after_the_wrapper_rewrote_it(self):
        self.wire()
        with open(os.path.join(self.rhome, ".env"), "w") as f:
            f.write("FERRY_API_KEY=rotated\n")
        rc, out = run_reasonix(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.rhome))

    def test_unwire_keeps_a_home_with_user_sessions(self):
        self.wire()
        os.makedirs(os.path.join(self.rhome, "sessions"))
        rc, out = run_reasonix(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(os.path.join(self.rhome, "sessions")))
        self.assertIn("Kept", out)
        self.assertFalse(os.path.exists(os.path.join(self.rhome, ".env")))

    def test_refuses_a_user_file_at_the_wrapper_or_config_path(self):
        os.makedirs(os.path.dirname(self.wrap))
        with open(self.wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_reasonix(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.wrap), "#!/bin/sh\necho mine\n")
        os.remove(self.wrap)
        os.makedirs(self.rhome)
        with open(os.path.join(self.rhome, "config.toml"), "w") as f:
            f.write("mine = true\n")
        rc, out = run_reasonix(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertEqual(_rd(os.path.join(self.rhome, "config.toml")), "mine = true\n")

    def test_dry_run_unwire_changes_nothing(self):
        self.wire()
        rc, out = run_reasonix(self.home, "--unwire", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("[dry-run]", out)
        self.assertTrue(os.path.exists(self.wrap))
        self.assertTrue(os.path.exists(os.path.join(self.rhome, ".env")))
        self.assertTrue(os.path.exists(self.rec))


class ReasonixE2E(unittest.TestCase):
    """The real binary through reasonix-ferry, against a stub, zero tokens."""

    def setUp(self):
        self.bin = find_binary()
        if not self.bin:
            self.skipTest("reasonix binary not found (PATH or FERRY_E2E_BIN_REASONIX)")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.proj = os.path.join(self.home, "proj")
        os.makedirs(self.proj)
        # a user config pointing at a dead provider: reasonix-ferry must ignore it
        os.makedirs(os.path.join(self.home, ".reasonix"))
        with open(os.path.join(self.home, ".reasonix", "config.toml"), "w") as f:
            f.write(USER_CONFIG)
        self.stub = Stub()

    def tearDown(self):
        if hasattr(self, "stub"):
            self.stub.close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_run_prompt_goes_through_the_ferry_provider(self):
        rc, out = run_reasonix(self.home, "--host", "127.0.0.1",
                               "--port", str(self.stub.port), "--key", KEY)
        self.assertEqual(rc, 0, out)
        bindir = os.path.join(self.home, "realbin")
        os.makedirs(bindir)
        os.symlink(self.bin, os.path.join(bindir, "reasonix"))
        env = _clean_env(self.home)
        env["PATH"] = bindir + ":" + env["PATH"]
        wrap = os.path.join(self.home, ".local", "bin", "reasonix-ferry")
        p = subprocess.run([wrap, "run", "-p", "say ping"], cwd=self.proj, env=env,
                           capture_output=True, text=True, timeout=90)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("pong-from-stub", p.stdout)
        posts = [s for s in self.stub.seen if s[0] == "POST"]
        self.assertTrue(posts, f"stub saw no POST: {self.stub.seen}")
        method, path, headers, body = posts[0]
        self.assertEqual(path, "/v1/chat/completions")
        h = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(h.get("authorization"), f"Bearer {KEY}")
        self.assertEqual(h.get("x-ferry-client"), "host")
        self.assertEqual(body.get("model"), "heavy")
        self.assertTrue(body.get("stream"), "reasonix is expected to stream")
        # nothing leaked into the user's own reasonix home
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, ".reasonix"))),
                         ["config.toml"])


if __name__ == "__main__":
    unittest.main()
