#!/usr/bin/env python3
"""Stdlib unittest for `ferry omp` (issue #3): oh-my-pi (can1357/oh-my-pi).

Run:  python3 lib/ferry-omp.test.py     (sourcing mode: the modules are sourced
straight from lib/, the built `ferry` monolith need not know the command yet)

omp reads models.yml (YAML), not Pi's models.json, and has no flag to point at
another one. So `ferry omp` edits NO file of the user's: it writes a ferry-owned
extension (~/.config/ferry/omp/ferry-provider.ts) that registers a `ferry`
provider and a wrapper ~/.local/bin/omp-ferry (`omp -e <ext> --model
ferry/<lane>`). The key is never in a config file: the extension's apiKey is
the env-var NAME FERRY_KEY, exported by the wrapper from the 0600 key file.

The e2e runs the real `omp` (PATH, ~/.bun/bin, or $FERRY_E2E_BIN_OMP)
non-interactively through the wrapper, HOME = a throwaway dir, against a stub.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "fk-test-0123456789abcdef"
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

def _rd(path):
    with open(path) as f:
        return f.read()


def clean_env(home, extra=None, path=BASE_PATH):
    """An allowlisted environment: nothing of the real user's leaks in."""
    env = {"PATH": path, "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8"}
    env.update(extra or {})
    return env


def run_ferry(home, *args, extra_env=None):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-omp.zsh\n"
              'cmd_omp "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-omp", *args],
                       capture_output=True, text=True, timeout=60,
                       env=clean_env(home, extra_env))
    return p.returncode, p.stdout + p.stderr


class Stub:
    """A tiny ferry: GET /v1/models and the OpenAI chat API, with a log."""

    def __init__(self, lanes=("heavy", "flash")):
        self.lanes = list(lanes)
        self.seen = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _log(self, body=b""):
                stub.seen.append({"method": self.command, "path": self.path,
                                  "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "body": body})

            def do_GET(self):
                self._log()
                if self.path.startswith("/v1/models"):
                    data = json.dumps({"object": "list", "data": [
                        {"id": l, "object": "model"} for l in stub.lanes]}).encode()
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
                raw = self.rfile.read(n)
                self._log(raw)
                try:
                    req = json.loads(raw or b"{}")
                except ValueError:
                    req = {}
                model = req.get("model", "m")
                if not self.path.startswith("/v1/chat/completions"):
                    self.send_response(404)
                    self.end_headers()
                    return
                usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
                if req.get("stream"):
                    def ev(o):
                        return b"data: " + json.dumps(o).encode() + b"\n\n"
                    base = {"id": "c1", "object": "chat.completion.chunk",
                            "created": 1, "model": model}
                    out = (ev({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
                           + ev({**base, "choices": [{"index": 0, "delta": {"content": "FERRY_STUB_OK"}, "finish_reason": None}]})
                           + ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage})
                           + b"data: [DONE]\n\n")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    out = json.dumps({"id": "c1", "object": "chat.completion", "created": 1,
                                      "model": model, "usage": usage,
                                      "choices": [{"index": 0, "finish_reason": "stop",
                                                   "message": {"role": "assistant",
                                                               "content": "FERRY_STUB_OK"}}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def tree(home):
    out = {}
    for d, _, fs in os.walk(home):
        for f in fs:
            p = os.path.join(d, f)
            if os.path.isfile(p) and not os.path.islink(p) and "/Library/Caches/" not in p:
                out[p] = p
    return out


def find_omp():
    cands = [os.environ.get("FERRY_E2E_BIN_OMP"), shutil.which("omp"),
             os.path.expanduser("~/.bun/bin/omp")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


USER_FILES = {
    "models.yml": "# my models\nproviders:\n  local:\n    baseUrl: http://localhost:11434/v1\n    api: openai-completions\n",
    "config.yml": "theme: dark\n",
    "models.json": '{"providers": {}}\n',
}


class OmpWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.agent = os.path.join(self.home, ".omp", "agent")
        os.makedirs(self.agent)
        for n, t in USER_FILES.items():
            with open(os.path.join(self.agent, n), "w") as f:
                f.write(t)
        self.ext = os.path.join(self.home, ".config", "ferry", "omp", "ferry-provider.ts")
        self.wrap = os.path.join(self.home, ".local", "bin", "omp-ferry")
        self.rec = os.path.join(self.home, ".config", "ferry", "omp.json")
        self.keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        os.makedirs(os.path.dirname(self.keyfile))
        with open(os.path.join(self.home, ".config", "ferry", "client.json"), "w") as f:
            json.dump({"host": "127.0.0.1", "port": "1", "name": "unit-box"}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def user_snapshot(self):
        return {n: _rd(os.path.join(self.agent, n)) for n in USER_FILES}

    def wire(self, stub, *extra):
        return run_ferry(self.home, "--host", "127.0.0.1", "--port", str(stub.port),
                         "--key", KEY, *extra)

    def ext_cfg(self):
        m = re.search(r'registerProvider\("ferry", (\{.*\})\);\n}\n$', _rd(self.ext), re.S)
        self.assertTrue(m, _rd(self.ext))
        return json.loads(m.group(1))

    def test_help_names_the_command(self):
        rc, out = run_ferry(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry omp", out)

    def test_wire_writes_only_ferry_files_and_leaves_user_config_byte_exact(self):
        before = self.user_snapshot()
        before_tree = set(tree(self.home))
        with Stub() as stub:
            rc, out = self.wire(stub)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.user_snapshot(), before)
        new = {p for p in set(tree(self.home)) - before_tree if not p.endswith(".bak")}
        self.assertEqual(new, {self.ext, self.wrap, self.rec, self.keyfile})
        self.assertTrue(os.access(self.wrap, os.X_OK))

    def test_provider_shape_headers_and_every_lane(self):
        with Stub(("heavy", "medium", "flash")) as stub:
            rc, out = self.wire(stub)
            cfg = self.ext_cfg()
        self.assertEqual(rc, 0, out)
        self.assertEqual(cfg["baseUrl"], f"http://127.0.0.1:{stub.port}/v1")
        self.assertEqual(cfg["api"], "openai-completions")
        self.assertEqual(cfg["headers"].get("X-Ferry-Client"), "unit-box")
        self.assertEqual([m["id"] for m in cfg["models"]], ["heavy", "medium", "flash"])
        self.assertIn("ferry/heavy", _rd(self.wrap))

    def test_key_only_in_the_0600_keyfile(self):
        with Stub() as stub:
            rc, out = self.wire(stub)
        self.assertEqual(rc, 0)
        self.assertNotIn(KEY, out)
        self.assertEqual(_rd(self.keyfile).strip(), KEY)
        self.assertEqual(stat.S_IMODE(os.stat(self.keyfile).st_mode), 0o600)
        for p in tree(self.home):
            if p == self.keyfile:
                continue
            with open(p, "rb") as f:
                self.assertNotIn(KEY.encode(), f.read(), p)
        self.assertEqual(self.ext_cfg()["apiKey"], "FERRY_KEY")

    def test_rewire_is_idempotent_and_follows_a_new_port(self):
        with Stub() as a, Stub() as b:
            self.assertEqual(self.wire(a)[0], 0)
            self.assertEqual(self.wire(b)[0], 0)
            self.assertEqual(self.ext_cfg()["baseUrl"], f"http://127.0.0.1:{b.port}/v1")
        self.assertEqual(_rd(self.ext).count("registerProvider("), 1)

    def test_unwire_removes_ferry_files_and_keeps_user_config(self):
        before = self.user_snapshot()
        with Stub() as stub:
            self.assertEqual(self.wire(stub)[0], 0)
            rc, out = run_ferry(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.ext))
        self.assertFalse(os.path.exists(self.wrap))
        self.assertEqual(self.user_snapshot(), before)

    def test_dry_run_changes_nothing(self):
        before = set(tree(self.home))
        with Stub() as stub:
            self.assertEqual(self.wire(stub)[0], 0)
            snap = {p: _rd(p) for p in tree(self.home) if p not in before}
            rc, out = run_ferry(self.home, "--unwire", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual({p: _rd(p) for p in tree(self.home) if p not in before}, snap)

    def test_user_owned_extension_file_is_never_replaced(self):
        os.makedirs(os.path.dirname(self.ext))
        with open(self.ext, "w") as f:
            f.write("// mine\n")
        with Stub() as stub:
            rc, out = self.wire(stub)
        self.assertNotEqual(rc, 0)
        self.assertEqual(_rd(self.ext), "// mine\n")

    def test_lane_falls_back_to_a_served_lane(self):
        with Stub(("flash",)) as stub:
            rc, out = self.wire(stub)
            self.assertEqual(rc, 0, out)
            cfg = self.ext_cfg()
        self.assertIn("flash", [m["id"] for m in cfg["models"]])
        self.assertTrue(f"ferry/{cfg['models'][0]['id']}" in _rd(self.wrap))

    def test_unknown_option_is_refused(self):
        with Stub() as stub:
            rc, out = self.wire(stub, "--bogus")
        self.assertNotEqual(rc, 0)


class OmpEndToEnd(unittest.TestCase):
    """The real omp, through the omp-ferry wrapper, against a local stub."""

    def test_omp_ferry_talks_to_the_stub_with_key_and_client_header(self):
        omp = find_omp()
        if not omp:
            self.skipTest("omp binary not found (PATH, ~/.bun/bin or FERRY_E2E_BIN_OMP)")
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd, \
                Stub() as stub:
            dirs = [os.path.dirname(omp)]
            for t in ("bun", "node"):
                w = shutil.which(t)
                if w:
                    dirs.append(os.path.dirname(w))
            path = ":".join(dict.fromkeys(dirs + BASE_PATH.split(":")))
            os.makedirs(os.path.join(home, ".config", "ferry"))
            with open(os.path.join(home, ".config", "ferry", "client.json"), "w") as f:
                json.dump({"host": "127.0.0.1", "port": str(stub.port), "name": "e2e-box"}, f)
            script = (f"source {REPO}/lib/ferry-core.zsh\nsource {REPO}/lib/ferry-agents.zsh\n"
                      f"source {REPO}/lib/ferry-omp.zsh\ncmd_omp \"$@\"\n")
            p = subprocess.run(["zsh", "-c", script, "ferry-omp", "--host", "127.0.0.1",
                                "--port", str(stub.port), "--key", KEY],
                               capture_output=True, text=True, timeout=60,
                               env=clean_env(home, {}, path))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            stub.seen.clear()
            r = subprocess.run([os.path.join(home, ".local", "bin", "omp-ferry"),
                                "-p", "say hi", "--no-session"],
                               capture_output=True, text=True, timeout=120,
                               env=clean_env(home, {}, path), cwd=cwd,
                               stdin=subprocess.DEVNULL)
            posts = [s for s in stub.seen if s["method"] == "POST"]
            self.assertTrue(posts, f"the stub saw no POST. stdout={r.stdout!r} stderr={r.stderr!r}")
            req = posts[0]
            self.assertTrue(req["path"].startswith("/v1/chat/completions"), req["path"])
            self.assertEqual(req["headers"].get("authorization"), f"Bearer {KEY}")
            self.assertEqual(req["headers"].get("x-ferry-client"), "e2e-box")
            self.assertEqual(json.loads(req["body"])["model"], "heavy")
            self.assertIn("FERRY_STUB_OK", r.stdout, f"stderr={r.stderr!r}")
            self.assertTrue(all(s["path"].startswith("/v1/") for s in stub.seen))
            self.assertFalse(os.path.exists(os.path.join(home, ".omp", "agent", "models.yml")))


if __name__ == "__main__":
    unittest.main()
