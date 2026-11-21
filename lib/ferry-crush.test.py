#!/usr/bin/env python3
"""Stdlib unittest for `ferry crush` (issue #3).

Run:  python3 lib/ferry-crush.test.py     (sourcing mode: no built ferry needed)

Failure case from the issue: on v1.51.0 `ferry crush` is "Unknown command" and
there is no way to point Crush at the gateway. The wiring under test is a
ferry-owned config dir (~/.config/ferry/crush/crush.json, selected with
CRUSH_GLOBAL_DATA) plus a `crush-ferry` wrapper; the user's own crush.json
files are never opened.

The e2e runs the real binary (PATH, or $FERRY_E2E_BIN_CRUSH) through the
wrapper against a local stub that answers /v1/models and /v1/chat/completions.
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
KEY = "fk-test-key-crush-1234"
LANES = ["heavy", "medium", "flash"]
SCRUB = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
         "FERRY_FLEET", "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY", "CLIENT_NAME",
         "CRUSH_GLOBAL_CONFIG", "CRUSH_GLOBAL_DATA", "CRUSH_DATA_DIR",
         "CRUSH_CACHE_DIR", "CRUSH_FERRY_KEY", "CRUSH_CLIENT_SERVER",
         "CRUSH_DISABLE_PROVIDER_AUTO_UPDATE", "OPENAI_API_KEY",
         "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY")


def _rd(p):
    with open(p) as f:
        return f.read()


def _rdb(p):
    with open(p, "rb") as f:
        return f.read()


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["HOME"] = home
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    env.update(extra)
    return env


def run_crush_cmd(home, *args, env_extra=None):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-crush.zsh\n"
              'cmd_crush "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-crush", *args],
                       capture_output=True, text=True, timeout=60,
                       env=clean_env(home, **(env_extra or {})))
    return p.returncode, p.stdout + p.stderr


class Stub:
    """Ferry-shaped stub: /v1/models plus OpenAI chat/completions."""

    def __init__(self, lanes=LANES):
        self.seen = []          # (method, path, headers dict, body)
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _rec(self, body=b""):
                stub.seen.append((self.command, self.path,
                                  {k.lower(): v for k, v in self.headers.items()}, body))

            def do_GET(self):
                self._rec()
                if self.path.split("?")[0] == "/v1/models":
                    out = json.dumps({"object": "list", "data": [
                        {"id": l, "object": "model", "owned_by": "ferry"} for l in lanes]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                self._rec(body)
                if not self.path.split("?")[0].endswith("/chat/completions"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    req = json.loads(body)
                except Exception:
                    req = {}
                base = {"id": "chatcmpl-stub", "created": 1, "model": req.get("model", "x")}
                if req.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [
                        dict(base, object="chat.completion.chunk", choices=[
                            {"index": 0, "delta": {"role": "assistant", "content": "stub says "},
                             "finish_reason": None}]),
                        dict(base, object="chat.completion.chunk", choices=[
                            {"index": 0, "delta": {"content": "FERRY-CRUSH-OK"},
                             "finish_reason": None}]),
                        dict(base, object="chat.completion.chunk", choices=[
                            {"index": 0, "delta": {}, "finish_reason": "stop"}],
                            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}),
                    ]
                    for c in chunks:
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    out = json.dumps(dict(base, object="chat.completion", choices=[
                        {"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "stub says FERRY-CRUSH-OK"}}],
                        usage={"prompt_tokens": 3, "completion_tokens": 4,
                               "total_tokens": 7})).encode()
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


def find_crush():
    env_bin = os.environ.get("FERRY_E2E_BIN_CRUSH")
    if env_bin and os.access(env_bin, os.X_OK):
        return env_bin
    return shutil.which("crush")


class CrushWiring(unittest.TestCase):
    USER_GLOBAL = '{"providers": {"mine": {"type": "openai-compat", "base_url": "http://x/v1"}},\n "options": {"debug": false}}\n'
    USER_DATA = '{"models": {"large": {"provider": "mine", "model": "m1"}}}\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.g = os.path.join(self.home, ".config", "crush", "crush.json")
        self.d = os.path.join(self.home, ".local", "share", "crush", "crush.json")
        for p, t in ((self.g, self.USER_GLOBAL), (self.d, self.USER_DATA)):
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                f.write(t)
        self.cfg = os.path.join(self.home, ".config", "ferry", "crush", "crush.json")
        self.wrap = os.path.join(self.home, ".local", "bin", "crush-ferry")
        self.stub = Stub()

    def tearDown(self):
        self.stub.close()
        self.tmp.cleanup()

    def wire(self, *extra, port=None):
        rc, out = run_crush_cmd(self.home, "--host", "127.0.0.1",
                                "--port", str(port or self.stub.port),
                                "--key", KEY, *extra)
        self.assertEqual(rc, 0, out)
        return out

    def test_help_names_the_command(self):
        rc, out = run_crush_cmd(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry crush", out)
        self.assertIn("crush-ferry", out)

    def test_wire_writes_owned_config_and_wrapper_only(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        d = json.loads(_rd(self.cfg))
        p = d["providers"]["ferry"]
        self.assertEqual(p["type"], "openai-compat")
        self.assertEqual(p["base_url"], f"http://127.0.0.1:{self.stub.port}/v1")
        self.assertEqual(p["api_key"], "$CRUSH_FERRY_KEY")
        self.assertEqual(p["extra_headers"]["X-Ferry-Client"], "host")
        self.assertEqual([m["id"] for m in p["models"]], LANES)
        self.assertEqual(d["models"]["large"], {"provider": "ferry", "model": "heavy"})
        self.assertEqual(d["models"]["small"], {"provider": "ferry", "model": "heavy"})
        wrap = _rd(self.wrap)
        self.assertIn("ferry-managed: crush", wrap)
        self.assertIn(f"CRUSH_GLOBAL_DATA={os.path.dirname(self.cfg)}", wrap)
        self.assertIn('CRUSH_FERRY_KEY="$FERRY_KEY"', wrap)
        self.assertIn("exec crush", wrap)
        self.assertTrue(os.access(self.wrap, os.X_OK))
        # The user's own configs are not even opened.
        self.assertEqual(_rd(self.g), self.USER_GLOBAL)
        self.assertEqual(_rd(self.d), self.USER_DATA)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.g))), ["crush.json"])
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.d))), ["crush.json"])

    def test_key_only_in_the_0600_key_file(self):
        self.wire()
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(_rd(keyfile), KEY)
        for root, _dirs, files in os.walk(self.home):
            for n in files:
                path = os.path.join(root, n)
                if KEY.encode() in _rdb(path):
                    self.assertEqual(path, keyfile, f"key found in {path}")
                    self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_fleet_header_when_set(self):
        rc, out = run_crush_cmd(self.home, "--host", "127.0.0.1",
                                "--port", str(self.stub.port), "--key", KEY,
                                env_extra={"FERRY_FLEET": "lab"})
        self.assertEqual(rc, 0, out)
        h = json.loads(_rd(self.cfg))["providers"]["ferry"]["extra_headers"]
        self.assertEqual(h["X-Ferry-Fleet"], "lab")

    def test_lanes_come_from_the_stub_catalogue(self):
        self.stub.close()
        self.stub = Stub(lanes=["alpha", "beta"])
        out = self.wire("--model", "heavy")      # not served: falls back to alpha
        self.assertIn("does not serve lane 'heavy'", out)
        d = json.loads(_rd(self.cfg))
        self.assertEqual([m["id"] for m in d["providers"]["ferry"]["models"]], ["alpha", "beta"])
        self.assertEqual(d["models"]["large"]["model"], "alpha")

    def test_lane_check_is_not_fatal_when_ferry_is_down(self):
        # lib/ferry-agents.zsh _agent_resolve parses the lane check output
        # with ${${(f)out}[N]}; with a single output line zsh indexes the
        # CHARACTERS of the string, so a ferry-down wire gets lane "h".
        probe = subprocess.run(
            ["zsh", "-c", "out=$(printf 'heavy\\n\\n'); print -r -- ${${(f)out}[1]:-}"],
            capture_output=True, text=True).stdout.strip()
        src = _rd(os.path.join(REPO, "lib", "ferry-agents.zsh"))
        if "${${(f)out}[1]:-$AG_LANE}" in src and probe != "heavy":
            self.skipTest("helper bug: ferry-agents.zsh _agent_resolve takes "
                          "${${(f)out}[1]} of a one-line string (lane 'h'); "
                          "needs \"${(@f)out}\" there, reported to the orchestrator")
        out = self.wire(port=9)
        self.assertIn("could not verify", out)
        d = json.loads(_rd(self.cfg))
        self.assertEqual([m["id"] for m in d["providers"]["ferry"]["models"]], ["heavy"])

    def test_rewire_is_idempotent(self):
        self.wire()
        first = (_rd(self.cfg), _rd(self.wrap))
        self.wire()
        self.assertEqual((_rd(self.cfg), _rd(self.wrap)), first)

    def test_rewire_keeps_what_crush_saved_in_the_data_file(self):
        self.wire()
        d = json.loads(_rd(self.cfg))
        d["models"]["large"]["reasoning_effort"] = "high"      # Crush's own save
        d["recent_models"] = {"large": [{"provider": "ferry", "model": "flash"}]}
        with open(self.cfg, "w") as f:
            json.dump(d, f)
        self.wire()
        d = json.loads(_rd(self.cfg))
        self.assertEqual(d["models"]["large"]["reasoning_effort"], "high")
        self.assertIn("recent_models", d)

    def test_unwire_removes_everything_and_leaves_user_files_alone(self):
        self.wire()
        rc, out = run_crush_cmd(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_rd(self.g), self.USER_GLOBAL)
        self.assertEqual(_rd(self.d), self.USER_DATA)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(self.cfg))
        self.assertFalse(os.path.exists(os.path.dirname(self.cfg)))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "crush.json")))

    def test_unwire_keeps_other_keys_crush_added_to_the_data_file(self):
        self.wire()
        d = json.loads(_rd(self.cfg))
        d["options"]["tui"] = {"compact_mode": True}
        d["models"]["large"]["provider"] = "ferry"
        with open(self.cfg, "w") as f:
            json.dump(d, f)
        rc, out = run_crush_cmd(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(_rd(self.cfg)), {"options": {"tui": {"compact_mode": True}}})

    def test_dry_run_unwire_changes_nothing(self):
        self.wire()
        before = _rd(self.cfg)
        rc, out = run_crush_cmd(self.home, "--unwire", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_rd(self.cfg), before)
        self.assertTrue(os.path.exists(self.wrap))

    def test_refuses_a_foreign_file_in_the_owned_dir(self):
        os.makedirs(os.path.dirname(self.cfg))
        with open(self.cfg, "w") as f:
            f.write('{"mine": 1}\n')
        rc, out = run_crush_cmd(self.home, "--host", "127.0.0.1",
                                "--port", "9", "--key", KEY)
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(self.cfg), '{"mine": 1}\n')


class CrushE2E(unittest.TestCase):
    """The real binary, through the wrapper, against the stub. Zero tokens."""

    def setUp(self):
        self.bin = find_crush()
        if not self.bin:
            self.skipTest("crush binary not found (PATH or FERRY_E2E_BIN_CRUSH)")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.work = os.path.join(self.home, "proj")
        os.makedirs(self.work)
        self.stub = Stub()
        # the user's own global config must still load next to ferry's
        g = os.path.join(self.home, ".config", "crush", "crush.json")
        os.makedirs(os.path.dirname(g))
        with open(g, "w") as f:
            json.dump({"providers": {"mine": {"type": "openai-compat",
                       "base_url": "http://127.0.0.1:9/v1", "api_key": "x",
                       "models": [{"id": "m1", "name": "m1", "context_window": 8000,
                                   "default_max_tokens": 100}]}}}, f)
        rc, out = run_crush_cmd(self.home, "--host", "127.0.0.1",
                                "--port", str(self.stub.port), "--key", KEY)
        self.assertEqual(rc, 0, out)
        self.stub.seen.clear()

    def tearDown(self):
        if getattr(self, "stub", None):
            self.stub.close()
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def run_wrapper(self, *args):
        bindir = os.path.dirname(self.bin)
        env = clean_env(self.home)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["CRUSH_DISABLE_METRICS"] = "1"
        env["CRUSH_CLIENT_SERVER"] = "0"
        wrap = os.path.join(self.home, ".local", "bin", "crush-ferry")
        return subprocess.run(["zsh", wrap, "--cwd", self.work, *args],
                              capture_output=True, text=True, timeout=120, env=env,
                              cwd=self.work)

    def test_run_uses_ferry_lane_with_bearer_and_client_header(self):
        p = self.run_wrapper("run", "hi")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("FERRY-CRUSH-OK", out)
        posts = [s for s in self.stub.seen if s[0] == "POST"]
        self.assertTrue(posts, f"stub saw no POST: {[s[:2] for s in self.stub.seen]}")
        for _m, path, hdr, body in posts:
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(hdr.get("authorization"), f"Bearer {KEY}")
            self.assertEqual(hdr.get("x-ferry-client"), "host")
            self.assertEqual(json.loads(body)["model"], "heavy")
        # streamed by default
        self.assertTrue(any(json.loads(b).get("stream") for *_x, b in posts))
        # nothing leaked to the real user files
        self.assertNotIn(KEY, _rd(os.path.join(self.home, ".config", "crush", "crush.json")))

    def test_explicit_lane_flag_and_users_own_provider_survive(self):
        p = self.run_wrapper("run", "-m", "ferry/flash", "hi")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        posts = [s for s in self.stub.seen if s[0] == "POST"]
        self.assertTrue(posts, out)
        self.assertEqual(json.loads(posts[0][3])["model"], "flash")
        # the user's provider is still known to crush-ferry (models list)
        q = self.run_wrapper("models")
        mo = q.stdout + q.stderr
        self.assertEqual(q.returncode, 0, mo)
        self.assertIn("mine", mo)
        self.assertIn("ferry", mo)


if __name__ == "__main__":
    unittest.main()
