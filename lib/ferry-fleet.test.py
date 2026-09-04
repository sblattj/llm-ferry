#!/usr/bin/env python3
"""Stdlib unittest for `ferry fleet` — the fleet-selection CLI.

Run:  python3 lib/ferry-fleet.test.py

`ferry fleet` talks to the front door's control plane at
GET/POST /v1/ferry/fleet (front/ferry_front.py). This suite never launches
that real server — it stands up a fake one that always answers the fixed
document below, and asserts on what `cmd_fleet()` (lib/ferry-fleet.zsh) sent
it and printed, mirroring the harness shape lib/ferry-integrate.test.py uses
for `ferry opencode` (a fake HTTP server + a temp HOME + subprocess).

The fixed document mirrors the shape in
docs/superpowers/specs/2026-09-04-fleets-design.md §5:
  - "domestic" is both the default and the caller's own resolved fleet, so
    TestLs can assert both the '*' and 'you' marks land on the same row.
  - "clients": {"host": "international"} lets TestShow assert the
    'host -> international' line without needing a second fake client.
  - the caller's identity in the document, "you": "laptop", matches the
    client.json 'name' every test's temp HOME writes, so a real cmd_fleet
    run and the fixture agree on who is asking.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

DOCUMENT = {
    "you": "laptop",
    "fleet": "domestic",
    "default": "domestic",
    "fleets": {
        "domestic": {
            "heavy": "chatgpt/responses/gpt-5.6-sol",
            "flash": "openrouter/~google/gemini-flash-latest",
            "super-flash": "openrouter/~google/gemini-flash-latest",
        },
        "international": {
            "heavy": "anthropic/k3",
            "flash": "zai/glm-5.3-flash",
            "super-flash": "zai/glm-5.3-flash",
        },
    },
    "clients": {"host": "international"},
}


class _FleetHandler(BaseHTTPRequestHandler):
    # Set by do_POST only. Reset to None in setUp() before every test, so a
    # test that must NOT post (a typo, or --default refused client-side)
    # can assert on it staying None.
    LAST_HEADERS = None
    LAST_BODY = None

    def _reply(self):
        body = json.dumps(DOCUMENT).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/v1/ferry/fleet":
            self.send_error(404)
            return
        self._reply()

    def do_POST(self):
        if self.path != "/v1/ferry/fleet":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        # self.headers is an email.message.Message: .get() is
        # case-insensitive, so this survives whatever casing urllib sends.
        type(self).LAST_HEADERS = self.headers
        type(self).LAST_BODY = json.loads(raw) if raw else None
        self._reply()

    def log_message(self, *a):        # keep test output clean
        pass


class FerryFleetCase(unittest.TestCase):
    """Base: a fake front door plus a scratch client.json HOME, and a runner."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FleetHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _FleetHandler.LAST_HEADERS = None
        _FleetHandler.LAST_BODY = None
        self.home = tempfile.mkdtemp(prefix="ferry-fleet-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        cfg_dir = os.path.join(self.home, ".config", "ferry")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "client.json"), "w") as f:
            json.dump({
                "host": "127.0.0.1",
                "port": str(self.port),
                "share_port": "8095",
                "name": "laptop",
                "master_key": "k1",
            }, f)

    def run_fleet(self, *args, home=None):
        cmd = ["zsh", FERRY, "fleet", *args]
        env = dict(os.environ)
        env["HOME"] = home if home is not None else self.home
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              cwd=REPO, timeout=30)
        return proc


class TestLs(FerryFleetCase):
    def test_table_has_both_fleets_with_default_and_you_marks(self):
        proc = self.run_fleet("ls")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("domestic", proc.stdout)
        self.assertIn("international", proc.stdout)
        domestic_line = next(l for l in proc.stdout.splitlines()
                             if l.startswith("domestic"))
        self.assertIn("*", domestic_line,
                      "default fleet 'domestic' should carry the '*' mark")
        self.assertIn("you", domestic_line,
                      "caller's resolved fleet 'domestic' should carry 'you'")


class TestShow(FerryFleetCase):
    def test_show_lists_host_arrow_international(self):
        proc = self.run_fleet("show")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("host -> international", proc.stdout)


class TestUse(FerryFleetCase):
    def test_use_posts_fleet_with_identity_and_auth_headers(self):
        proc = self.run_fleet("use", "international")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(_FleetHandler.LAST_BODY, {"fleet": "international"})
        self.assertEqual(_FleetHandler.LAST_HEADERS.get("X-Ferry-Client"), "laptop")
        self.assertEqual(_FleetHandler.LAST_HEADERS.get("Authorization"), "Bearer k1")

    def test_use_clear_posts_null_fleet(self):
        proc = self.run_fleet("use", "--clear")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(_FleetHandler.LAST_BODY, {"fleet": None})

    def test_typo_exits_nonzero_lists_real_fleets_and_never_posts(self):
        proc = self.run_fleet("use", "nope")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("domestic", proc.stderr)
        self.assertIn("international", proc.stderr)
        self.assertIsNone(_FleetHandler.LAST_BODY,
                          "a typo must be caught before any POST")

    def test_default_refused_on_a_client_before_any_http_call(self):
        proc = self.run_fleet("use", "international", "--default")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr.strip(), "the default is the host's to set")
        self.assertIsNone(_FleetHandler.LAST_BODY,
                          "--default on a client must never reach the network")

    def test_an_unreachable_front_door_is_a_one_line_error(self):
        home2 = tempfile.mkdtemp(prefix="ferry-fleet-home2-")
        self.addCleanup(shutil.rmtree, home2, True)
        cfg_dir = os.path.join(home2, ".config", "ferry")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "client.json"), "w") as f:
            json.dump({
                "host": "127.0.0.1",
                "port": "1",
                "share_port": "8095",
                "name": "laptop",
                "master_key": "k1",
            }, f)
        proc = self.run_fleet("show", home=home2)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertTrue(
            proc.stderr.startswith(
                "ferry fleet: cannot reach the front door at http://127.0.0.1:1:"
            ),
            proc.stderr,
        )
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_bare_default_flag_is_refused_before_any_http_call(self):
        proc = self.run_fleet("use", "--default")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Usage: ferry fleet use", proc.stderr)
        self.assertIsNone(_FleetHandler.LAST_BODY)


class TestHelp(FerryFleetCase):
    def test_help_exits_zero_and_mentions_every_verb(self):
        proc = self.run_fleet("--help")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for verb in ("ls", "show", "use"):
            self.assertIn(verb, proc.stdout)


class TestBuildSync(unittest.TestCase):
    def test_generated_ferry_is_in_sync_with_lib(self):
        # Mirrors lib/ferry-integrate.test.py's own sync guard: `ferry` is a
        # build artifact, and an edit to lib/ferry-fleet.zsh that was never
        # rebuilt is invisible to every client, which fetches the one file.
        proc = subprocess.run(["zsh", os.path.join(REPO, "build.zsh"), "--check"],
                              capture_output=True, text=True, cwd=REPO, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    if not os.path.exists(FERRY):
        sys.exit("built ./ferry not found — run ./build.zsh first")
    unittest.main(verbosity=2)
