#!/usr/bin/env python3
"""Stdlib unittest for `ferry kilo` (issue #3): Kilo Code, the `kilo` CLI.

Run:  python3 lib/ferry-kilo.test.py

Sourcing mode: the module is sourced next to ferry-agents.zsh, the built
`ferry` monolith is not involved. Everything runs under a throwaway HOME with
every XDG_*, FERRY_* and KILO_* variable stripped, against a stub gateway on an
ephemeral port (never the live 8090).

The e2e class runs the real `kilo` binary non-interactively (`kilo run`)
through the generated `kilo-ferry` wrapper, when one is found: env
FERRY_E2E_BIN_KILO (for a scratch install, point it at the native file
node_modules/@kilocode/cli-<platform>/bin/kilo), else `kilo` on PATH. It skips
with the reason otherwise.
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
KEY = "sk-test-kilo-0123456789"
SEEDED = {"theme": "mine", "provider": {"mine": {"name": "Mine", "options": {"baseURL": "http://x/v1"}}},
          "model": "mine/m1"}


def _rd(path):
    with open(path) as f:
        return f.read()


def _env(home, extra=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("XDG_", "FERRY_", "KILO_", "OPENCODE_"))
           and k not in ("CLIENT_MASTER_KEY", "CLIENT_NAME", "CLIENT_HOST", "CLIENT_PORT")}
    env["HOME"] = home
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    env.update(extra or {})
    return env


def run_ferry_kilo(home, *args, path=None):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-kilo.zsh\n"
              'cmd_kilo "$@"\n')
    env = _env(home, {"PATH": path} if path else None)
    p = subprocess.run(["zsh", "-c", script, "ferry-kilo", *args],
                       capture_output=True, text=True, timeout=90, env=env)
    return p.returncode, p.stdout + p.stderr


class Stub:
    """Serves GET /v1/models and answers chat/completions (stream and not)."""

    def __init__(self, lanes=("heavy", "flash")):
        self.lanes = list(lanes)
        self.seen = []
        stub = self

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
                stub.seen.append({"method": "GET", "path": self.path,
                                  "headers": {k.lower(): v for k, v in self.headers.items()}, "body": {}})
                data = [{"id": l, "object": "model"} for l in stub.lanes]
                self._send(200, json.dumps({"object": "list", "data": data}).encode())

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    body = {}
                stub.seen.append({"method": "POST", "path": self.path,
                                  "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [
                        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "ferry-pong"},
                                      "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
                    ]
                    for c in chunks:
                        c.update(id="c1", object="chat.completion.chunk", model="heavy")
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    msg = {"role": "assistant", "content": "ferry-pong"}
                    self._send(200, json.dumps({
                        "id": "c1", "object": "chat.completion", "model": "heavy",
                        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                        "usage": usage}).encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def all_files(root):
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            yield os.path.join(dp, f)


class KiloWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.kdir = os.path.join(self.home, ".config", "kilo")
        os.makedirs(self.kdir)
        self.user_cfg = os.path.join(self.kdir, "kilo.json")
        with open(self.user_cfg, "w") as f:
            json.dump(SEEDED, f, indent=1)
        self.seed_bytes = _rd(self.user_cfg)
        self.stub = Stub()
        self.addCleanup(self.stub.stop)
        self.addCleanup(self.tmp.cleanup)
        self.wrap = os.path.join(self.home, ".local", "bin", "kilo-ferry")
        self.owned = os.path.join(self.home, ".config", "ferry", "kilo", "kilo.json")

    def wire(self, *extra, port=None, expect=0):
        rc, out = run_ferry_kilo(self.home, "--host", "127.0.0.1",
                                 "--port", str(port or self.stub.port),
                                 "--key", KEY, *extra)
        self.assertEqual(rc, expect, out)
        return out

    def owned_cfg(self):
        text = _rd(self.owned)
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
        return json.loads(body)

    def test_help_names_the_command(self):
        rc, out = run_ferry_kilo(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry kilo", out)
        self.assertTrue(out.startswith("ferry kilo - "), out[:60])
        self.assertIn("--global", out)

    def test_wire_writes_wrapper_owned_config_and_leaves_user_config_alone(self):
        out = self.wire()
        self.assertNotIn(KEY, out, "key leaked to stdout")
        self.assertEqual(_rd(self.user_cfg), self.seed_bytes, "user kilo.json changed")
        body = _rd(self.wrap)
        self.assertIn("ferry-managed: kilo", body)
        self.assertIn(f'export KILO_CONFIG="{self.owned}"', body)
        self.assertIn('export FERRY_KEY="$FERRY_KEY"', body)
        self.assertIn("exec kilo", body)
        self.assertNotIn(KEY, body)
        self.assertTrue(os.access(self.wrap, os.X_OK))
        cfg = self.owned_cfg()
        prov = cfg["provider"]["ferry"]
        self.assertEqual(prov["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(prov["options"]["baseURL"], f"http://127.0.0.1:{self.stub.port}/v1")
        self.assertEqual(prov["options"]["apiKey"], "{env:FERRY_KEY}")
        self.assertEqual(prov["options"]["headers"]["X-Ferry-Client"], "host")
        self.assertNotIn("X-Ferry-Fleet", prov["options"]["headers"])
        self.assertEqual(sorted(prov["models"]), ["flash", "heavy"])
        self.assertEqual(cfg["model"], "ferry/heavy")

    def test_key_only_in_0600_files(self):
        self.wire()
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(_rd(keyfile), KEY)
        self.assertEqual(stat.S_IMODE(os.stat(keyfile).st_mode), 0o600)
        for p in all_files(self.home):
            with open(p, "rb") as fh:
                has = KEY.encode() in fh.read()
            if has:
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600,
                                 f"{p} holds the key and is not 0600")

    def test_fleet_header_when_set_at_wire_time(self):
        script = (f"source {REPO}/lib/ferry-core.zsh\nsource {REPO}/lib/ferry-agents.zsh\n"
                  f"source {REPO}/lib/ferry-kilo.zsh\ncmd_kilo \"$@\"\n")
        p = subprocess.run(["zsh", "-c", script, "x", "--host", "127.0.0.1", "--port",
                            str(self.stub.port), "--key", KEY],
                           capture_output=True, text=True, timeout=90,
                           env=_env(self.home, {"FERRY_FLEET": "international"}))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.owned_cfg()["provider"]["ferry"]["options"]["headers"]["X-Ferry-Fleet"],
                         "international")

    def test_lane_check_falls_back_to_a_served_lane(self):
        self.stub.lanes = ["flash", "medium"]
        out = self.wire()
        self.assertIn("does not serve lane 'heavy'", out)
        cfg = self.owned_cfg()
        self.assertEqual(cfg["model"], "ferry/flash")
        self.assertEqual(sorted(cfg["provider"]["ferry"]["models"]), ["flash", "medium"])
        self.assertTrue(any(s["path"] == "/v1/models" and
                            s["headers"].get("authorization") == f"Bearer {KEY}"
                            for s in self.stub.seen), self.stub.seen)

    def test_unreachable_host_is_not_fatal(self):
        out = self.wire(port=9)
        self.assertIn("could not verify the lane catalogue", out)
        self.assertEqual(self.owned_cfg()["model"], "ferry/heavy")

    def test_rewire_is_idempotent(self):
        self.wire()
        first = (_rd(self.owned), _rd(self.wrap), _rd(self.user_cfg))
        self.wire()
        self.assertEqual((_rd(self.owned), _rd(self.wrap), _rd(self.user_cfg)), first)

    def test_unwire_removes_ferry_files_and_keeps_user_config(self):
        self.wire()
        rc, out = run_ferry_kilo(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_rd(self.user_cfg), self.seed_bytes)
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(self.owned))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "kilo.json")))

    def test_refuses_to_replace_a_user_file_at_the_owned_path(self):
        os.makedirs(os.path.dirname(self.owned))
        with open(self.owned, "w") as f:
            f.write('{"mine": true}\n')
        rc, out = run_ferry_kilo(self.home, "--host", "127.0.0.1", "--port", "9", "--key", KEY)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(_rd(self.owned), '{"mine": true}\n')

    def test_global_adds_one_provider_member_and_unwire_restores_bytes(self):
        out = self.wire("--global")
        self.assertNotIn(KEY, out)
        d = json.loads(_rd(self.user_cfg))
        self.assertEqual(d["theme"], "mine")
        self.assertEqual(d["model"], "mine/m1", "--global must not move the user's default model")
        self.assertEqual(d["provider"]["mine"], SEEDED["provider"]["mine"])
        ferry = d["provider"]["ferry"]
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(ferry["options"]["apiKey"], "{file:" + keyfile + "}")
        self.assertEqual(ferry["options"]["headers"]["X-Ferry-Client"], "host")
        self.assertNotIn(KEY, _rd(self.user_cfg))
        self.wire("--global")   # idempotent
        rc, out = run_ferry_kilo(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(_rd(self.user_cfg)), SEEDED)
        self.assertFalse(os.path.exists(self.wrap))

    def test_global_leaves_a_jsonc_config_alone(self):
        os.remove(self.user_cfg)
        jsonc = os.path.join(self.kdir, "kilo.jsonc")
        with open(jsonc, "w") as f:
            f.write('// mine\n{"model": "mine/m1",}\n')
        before = _rd(jsonc)
        out = self.wire("--global")
        self.assertIn("kilo.jsonc exists", out)
        self.assertEqual(_rd(jsonc), before)
        self.assertFalse(os.path.exists(self.user_cfg))

    def test_unknown_flag_is_rejected(self):
        rc, out = run_ferry_kilo(self.home, "--bogus")
        self.assertNotEqual(rc, 0)
        self.assertIn("Unknown option for 'ferry kilo'", out)


def find_kilo():
    cand = os.environ.get("FERRY_E2E_BIN_KILO")
    if cand:
        return cand if os.access(cand, os.X_OK) else None
    return shutil.which("kilo")


class KiloEndToEnd(unittest.TestCase):
    """The real `kilo run` through kilo-ferry against the stub."""

    def setUp(self):
        self.bin = find_kilo()
        if not self.bin:
            self.skipTest("kilo binary not found (set FERRY_E2E_BIN_KILO or put `kilo` on PATH; "
                          "`bun add @kilocode/cli` in a scratch dir installs one)")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        # A directory holding only a `kilo` symlink: the wrapper execs `kilo`
        # by name, and the npm shim needs node while the native file does not.
        self.bindir = os.path.join(self.home, "realbin")
        os.makedirs(self.bindir)
        os.symlink(os.path.realpath(self.bin), os.path.join(self.bindir, "kilo"))
        self.stub = Stub()
        self.addCleanup(self.stub.stop)
        kdir = os.path.join(self.home, ".config", "kilo")
        os.makedirs(kdir)
        with open(os.path.join(kdir, "kilo.json"), "w") as f:
            json.dump({"model": "mine/m1"}, f)
        self.work = os.path.join(self.home, "work")
        os.makedirs(self.work)

    def test_kilo_run_hits_chat_completions_with_key_and_client_header(self):
        rc, out = run_ferry_kilo(self.home, "--host", "127.0.0.1",
                                 "--port", str(self.stub.port), "--key", KEY)
        self.assertEqual(rc, 0, out)
        wrap = os.path.join(self.home, ".local", "bin", "kilo-ferry")
        env = _env(self.home, {
            "PATH": f"{os.path.dirname(wrap)}:{self.bindir}:/usr/bin:/bin",
            "KILO_DISABLE_AUTOUPDATE": "1", "KILO_DISABLE_MODELS_FETCH": "1",
        })
        self.stub.seen.clear()
        p = subprocess.run([wrap, "run", "say pong"], cwd=self.work, env=env,
                           capture_output=True, text=True, timeout=120)
        text = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, text[-1500:])
        self.assertIn("ferry-pong", text, "the stub's canned reply never reached kilo's output")
        posts = [s for s in self.stub.seen if s["method"] == "POST"]
        self.assertTrue(posts, f"no POST reached the stub: {self.stub.seen}")
        chat = [s for s in posts if s["path"] == "/v1/chat/completions"]
        self.assertEqual(len(chat), len(posts), [s["path"] for s in posts])
        first = chat[0]
        self.assertEqual(first["headers"].get("authorization"), f"Bearer {KEY}")
        self.assertEqual(first["headers"].get("x-ferry-client"), "host")
        self.assertEqual(first["body"].get("model"), "heavy")
        self.assertTrue(first["body"].get("stream"), "kilo run is expected to stream")
        # the user's own default provider was not used
        self.assertNotIn("m1", json.dumps([s["body"].get("model") for s in posts]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
