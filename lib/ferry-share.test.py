#!/usr/bin/env python3
"""Stdlib unittest for the share server's client-script injection.

Run:  python3 lib/ferry-share.test.py

`ferry share` does not serve the client scripts statically. It rewrites three
placeholders on the way out — HOST_MDNS_PLACEHOLDER, SHARE_PORT_PLACEHOLDER, and
the literal `your-host.local` fallback — so the copy a client pipes into zsh
already knows which machine to talk to.

The list of scripts that get this treatment was hardcoded to one name. Adding
client-reset.sh without adding it to that list produces the worst possible
failure: HTTP 200, a script that looks fine, and placeholders reaching the
client verbatim, where HOST_MDNS_PLACEHOLDER resolves to nothing and the first
curl fails against a host that does not exist. Nothing errors on the host side.

These tests run the REAL embedded server — extracted from the built `ferry`, not
a reimplementation — against a temp directory, so a change to the handler that
breaks injection fails here rather than on someone's laptop.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

PLACEHOLDERS = ("HOST_MDNS_PLACEHOLDER", "SHARE_PORT_PLACEHOLDER", "your-host.local")

# Every client-facing script the repo ships. Each must be injected, and each
# must actually carry the placeholders for injection to have anything to do.
CLIENT_SCRIPTS = ("client-bootstrap.sh", "client-reset.sh")

STUB = """#!/bin/zsh
HOST_NAME="${HOST_NAME:-HOST_MDNS_PLACEHOLDER}"
SHARE_PORT="${SHARE_PORT:-SHARE_PORT_PLACEHOLDER}"
if [[ "$HOST_NAME" == "HOST_MDNS_PLACEHOLDER" ]]; then
  HOST_NAME="your-host.local"
fi
echo "em-dash payload — multi-byte, guards the Content-Length byte count"
"""


def extract_embedded_server():
    """Pull the python HTTP server out of the built `ferry`'s cmd_share heredoc."""
    with open(FERRY) as f:
        text = f.read()
    m = re.search(r"nohup python3 - .*?<<'PYEOF'.*?\n(.*?)\nPYEOF\n", text, re.S)
    if not m:
        raise AssertionError("could not find the share server heredoc in ./ferry")
    return m.group(1)


def read_repo(*parts):
    with open(os.path.join(REPO, *parts)) as f:
        return f.read()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ShareServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ferry-share-")
        for name in CLIENT_SCRIPTS:
            with open(os.path.join(cls.dir, name), "w") as f:
                f.write(STUB)
        # A client-facing-looking script that is NOT on the injection list, to
        # prove the handler is selective rather than rewriting every .sh.
        with open(os.path.join(cls.dir, "unrelated.sh"), "w") as f:
            f.write(STUB)

        cls.server_py = os.path.join(cls.dir, "_server.py")
        with open(cls.server_py, "w") as f:
            f.write(extract_embedded_server())

        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, cls.server_py, str(cls.port), cls.dir, "ferry-share-marker"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/manifest", timeout=1).read()
                break
            except Exception:
                if cls.proc.poll() is not None:
                    raise AssertionError(f"share server died:\n{cls.proc.stdout.read()}")
                time.sleep(0.15)
        else:
            raise AssertionError("share server never came up")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=10)
        cls._drain()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, r.headers, r.read()

    @classmethod
    def _drain(cls):
        if cls.proc.stdout and not cls.proc.stdout.closed:
            cls.proc.stdout.close()


class TestClientScriptInjection(ShareServerCase):
    def test_every_client_script_is_injected(self):
        for name in CLIENT_SCRIPTS:
            with self.subTest(script=name):
                status, _, body = self.get(f"/{name}")
                self.assertEqual(status, 200)
                text = body.decode()
                for ph in PLACEHOLDERS:
                    self.assertNotIn(ph, text, f"{name} served with {ph} un-substituted")

    def test_injected_host_is_a_real_dotlocal_name(self):
        _, _, body = self.get("/client-reset.sh")
        text = body.decode()
        self.assertRegex(text, r'HOST_NAME:-[\w.-]+\.local')

    def test_injected_port_is_the_live_share_port(self):
        _, _, body = self.get("/client-reset.sh")
        self.assertIn(f"SHARE_PORT:-{self.port}", body.decode())

    def test_content_length_counts_bytes_not_characters(self):
        # The scripts contain multi-byte UTF-8. A char-count header truncates the
        # tail, and the client's zsh reports it as an unmatched quote at EOF —
        # a failure that points nowhere near the header that caused it.
        for name in CLIENT_SCRIPTS:
            with self.subTest(script=name):
                _, headers, body = self.get(f"/{name}")
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertIn("—", body.decode())

    def test_unlisted_scripts_are_served_verbatim(self):
        _, _, body = self.get("/unrelated.sh")
        self.assertIn("HOST_MDNS_PLACEHOLDER", body.decode())


class TestShippedClientScripts(unittest.TestCase):
    """The real scripts must carry what the server expects to rewrite."""

    def test_each_client_script_exists_and_is_executable(self):
        for name in CLIENT_SCRIPTS:
            p = os.path.join(REPO, name)
            self.assertTrue(os.path.exists(p), f"{name} is missing")
            self.assertTrue(os.access(p, os.X_OK), f"{name} is not executable")

    def test_each_client_script_carries_the_placeholders(self):
        # Injection is a no-op on a script that hardcodes a host, and the result
        # is a client silently pointed at whatever the author's machine was.
        for name in CLIENT_SCRIPTS:
            with self.subTest(script=name):
                text = read_repo(name)
                self.assertIn("HOST_MDNS_PLACEHOLDER", text)
                self.assertIn("SHARE_PORT_PLACEHOLDER", text)

    def test_server_injection_list_matches_the_shipped_scripts(self):
        # Guards the actual regression: a new client script added to the repo but
        # never added to the handler's INJECTED tuple.
        module = read_repo("lib", "ferry-share.zsh")
        m = re.search(r"INJECTED\s*=\s*\(([^)]*)\)", module)
        self.assertIsNotNone(m, "no INJECTED tuple in lib/ferry-share.zsh")
        listed = set(re.findall(r'"([^"]+)"', m.group(1)))
        self.assertEqual(listed, set(CLIENT_SCRIPTS))

    def test_the_share_server_never_injects_the_master_key(self):
        # The share server is unauthenticated, so the v1.22.0 master key must
        # never ride the served script — that would publish it to the LAN.
        # Comment lines may DOCUMENT the omission; no code may reference it.
        code = "\n".join(line for line in read_repo("lib", "ferry-share.zsh").splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("master_key", code)
        self.assertNotIn("MASTER_KEY", code)

    def test_reset_validates_the_download_before_overwriting(self):
        # A share server that is down, or a proxy serving an error page, must not
        # be able to replace a working CLI with an HTML 404.
        text = read_repo("client-reset.sh")
        self.assertIn("mktemp", text)
        self.assertIn("cmd_opencode()", text)
        self.assertIn("zsh -n", text)
        # ...and the move must happen only after those checks.
        self.assertLess(text.index("zsh -n"), text.index('mv "$tmp_ferry"'))

    def test_reset_neutralises_an_inherited_opencode_config(self):
        text = read_repo("client-reset.sh")
        self.assertIn("env -u OPENCODE_CONFIG", text)

    def test_reset_passes_the_host_explicitly(self):
        # Without --host/--port, a ferry that cannot find a client profile
        # concludes it is running ON the host and wires the config to
        # 127.0.0.1 — which on a client points opencode at itself. Observed
        # against a scratch HOME before this was added.
        text = read_repo("client-reset.sh")
        self.assertIn('--host "$HOST_NAME" --port "$HOST_PORT"', text)

    def test_reset_covers_all_three_targets(self):
        text = read_repo("client-reset.sh")
        for target in ("opencode/opencode.json",
                       "ferry/opencode-cloud.json",
                       "ferry/opencode-local.json"):
            self.assertIn(target, text)
        self.assertIn("--local", text)


class TestClientTelemetryLogPath(unittest.TestCase):
    """`ferry msg` / `ferry log` must outlive the checkout the server started in.

    The handler used to build its path from the serving directory, captured once
    at startup. A share server launched from a git worktree that was later
    removed turned every /hq POST into an unhandled exception and a bare 500:
    the client saw a failed send, the host logged nothing a human reads, and the
    message was gone. Observed 2026-08-26 — two messages lost that way.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-hqhome-")
        self.addCleanup(shutil.rmtree, self.home, True)
        # The tree the server is launched from — deliberately separate from HOME,
        # and deliberately deletable.
        self.serve = tempfile.mkdtemp(prefix="ferry-hqserve-")
        self.addCleanup(shutil.rmtree, self.serve, True)

        server_py = os.path.join(self.home, "_server.py")
        with open(server_py, "w") as f:
            f.write(extract_embedded_server())

        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, server_py, str(self.port), self.serve, "ferry-share-marker"],
            env=dict(os.environ, HOME=self.home),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self._stop)

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/manifest", timeout=1).read()
                return
            except Exception:
                if self.proc.poll() is not None:
                    raise AssertionError(f"share server died:\n{self.proc.stdout.read()}")
                time.sleep(0.15)
        raise AssertionError("share server never came up")

    def _stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        if self.proc.stdout and not self.proc.stdout.closed:
            self.proc.stdout.close()

    def post_hq(self, text):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/hq",
                                     data=text.encode(), method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    @property
    def log_path(self):
        return os.path.join(self.home, ".config", "ferry", "client_logs.txt")

    def test_telemetry_lands_outside_the_serving_directory(self):
        self.assertEqual(self.post_hq("hello from a client"), 200)
        self.assertTrue(os.path.exists(self.log_path),
                        "client telemetry did not reach ~/.config/ferry/client_logs.txt")
        with open(self.log_path) as f:
            self.assertIn("hello from a client", f.read())
        self.assertFalse(os.path.exists(os.path.join(self.serve, "client_logs.txt")),
                         "telemetry was written into the serving directory")

    def test_the_directory_is_created_if_absent(self):
        # A fresh host may never have run anything that makes ~/.config/ferry.
        self.assertFalse(os.path.exists(os.path.dirname(self.log_path)))
        self.assertEqual(self.post_hq("first ever message"), 200)
        self.assertTrue(os.path.exists(self.log_path))

    def test_a_deleted_serving_directory_does_not_lose_the_message(self):
        # The actual regression: the checkout the server was launched from goes
        # away (a removed worktree), and every send after that 500s.
        self.assertEqual(self.post_hq("before"), 200)
        shutil.rmtree(self.serve, ignore_errors=True)
        self.assertEqual(self.post_hq("after the checkout vanished"), 200)
        with open(self.log_path) as f:
            body = f.read()
        self.assertIn("before", body)
        self.assertIn("after the checkout vanished", body)


if __name__ == "__main__":
    if not os.path.exists(FERRY):
        sys.exit("built ./ferry not found — run ./build.zsh first")
    unittest.main(verbosity=2)
