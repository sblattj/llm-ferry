#!/usr/bin/env python3
"""Stdlib unittest for `ferry aider` (issue #3): wire Aider to ferry through an
`aider-ferry` wrapper and two ferry-owned settings files.

Run:  python3 lib/ferry-aider.test.py

Wiring tests source lib/ferry-core.zsh + ferry-agents.zsh + ferry-aider.zsh in
a zsh subprocess against a throwaway HOME and a stub /v1/models on an
ephemeral port. The e2e runs the REAL aider (PATH, or FERRY_E2E_BIN_AIDER)
through the wrapper against a stub that answers /v1/chat/completions, with git,
analytics, update checks and release notes switched off. Nothing here touches
port 8090 or the real home directory; skipped when aider is absent.
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
CATALOGUE = ["heavy", "medium", "flash"]
KEY = "sk-aider-test-key-0123456789"
SEED_CONF = "# my own aider config\nmodel: gpt-4o\ndark-mode: true\n"
SEED_SETTINGS = "- name: gpt-4o\n  edit_format: udiff\n"


def _rd(path):
    with open(path) as f:
        return f.read()


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("XDG_", "AIDER_", "OPENAI_", "LITELLM_"))
                   or k in ("FERRY_FLEET", "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY",
                            "CLIENT_NAME", "CLIENT_MODE", "OPENAI_API_KEY"))}
    env["HOME"] = home
    env.update(extra)
    return env


def run_ferry(home, *args, env_extra=None):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-aider.zsh\n"
              'cmd_aider "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-aider", *args],
                       capture_output=True, text=True, timeout=90,
                       env=clean_env(home, **(env_extra or {})))
    return p.returncode, p.stdout + p.stderr


class Stub:
    """Serves GET /v1/models and POST /v1/chat/completions (stream + not)."""

    def __init__(self, catalogue=None):
        self.seen = []          # (method, path, headers dict, body text)
        self.catalogue = CATALOGUE if catalogue is None else catalogue
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _rec(self, body=""):
                stub.seen.append((self.command, self.path,
                                  {k.lower(): v for k, v in self.headers.items()}, body))

            def do_GET(self):
                self._rec()
                if self.path.rstrip("/").endswith("/models"):
                    data = json.dumps({"object": "list", "data": [
                        {"id": m, "object": "model"} for m in stub.catalogue]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                self._rec(body)
                try:
                    want_stream = bool(json.loads(body).get("stream"))
                except Exception:
                    want_stream = False
                if not self.path.endswith("/chat/completions"):
                    self.send_response(404)
                    self.end_headers()
                    return
                text = "FERRY-STUB-REPLY"
                if want_stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for i, piece in enumerate(["FERRY-STUB-", "REPLY"]):
                        ch = {"id": "c1", "object": "chat.completion.chunk", "created": 1,
                              "model": "heavy", "choices": [{"index": 0,
                              "delta": {"role": "assistant", "content": piece},
                              "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
                    fin = {"id": "c1", "object": "chat.completion.chunk", "created": 1,
                           "model": "heavy", "choices": [{"index": 0, "delta": {},
                           "finish_reason": "stop"}]}
                    self.wfile.write(f"data: {json.dumps(fin)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    data = json.dumps({"id": "c1", "object": "chat.completion",
                                       "created": 1, "model": "heavy",
                                       "choices": [{"index": 0, "finish_reason": "stop",
                                                    "message": {"role": "assistant",
                                                                "content": text}}],
                                       "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                 "total_tokens": 2}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def _all_files(root):
    for d, _, fs in os.walk(root):
        for f in fs:
            yield os.path.join(d, f)


class Wiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.conf = os.path.join(self.home, ".aider.conf.yml")
        self.msettings = os.path.join(self.home, ".aider.model.settings.yml")
        with open(self.conf, "w") as f:
            f.write(SEED_CONF)
        with open(self.msettings, "w") as f:
            f.write(SEED_SETTINGS)
        self.cfg = os.path.join(self.home, ".config", "ferry", "aider")
        self.wrap = os.path.join(self.home, ".local", "bin", "aider-ferry")

    def tearDown(self):
        self.tmp.cleanup()

    def wire(self, stub, *extra, key=KEY):
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", str(stub.port),
                            "--key", key, *extra)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_names_the_command(self):
        rc, out = run_ferry(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry aider", out)
        self.assertIn("aider-ferry", out)

    def test_wire_files_headers_and_user_config_untouched(self):
        with Stub() as stub:
            out = self.wire(stub)
            base = f"http://127.0.0.1:{stub.port}/v1"
            self.assertNotIn(KEY, out, "key leaked to stdout")
            # the user's own files survive byte for byte
            self.assertEqual(_rd(self.conf), SEED_CONF)
            self.assertEqual(_rd(self.msettings), SEED_SETTINGS)
            body = _rd(self.wrap)
            self.assertIn("ferry-managed: aider", body)
            self.assertIn("--openai-api-base " + base, body)
            self.assertIn("--model openai/heavy", body)
            self.assertIn("--model-settings-file", body)
            self.assertIn("--model-metadata-file", body)
            self.assertIn('OPENAI_API_KEY="$FERRY_KEY"', body)
            self.assertNotIn(KEY, body, "key baked into wrapper")
            self.assertTrue(os.access(self.wrap, os.X_OK))
            settings = _rd(os.path.join(self.cfg, "model-settings.yml"))
            self.assertIn("name: aider/extra_params", settings)
            self.assertIn('"X-Ferry-Client": "host"', settings)
            for lane in CATALOGUE:
                self.assertIn(f'name: "openai/{lane}"', settings)
            meta = os.path.join(self.cfg, "model-metadata.json")
            self.assertIn('"openai/medium"', _rd(meta))
            # the key: only in the 0600 key file, never in any other file
            keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
            self.assertEqual(_rd(keyfile), KEY)
            self.assertEqual(stat.S_IMODE(os.stat(keyfile).st_mode), 0o600)
            for p in _all_files(self.home):
                if p == keyfile:
                    continue
                self.assertNotIn(KEY, _rd(p), f"key found in {p}")
            rec = json.loads(_rd(os.path.join(self.home, ".config", "ferry", "aider.json")))
            self.assertEqual(rec["model"], "heavy")
            self.assertEqual(rec["edited"], [])
            self.assertEqual(len(rec["created"]), 3)

    def test_fleet_header_when_set(self):
        with Stub() as stub:
            rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", str(stub.port),
                                "--key", KEY, env_extra={"FERRY_FLEET": "pool-a"})
            self.assertEqual(rc, 0, out)
            self.assertIn('"X-Ferry-Fleet": "pool-a"',
                          _rd(os.path.join(self.cfg, "model-settings.yml")))

    def test_lane_check_falls_back_to_a_served_lane(self):
        with Stub() as stub:
            out = self.wire(stub, "--model", "super-flash")
            self.assertIn("falling back", out)
            self.assertIn("--model openai/heavy", _rd(self.wrap))
        with Stub(catalogue=["only-lane"]) as stub:
            self.wire(stub, "--model", "super-flash")
            self.assertIn("--model openai/only-lane", _rd(self.wrap))

    def test_served_lane_is_kept(self):
        with Stub() as stub:
            self.wire(stub, "--model", "flash")
            self.assertIn("--model openai/flash", _rd(self.wrap))

    def test_rewire_is_idempotent(self):
        with Stub() as stub:
            self.wire(stub)
            first = {p: _rd(p) for p in _all_files(self.cfg)}
            wrap1 = _rd(self.wrap)
            self.wire(stub)
            self.assertEqual({p: _rd(p) for p in _all_files(self.cfg)}, first)
            self.assertEqual(_rd(self.wrap), wrap1)

    def test_unwire_restores_user_config_and_removes_ferry_files(self):
        with Stub() as stub:
            self.wire(stub)
            self.wire(stub)
        rc, out = run_ferry(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_rd(self.conf), SEED_CONF)
        self.assertEqual(_rd(self.msettings), SEED_SETTINGS)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "model-settings.yml")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "model-metadata.json")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "aider.json")))

    def test_refuses_a_foreign_file_at_the_wrapper_path(self):
        os.makedirs(os.path.dirname(self.wrap))
        with open(self.wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.wrap), "#!/bin/sh\necho mine\n")


def _find_aider():
    env = os.environ.get("FERRY_E2E_BIN_AIDER")
    if env and os.access(env, os.X_OK):
        return env
    return shutil.which("aider")


class E2E(unittest.TestCase):
    """Real aider -> aider-ferry wrapper -> stub. Zero tokens."""

    def setUp(self):
        self.bin = _find_aider()
        if not self.bin:
            self.skipTest("aider binary not found (PATH or FERRY_E2E_BIN_AIDER)")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.work = os.path.join(self.tmp.name, "work")
        os.makedirs(self.home)
        os.makedirs(self.work)
        # a hostile user config: points elsewhere; ferry's argv must beat it
        with open(os.path.join(self.home, ".aider.conf.yml"), "w") as f:
            f.write("openai-api-base: http://127.0.0.1:9/v1\nmodel: gpt-4o\n")
        with open(os.path.join(self.home, ".env"), "w") as f:
            f.write("OPENAI_API_BASE=http://127.0.0.1:9/v1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_aider(self, stub, *extra, key=KEY):
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", str(stub.port),
                            "--key", key)
        self.assertEqual(rc, 0, out)
        wrap = os.path.join(self.home, ".local", "bin", "aider-ferry")
        env = clean_env(self.home,
                        PATH=os.path.dirname(self.bin) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                        LITELLM_LOCAL_MODEL_COST_MAP="True",
                        LITELLM_LOG="ERROR", NO_COLOR="1", TERM="dumb")
        # git, analytics, update check, release notes, url scraping all off:
        # the only network peer is the stub.
        args = [wrap, "--no-git", "--no-analytics", "--no-check-update",
                "--no-show-release-notes", "--no-detect-urls", "--no-pretty",
                "--no-fancy-input", "--no-auto-commits", "--yes-always",
                "--message", "say hi", *extra]
        return subprocess.run(args, cwd=self.work, capture_output=True, text=True,
                              timeout=240, env=env, stdin=subprocess.DEVNULL)

    def chat_posts(self, stub):
        return [s for s in stub.seen if s[0] == "POST"]

    def check(self, stub, p):
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("FERRY-STUB-REPLY", out)
        posts = self.chat_posts(stub)
        self.assertTrue(posts, f"stub saw no POST; saw {[(s[0], s[1]) for s in stub.seen]}\n{out}")
        method, path, hdr, body = posts[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(hdr.get("authorization"), "Bearer " + KEY)
        self.assertEqual(hdr.get("x-ferry-client"), "host")
        self.assertEqual(json.loads(body)["model"], "heavy")
        # nothing else reached the stub except /v1/models-style probes
        for m, pth, _, _ in stub.seen:
            self.assertTrue(pth.startswith("/v1/"), pth)
        self.assertNotIn(KEY, out)

    def test_streaming_chat_completions(self):
        with Stub() as stub:
            self.check(stub, self.run_aider(stub))

    def test_non_streaming_chat_completions(self):
        with Stub() as stub:
            p = self.run_aider(stub, "--no-stream")
            self.check(stub, p)
            self.assertFalse(json.loads(self.chat_posts(stub)[0][3]).get("stream"))

    def test_other_lane_by_flag(self):
        with Stub() as stub:
            p = self.run_aider(stub, "--model", "openai/flash")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            posts = self.chat_posts(stub)
            self.assertEqual(json.loads(posts[0][3])["model"], "flash")
            self.assertEqual(posts[0][2].get("x-ferry-client"), "host")


if __name__ == "__main__":
    unittest.main()
