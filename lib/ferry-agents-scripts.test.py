#!/usr/bin/env python3
"""Stdlib unittest for the agent-CLI blocks in the three client scripts (issue #3).

Run:  python3 lib/ferry-agents-scripts.test.py -v

client-bootstrap.sh section 4f wires every registry client whose binary is on
PATH (or that got --with-<client>), client-reset.sh re-wires only clients whose
recorded mode is "full", and client-cleanup.sh unwires them (through the ferry
CLI when present, through a python fallback when not). The subject here is
`goose`: its wiring is a wrapper only, ~/.local/bin/goose-ferry.

The scripts run for real against a throwaway $HOME and a stub host. The PATH is
built from scratch: a developer machine often has real goose / pi / gemini /
omp / copilot installed, and an inherited PATH would make "no goose binary"
untestable. Only the agent binaries this test puts in its own stub dir are
visible; test_path_trap_is_closed checks that as a control.
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
BOOTSTRAP = os.path.join(REPO, "client-bootstrap.sh")
RESET = os.path.join(REPO, "client-reset.sh")
CLEANUP = os.path.join(REPO, "client-cleanup.sh")
FERRY = os.path.join(REPO, "ferry")
LANES = ("heavy", "flash", "local-orch", "local-sub")
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


class StubHost(BaseHTTPRequestHandler):
    """/v1/models and /ferry: all a client bootstrap depends on."""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/models"):
            body = json.dumps({"data": [{"id": l} for l in LANES]}).encode()
            ctype = "application/json"
        elif self.path == "/ferry":
            with open(FERRY, "rb") as f:
                body = f.read()
            ctype = "text/plain"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class AgentsScriptsTest(unittest.TestCase):
    """bootstrap / reset / cleanup against the goose client."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHost)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-agents-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        # tools dir: only zsh, python3 and curl, symlinked from wherever they
        # resolve on the inherited PATH. Agent binaries are NOT carried over.
        self.tools = os.path.join(self.home, "tools")
        os.makedirs(self.tools)
        for t in ("zsh", "python3", "curl"):
            src = shutil.which(t)
            self.assertIsNotNone(src, f"{t} not found on PATH")
            os.symlink(src, os.path.join(self.tools, t))
        # stub dir: the fake agent binaries a test chooses to install
        self.stub = os.path.join(self.home, "stubbin")
        os.makedirs(self.stub)

    # --- helpers ------------------------------------------------------------
    def add_binary(self, name):
        p = os.path.join(self.stub, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)

    def env(self):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["PATH"] = os.pathsep.join((self.stub, self.tools) + SYSTEM_PATH)
        e["HOST_NAME"] = "127.0.0.1"
        e["HOST_PORT"] = str(self.port)
        e["SHARE_PORT"] = str(self.port)
        for k in ("OPENCODE_CONFIG", "OPENCODE_TUI_CONFIG", "XDG_CONFIG_HOME",
                  "XDG_CACHE_HOME", "XDG_DATA_HOME", "FERRY_MASTER_KEY"):
            e.pop(k, None)
        return e

    def run_script(self, script, *flags):
        p = subprocess.run(["zsh", script, *flags], env=self.env(),
                           capture_output=True, text=True, timeout=240)
        self.assertEqual(p.returncode, 0,
                         f"{os.path.basename(script)} {' '.join(flags)} exited "
                         f"{p.returncode}:\n{p.stdout}\n{p.stderr}")
        return p

    def path(self, *parts):
        return os.path.join(self.home, *parts)

    def profile(self):
        with open(self.path(".config", "ferry", "client.json")) as f:
            return json.load(f)

    def registry(self):
        p = subprocess.run(["zsh", FERRY, "agents", "--list"], env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, f"ferry agents --list:\n{p.stdout}\n{p.stderr}")
        names = p.stdout.split()
        self.assertIn("goose", names, p.stdout)
        return names

    def wrapper(self, client="goose"):
        return self.path(".local", "bin", f"{client}-ferry")

    def assert_mode(self, client, want, out=""):
        got = self.profile().get(f"{client}_mode")
        self.assertEqual(got, want, f"{client}_mode is {got!r}, want {want!r}\n{out}")

    def assert_others_none(self, out=""):
        for c in self.registry():
            if c == "goose":
                continue
            self.assert_mode(c, "none", out)
            self.assertFalse(os.path.exists(self.wrapper(c)), f"{c} wrapper exists\n{out}")

    # --- the control ---------------------------------------------------------
    def test_path_trap_is_closed(self):
        """Under this PATH the only agent binary visible is the one placed."""
        def installed():
            p = subprocess.run(["zsh", FERRY, "agents", "--installed"], env=self.env(),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, f"{p.stdout}\n{p.stderr}")
            return p.stdout.split()
        self.assertEqual(installed(), [], "an agent binary leaked in from the real PATH")
        self.add_binary("goose")
        self.assertEqual(installed(), ["goose"])
        for t in ("zsh", "python3", "curl"):
            r = subprocess.run(["sh", "-c", f"command -v {t}"], env=self.env(),
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{t} does not resolve under the test PATH")

    # --- bootstrap -----------------------------------------------------------
    def test_1_bootstrap_with_goose_binary_wires_it_only(self):
        self.add_binary("goose")
        out = self.run_script(BOOTSTRAP).stdout
        self.assertTrue(os.path.exists(self.wrapper()), out)
        self.assert_mode("goose", "full", out)
        self.assertTrue(os.path.exists(self.path(".config", "ferry", "goose.json")), out)
        self.assert_others_none(out)

    def test_2_bootstrap_without_goose_binary_skips_it(self):
        out = self.run_script(BOOTSTRAP).stdout
        self.assertFalse(os.path.exists(self.wrapper()), out)
        self.assert_mode("goose", "none", out)
        self.assertFalse(os.path.exists(self.path(".config", "ferry", "goose.json")), out)
        self.assert_others_none(out)

    def test_3a_no_goose_skips_it_even_with_the_binary(self):
        self.add_binary("goose")
        out = self.run_script(BOOTSTRAP, "--no-goose").stdout
        self.assertFalse(os.path.exists(self.wrapper()), out)
        self.assert_mode("goose", "none", out)

    def test_3b_no_agents_skips_every_client(self):
        self.add_binary("goose")
        out = self.run_script(BOOTSTRAP, "--no-agents").stdout
        self.assertFalse(os.path.exists(self.wrapper()), out)
        self.assert_mode("goose", "none", out)
        self.assert_others_none(out)

    def test_4_with_goose_wires_it_without_the_binary(self):
        out = self.run_script(BOOTSTRAP, "--with-goose").stdout
        self.assertTrue(os.path.exists(self.wrapper()), out)
        self.assert_mode("goose", "full", out)
        self.assert_others_none(out)

    # --- reset ---------------------------------------------------------------
    def test_5a_reset_restores_a_deleted_wrapper_for_a_full_client(self):
        self.add_binary("goose")
        self.run_script(BOOTSTRAP)
        os.remove(self.wrapper())
        out = self.run_script(RESET).stdout
        self.assertTrue(os.path.exists(self.wrapper()), out)
        self.assert_mode("goose", "full", out)

    def test_5b_reset_does_not_widen_a_none_client(self):
        self.run_script(BOOTSTRAP)
        self.assert_mode("goose", "none")
        out = self.run_script(RESET).stdout
        self.assertFalse(os.path.exists(self.wrapper()), out)
        self.assertFalse(os.path.exists(self.path(".config", "ferry", "goose.json")), out)
        self.assert_mode("goose", "none", out)

    # --- cleanup -------------------------------------------------------------
    def test_6_cleanup_with_the_cli_removes_wrapper_and_record(self):
        self.add_binary("goose")
        self.run_script(BOOTSTRAP)
        self.assertTrue(os.path.exists(self.wrapper()))
        self.assertTrue(os.access(self.path(".local", "bin", "ferry"), os.X_OK))
        out = self.run_script(CLEANUP).stdout
        self.assertFalse(os.path.exists(self.wrapper()), out)
        self.assertFalse(os.path.exists(self.path(".config", "ferry", "goose.json")), out)

    def test_7a_cleanup_fallback_removes_a_matching_wrapper(self):
        self.add_binary("goose")
        self.run_script(BOOTSTRAP)
        self.assertTrue(os.path.exists(self.wrapper()))
        os.remove(self.path(".local", "bin", "ferry"))
        out = self.run_script(CLEANUP).stdout
        self.assertFalse(os.path.exists(self.wrapper()), out)
        self.assertIn("Removed", out)

    def test_7b_cleanup_fallback_keeps_a_wrapper_the_user_edited(self):
        self.add_binary("goose")
        self.run_script(BOOTSTRAP)
        with open(self.wrapper(), "a") as f:
            f.write("# my own line\n")
        os.remove(self.path(".local", "bin", "ferry"))
        out = self.run_script(CLEANUP).stdout
        self.assertTrue(os.path.exists(self.wrapper()), out)
        self.assertIn("changed since", out)
        with open(self.wrapper()) as f:
            self.assertIn("# my own line", f.read())


if __name__ == "__main__":
    unittest.main()
