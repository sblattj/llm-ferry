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


if __name__ == "__main__":
    if not os.path.exists(FERRY):
        sys.exit("built ./ferry not found — run ./build.zsh first")
    unittest.main(verbosity=2)
