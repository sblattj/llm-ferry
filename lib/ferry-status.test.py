#!/usr/bin/env python3
"""Stdlib unittest for `ferry status` relay reporting after a reboot.

Run:  python3 lib/ferry-status.test.py

After a reboot the relay is gone but ~/.config/ferry/relay-published.json
survives. Status must say the relay is OFFLINE and must not list the stale
tmux entries as live clients. Runs the REAL built ./ferry against a throwaway
HOME with FERRY_RELAY_PORT pointed at a port nothing listens on.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StatusRelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".config", "ferry"))
        with open(os.path.join(self.home, ".config", "ferry", "relay-published.json"), "w") as f:
            json.dump({"8101": {"kind": "tmux", "label": "demo-box", "client": "192.0.2.7",
                                "bind": "0.0.0.0", "since": "2026-01-01"}}, f)

    def status(self, relay_port):
        env = dict(os.environ, HOME=self.home, TMPDIR=self.tmp,
                   FERRY_RELAY_PORT=str(relay_port))
        p = subprocess.run([FERRY, "status"], env=env, capture_output=True,
                           text=True, timeout=90)
        return ANSI.sub("", p.stdout + p.stderr)

    def test_dead_relay_is_offline_and_ghost_clients_hidden(self):
        port = free_port()
        out = self.status(port)
        self.assertIn(f"Port {port} (reverse relay) is OFFLINE", out)
        self.assertNotIn("ferry tmux demo-box", out)
        self.assertIn("stale", out)

    def test_live_relay_keeps_listing(self):
        """Control: a listener on the relay port that is not ferry's is not
        'offline', and a ferry relay (marker in argv) keeps the tmux listing."""
        port = free_port()
        marker = subprocess.Popen(
            ["python3", "-c",
             "import socket,time;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
             f"s.bind(('127.0.0.1',{port}));s.listen(1);time.sleep(60)",
             "ferry-relay-marker"])
        self.addCleanup(marker.kill)
        import time; time.sleep(0.8)
        out = self.status(port)
        self.assertNotIn("(reverse relay) is OFFLINE", out)
        self.assertIn("ferry tmux demo-box", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
