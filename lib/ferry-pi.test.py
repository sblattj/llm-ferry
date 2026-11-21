#!/usr/bin/env python3
"""Stdlib unittest for `ferry pi` (issue #3): Pi (earendil-works/pi).

Run:  python3 lib/ferry-pi.test.py     (sourcing mode: the modules are sourced
straight from lib/, the built `ferry` monolith need not know the command yet)

Pi keeps custom providers in <agent dir>/models.json (JSONC: // comments and
trailing commas). `ferry pi` inserts ONE `ferry` provider as the first member
of "providers", records the literal text it inserted, and `--unwire` removes
exactly that text. The key is never in models.json: the provider's apiKey is
"!cat '<~/.config/ferry/agents.key>'".

The e2e runs the real `pi` (PATH, or $FERRY_E2E_BIN_PI) non-interactively
through the pi-ferry wrapper, HOME = a throwaway dir, against a local stub.
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

SEEDS = {
    "comments_trailing_comma": (
        '// my pi models\n'
        '{\n'
        '  "providers": {\n'
        '    // local ollama\n'
        '    "ollama": {\n'
        '      "baseUrl": "http://localhost:11434/v1",\n'
        '      "api": "openai-completions",\n'
        '      "apiKey": "ollama",\n'
        '      "models": [{"id": "llama3",},],\n'
        '    },\n'
        '  },\n'
        '}\n'),
    "no_trailing_comma": (
        '{\n'
        '  "providers": {\n'
        '    "mine": {"baseUrl": "http://x/v1", "api": "openai-completions", "models": []}\n'
        '  }\n'
        '}\n'),
    "no_providers_key": '{\n  "$schema": "https://example.invalid/s.json"\n}\n',
    "empty_object": '{}\n',
    "empty_providers": '{ "providers": {} }',
}


def _rd(path):
    with open(path) as f:
        return f.read()


def _strip_jsonc(t):
    """Same as pi's stripJsonComments (src/utils/json.ts:2-5)."""
    t = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*',
               lambda m: m.group(0) if m.group(0)[0] == '"' else "", t)
    return re.sub(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])',
                  lambda m: m.group(1) if m.group(1) is not None else m.group(0), t)


def jsonc(path):
    return json.loads(_strip_jsonc(_rd(path)))


def clean_env(home, extra=None, path=BASE_PATH):
    """An allowlisted environment: nothing of the real user's leaks in."""
    env = {"PATH": path, "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8"}
    env.update(extra or {})
    return env


def run_ferry(home, *args, extra_env=None):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"source {REPO}/lib/ferry-pi.zsh\n"
              'cmd_pi "$@"\n')
    p = subprocess.run(["zsh", "-c", script, "ferry-pi", *args],
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
            if os.path.isfile(p) and not os.path.islink(p):
                out[p] = p
    return out


class PiWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.agent = os.path.join(self.home, ".pi", "agent")
        self.models = os.path.join(self.agent, "models.json")
        self.wrap = os.path.join(self.home, ".local", "bin", "pi-ferry")
        self.rec = os.path.join(self.home, ".config", "ferry", "pi.json")
        self.keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, text):
        os.makedirs(self.agent, exist_ok=True)
        with open(self.models, "w") as f:
            f.write(text)

    def client_json(self, name):
        cj = os.path.join(self.home, ".config", "ferry", "client.json")
        os.makedirs(os.path.dirname(cj), exist_ok=True)
        with open(cj, "w") as f:
            json.dump({"host": "127.0.0.1", "port": "9", "name": name}, f)

    def wire(self, *extra, port="9", env=None):
        # Port 9 (discard) is never a ferry: the lane check must not be fatal.
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", port,
                            "--key", KEY, *extra, extra_env=env)
        self.assertEqual(rc, 0, out)
        return out

    # -- help ----------------------------------------------------------------
    def test_help_names_the_command(self):
        rc, out = run_ferry(self.home, "--help")
        self.assertEqual(rc, 0, out)
        self.assertIn("ferry pi", out.splitlines()[0])
        self.assertIn("pi-ferry", out)

    # -- wire ------------------------------------------------------------------
    def test_wire_inserts_a_ferry_provider_and_keeps_the_users_bytes(self):
        seed = SEEDS["comments_trailing_comma"]
        self.seed(seed)
        self.client_json("lap")
        out = self.wire(env={"FERRY_FLEET": "fl1"})
        self.assertNotIn(KEY, out, "key leaked to stdout")
        text = _rd(self.models)
        d = jsonc(self.models)
        self.assertIn("ollama", d["providers"])
        p = d["providers"]["ferry"]
        self.assertEqual(p["baseUrl"], "http://127.0.0.1:9/v1")
        self.assertEqual(p["api"], "openai-completions")
        self.assertEqual(p["headers"]["X-Ferry-Client"], "lap")
        self.assertEqual(p["headers"]["X-Ferry-Fleet"], "fl1")
        self.assertTrue(p["apiKey"].startswith("!cat "), p["apiKey"])
        self.assertIn("agents.key", p["apiKey"])
        self.assertEqual(p["models"][0]["id"], "heavy")
        # the user's own text survives verbatim (comments, trailing commas)
        self.assertIn("// my pi models", text)
        self.assertIn("// local ollama", text)
        self.assertIn('"models": [{"id": "llama3",},],', text)
        rec = json.loads(_rd(self.rec))
        ins = rec["edited"][0]["inserted"]
        self.assertEqual(text.replace(ins, "", 1), seed)

    def test_key_only_in_the_0600_keyfile(self):
        self.seed(SEEDS["no_trailing_comma"])
        out = self.wire()
        self.assertNotIn(KEY, out)
        self.assertEqual(_rd(self.keyfile), KEY)
        for p in tree(self.home):
            with open(p, "rb") as f:
                if KEY.encode() in f.read():
                    mode = stat.S_IMODE(os.stat(p).st_mode)
                    self.assertEqual(mode, 0o600, f"{p} holds the key at {oct(mode)}")
        body = _rd(self.wrap)
        self.assertNotIn(KEY, body)
        self.assertIn("ferry-managed: pi", body)
        self.assertIn("--provider ferry --model heavy", body)
        self.assertTrue(os.access(self.wrap, os.X_OK))

    def test_header_value_cannot_become_a_command_or_variable(self):
        self.client_json("!rm $HOME")
        self.wire()
        h = jsonc(self.models)["providers"]["ferry"]["headers"]["X-Ferry-Client"]
        self.assertEqual(h, "$!rm $$HOME")

    def test_wire_creates_models_json_when_absent_and_unwire_removes_it(self):
        self.wire()
        d = jsonc(self.models)
        self.assertEqual(list(d["providers"]), ["ferry"])
        rc, out = run_ferry(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.models))
        self.assertFalse(os.path.exists(self.wrap))
        self.assertFalse(os.path.exists(self.rec))

    def test_every_seed_wire_twice_then_unwire_is_byte_exact(self):
        for name, seed in SEEDS.items():
            with self.subTest(seed=name):
                shutil.rmtree(self.agent, ignore_errors=True)
                self.seed(seed)
                self.wire()
                first = _rd(self.models)
                self.wire()
                self.assertEqual(_rd(self.models), first, "re-wire is not idempotent")
                self.assertEqual(first.count('"ferry": {'), 1)
                self.assertIn("ferry", jsonc(self.models)["providers"])
                rc, out = run_ferry(self.home, "--unwire")
                self.assertEqual(rc, 0, out)
                self.assertEqual(_rd(self.models), seed)
                self.assertFalse(os.path.exists(self.wrap))
                self.assertFalse(os.path.exists(self.rec))

    def test_rewire_to_another_port_replaces_in_place(self):
        seed = SEEDS["comments_trailing_comma"]
        self.seed(seed)
        self.wire(port="9")
        self.wire(port="10")
        self.assertEqual(jsonc(self.models)["providers"]["ferry"]["baseUrl"],
                         "http://127.0.0.1:10/v1")
        run_ferry(self.home, "--unwire")
        self.assertEqual(_rd(self.models), seed)

    def test_dry_run_unwire_changes_nothing(self):
        self.seed(SEEDS["no_trailing_comma"])
        self.wire()
        before = _rd(self.models)
        rc, out = run_ferry(self.home, "--unwire", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("[dry-run]", out)
        self.assertEqual(_rd(self.models), before)
        self.assertTrue(os.path.exists(self.rec))
        self.assertTrue(os.path.exists(self.wrap))

    def test_unwire_keeps_user_edits_made_after_wiring(self):
        self.seed(SEEDS["no_trailing_comma"])
        self.wire()
        with open(self.models, "a") as f:
            f.write("// added later by me\n")
        run_ferry(self.home, "--unwire")
        text = _rd(self.models)
        self.assertIn("// added later by me", text)
        self.assertNotIn('"ferry"', text)

    # -- refusals ----------------------------------------------------------------
    def test_block_comment_is_refused_untouched(self):
        seed = '/* pi does not read this */\n{"providers": {}}\n'
        self.seed(seed)
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", "9", "--key", KEY)
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT modified", out)
        self.assertEqual(_rd(self.models), seed)

    def test_a_users_own_ferry_provider_is_never_replaced(self):
        seed = '{"providers": {"ferry": {"baseUrl": "http://mine/v1", "api": "openai-completions"}}}\n'
        self.seed(seed)
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", "9", "--key", KEY)
        self.assertNotEqual(rc, 0)
        self.assertIn("did not write", out)
        self.assertEqual(_rd(self.models), seed)

    def test_invalid_json_is_refused_untouched(self):
        seed = '{"providers": {\n'
        self.seed(seed)
        rc, out = run_ferry(self.home, "--host", "127.0.0.1", "--port", "9", "--key", KEY)
        self.assertNotEqual(rc, 0)
        self.assertEqual(_rd(self.models), seed)

    # -- lane check ----------------------------------------------------------------
    def test_unserved_lane_falls_back_and_every_served_lane_is_listed(self):
        with Stub(lanes=("flash", "medium")) as stub:
            out = self.wire(port=str(stub.port))
        self.assertIn("does not serve lane 'heavy'", out)
        p = jsonc(self.models)["providers"]["ferry"]
        self.assertEqual([m["id"] for m in p["models"]], ["flash", "medium"])
        self.assertIn("--model flash", _rd(self.wrap))

    def test_served_lanes_are_all_listed_default_first(self):
        with Stub(lanes=("flash", "heavy", "medium")) as stub:
            self.wire(port=str(stub.port))
        p = jsonc(self.models)["providers"]["ferry"]
        self.assertEqual([m["id"] for m in p["models"]], ["heavy", "flash", "medium"])

    def test_agent_dir_flag_and_env_relocate_the_file(self):
        alt = os.path.join(self.home, "alt-agent")
        self.wire("--agent-dir", alt)
        self.assertTrue(os.path.exists(os.path.join(alt, "models.json")))
        self.assertFalse(os.path.exists(self.models))
        run_ferry(self.home, "--unwire")
        alt2 = os.path.join(self.home, "alt2")
        self.wire(env={"PI_CODING_AGENT_DIR": alt2})
        self.assertTrue(os.path.exists(os.path.join(alt2, "models.json")))


def find_pi():
    cand = os.environ.get("FERRY_E2E_BIN_PI") or shutil.which("pi")
    return cand if cand and os.access(cand, os.X_OK) else None


class PiEndToEnd(unittest.TestCase):
    """The real pi, through the pi-ferry wrapper, against a local stub."""

    def test_pi_ferry_talks_to_the_stub_with_key_and_client_header(self):
        pi = find_pi()
        if not pi:
            self.skipTest("pi binary not found (PATH or FERRY_E2E_BIN_PI)")
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd, \
                Stub() as stub:
            # node (pi's runtime) must be reachable; add the dirs of both.
            dirs = [os.path.dirname(pi)]
            node = shutil.which("node")
            if node:
                dirs.append(os.path.dirname(node))
            path = ":".join(dict.fromkeys(dirs + BASE_PATH.split(":")))
            seed = SEEDS["comments_trailing_comma"]
            agent = os.path.join(home, ".pi", "agent")
            os.makedirs(agent)
            with open(os.path.join(agent, "models.json"), "w") as f:
                f.write(seed)
            script = (f"source {REPO}/lib/ferry-core.zsh\nsource {REPO}/lib/ferry-agents.zsh\n"
                      f"source {REPO}/lib/ferry-pi.zsh\ncmd_pi \"$@\"\n")
            os.makedirs(os.path.join(home, ".config", "ferry"))
            with open(os.path.join(home, ".config", "ferry", "client.json"), "w") as f:
                json.dump({"host": "127.0.0.1", "port": str(stub.port), "name": "e2e-box"}, f)
            wenv = clean_env(home, {}, path)
            p = subprocess.run(["zsh", "-c", script, "ferry-pi", "--host", "127.0.0.1",
                                "--port", str(stub.port), "--key", KEY],
                               capture_output=True, text=True, timeout=60, env=wenv)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            stub.seen.clear()
            env = clean_env(home, {"PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1",
                                   "PI_TELEMETRY": "0"}, path)
            r = subprocess.run([os.path.join(home, ".local", "bin", "pi-ferry"),
                                "-p", "say hi", "--no-session"],
                               capture_output=True, text=True, timeout=120, env=env,
                               cwd=cwd, stdin=subprocess.DEVNULL)
            posts = [s for s in stub.seen if s["method"] == "POST"]
            self.assertTrue(posts, f"the stub saw no POST. stdout={r.stdout!r} stderr={r.stderr!r}")
            req = posts[0]
            self.assertTrue(req["path"].startswith("/v1/chat/completions"), req["path"])
            self.assertEqual(req["headers"].get("authorization"), f"Bearer {KEY}")
            self.assertEqual(req["headers"].get("x-ferry-client"), "e2e-box")
            body = json.loads(req["body"])
            self.assertEqual(body["model"], "heavy")
            self.assertIn("FERRY_STUB_OK", r.stdout, f"stderr={r.stderr!r}")
            self.assertTrue(all(s["path"].startswith("/v1/") for s in stub.seen))


if __name__ == "__main__":
    unittest.main()
