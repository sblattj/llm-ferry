#!/usr/bin/env python3
"""Stdlib unittest for `ferry codewhale` (issue #3): wire Codewhale
(Hmbown/Codewhale) to ferry through a `codewhale-ferry` wrapper and one
ferry-owned config.toml (custom OpenAI-compatible provider "ferry").

Run:  python3 lib/ferry-codewhale.test.py

Wiring tests source lib/ferry-core.zsh + ferry-agents.zsh + ferry-codewhale.zsh
in a zsh subprocess against a throwaway HOME and a stub /v1/models on an
ephemeral port. The e2e runs the REAL codewhale (PATH, or FERRY_E2E_BIN_CODEWHALE;
its codewhale-tui sibling must sit next to it) non-interactively through the
wrapper against a stub that answers /v1/chat/completions. Nothing here touches
port 8090 or the real home directory; the e2e is skipped when codewhale is absent.
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
KEY = "sk-codewhale-test-key-0123456789"
SEED_CONF = ('# my own codewhale config\nprovider = "openai"\n'
             'approval_policy = "never"\n\n[providers.openai]\napi_key = "user-own-key"\n')


def _rd(path):
    with open(path) as f:
        return f.read()


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("XDG_", "CODEWHALE_", "DEEPSEEK_", "OPENAI_"))
                   or k in ("FERRY_FLEET", "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY",
                            "CLIENT_NAME", "CLIENT_MODE", "FERRY_KEY"))}
    env["HOME"] = home
    env.update(extra)
    return env


def run_ferry(home, *args, env_extra=None):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-codewhale.zsh\n"
              'cmd_codewhale "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-codewhale", *args],
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
        os.makedirs(os.path.join(self.home, ".codewhale"))
        self.conf = os.path.join(self.home, ".codewhale", "config.toml")
        with open(self.conf, "w") as f:
            f.write(SEED_CONF)
        self.cfg = os.path.join(self.home, ".config", "ferry", "codewhale", "config.toml")
        self.wrap = os.path.join(self.home, ".local", "bin", "codewhale-ferry")

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
        self.assertIn("ferry codewhale", out)
        self.assertIn("codewhale-ferry", out)

    def test_wire_files_headers_and_user_config_untouched(self):
        with Stub() as stub:
            out = self.wire(stub)
            base = f"http://127.0.0.1:{stub.port}/v1"
            self.assertNotIn(KEY, out, "key leaked to stdout")
            self.assertEqual(_rd(self.conf), SEED_CONF, "user config changed")
            toml = _rd(self.cfg)
            self.assertIn("ferry-managed: codewhale", toml)
            self.assertIn('provider = "ferry"', toml)
            self.assertIn('kind = "openai-compatible"', toml)
            self.assertIn(f'base_url = "{base}"', toml)
            self.assertIn('api_key_env = "FERRY_KEY"', toml)
            self.assertIn('model = "heavy"', toml)
            self.assertIn("allow_insecure_http = true", toml)
            self.assertIn('"X-Ferry-Client" = "host"', toml)
            self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
            w = _rd(self.wrap)
            self.assertIn("ferry-managed: codewhale", w)
            self.assertIn("export CODEWHALE_CONFIG_PATH=" + self.cfg, w)
            self.assertIn('export FERRY_KEY="$FERRY_KEY"', w)
            self.assertIn("unset CODEWHALE_PROVIDER", w)
            self.assertIn("exec codewhale", w)
            self.assertNotIn(KEY, w, "key baked into wrapper")
            self.assertTrue(os.access(self.wrap, os.X_OK))
            keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
            self.assertEqual(_rd(keyfile), KEY)
            self.assertEqual(stat.S_IMODE(os.stat(keyfile).st_mode), 0o600)
            for p in _all_files(self.home):
                if p == keyfile:
                    continue
                self.assertNotIn(KEY, _rd(p), f"key found in {p}")
            rec = json.loads(_rd(os.path.join(self.home, ".config", "ferry", "codewhale.json")))
            self.assertEqual(rec["model"], "heavy")
            self.assertEqual(rec["edited"], [])
            self.assertEqual(len(rec["created"]), 2)

    def test_fleet_header_when_set(self):
        with Stub() as stub:
            rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", str(stub.port),
                                "--key", KEY, env_extra={"FERRY_FLEET": "pool-a"})
            self.assertEqual(rc, 0, out)
            self.assertIn('"X-Ferry-Fleet" = "pool-a"', _rd(self.cfg))

    def test_lane_check_falls_back_to_a_served_lane(self):
        with Stub() as stub:
            out = self.wire(stub, "--model", "super-flash")
            self.assertIn("falling back", out)
            self.assertIn('model = "heavy"', _rd(self.cfg))
        with Stub(catalogue=["only-lane"]) as stub:
            self.wire(stub, "--model", "super-flash")
            self.assertIn('model = "only-lane"', _rd(self.cfg))

    def test_served_lane_is_kept(self):
        with Stub() as stub:
            self.wire(stub, "--model", "flash")
            self.assertIn('default_text_model = "flash"', _rd(self.cfg))

    def test_rewire_is_idempotent(self):
        with Stub() as stub:
            self.wire(stub)
            cfg1, wrap1 = _rd(self.cfg), _rd(self.wrap)
            self.wire(stub)
            self.assertEqual(_rd(self.cfg), cfg1)
            self.assertEqual(_rd(self.wrap), wrap1)

    def test_unwire_restores_user_config_and_removes_ferry_files(self):
        with Stub() as stub:
            self.wire(stub)
            self.wire(stub)
        rc, out = run_ferry(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_rd(self.conf), SEED_CONF)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(self.cfg))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "codewhale.json")))

    def test_refuses_a_foreign_file_at_the_wrapper_path(self):
        os.makedirs(os.path.dirname(self.wrap))
        with open(self.wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.wrap), "#!/bin/sh\necho mine\n")


def _find_codewhale():
    env = os.environ.get("FERRY_E2E_BIN_CODEWHALE")
    if env and os.access(env, os.X_OK):
        return env
    return shutil.which("codewhale")


class E2E(unittest.TestCase):
    """Real codewhale -> codewhale-ferry wrapper -> stub. Zero tokens."""

    def setUp(self):
        self.bin = _find_codewhale()
        if not self.bin:
            self.skipTest("codewhale binary not found (PATH or FERRY_E2E_BIN_CODEWHALE)")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.work = os.path.join(self.tmp.name, "work")
        os.makedirs(os.path.join(self.home, ".codewhale"))
        os.makedirs(self.work)
        # a hostile user config pointing elsewhere: the wrapper must not read it
        with open(os.path.join(self.home, ".codewhale", "config.toml"), "w") as f:
            f.write('provider = "openai"\n[providers.openai]\n'
                    'base_url = "http://127.0.0.1:9/v1"\napi_key = "user-own-key"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_exec_through_wrapper_hits_the_stub_with_key_and_header(self):
        with Stub() as stub:
            rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", str(stub.port),
                                "--key", KEY)
            self.assertEqual(rc, 0, out)
            wrap = os.path.join(self.home, ".local", "bin", "codewhale-ferry")
            # a shell-level provider override must be neutralised by the wrapper
            env = clean_env(self.home,
                            PATH=os.path.dirname(os.path.realpath(self.bin)) + ":/usr/bin:/bin",
                            CODEWHALE_PROVIDER="openai", OPENAI_BASE_URL="http://127.0.0.1:9/v1",
                            NO_COLOR="1", TERM="dumb")
            p = subprocess.run([wrap, "exec", "say hi"], cwd=self.work, capture_output=True,
                               text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
            o = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, o)
            self.assertIn("FERRY-STUB-REPLY", o)
            posts = [s for s in stub.seen if s[0] == "POST"]
            self.assertTrue(posts, f"stub saw no POST: {[(s[0], s[1]) for s in stub.seen]}\n{o}")
            _, path, hdr, body = posts[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(hdr.get("authorization"), "Bearer " + KEY)
            self.assertEqual(hdr.get("x-ferry-client"), "host")
            req = json.loads(body)
            self.assertEqual(req["model"], "heavy")
            self.assertTrue(req.get("stream"), "codewhale is expected to stream")
            self.assertNotIn("user-own-key", json.dumps(stub.seen))
            self.assertNotIn(KEY, o)
            # the user's own config was neither read for routing nor modified
            self.assertIn("user-own-key", _rd(os.path.join(self.home, ".codewhale", "config.toml")))


if __name__ == "__main__":
    unittest.main()
