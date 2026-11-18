#!/usr/bin/env python3
"""Stdlib unittest for `_ferry_ensure_share_relay` and its two callers.

Run:  python3 lib/ferry-ensure-share-relay.test.py

After a host reboot `ferry up` restored only the endpoint; the share server and
the reverse relay (plain background processes) stayed down. The helper starts
whichever of the two is not listening, never touches one that already is, and
never lets the relay banner (which prints the relay token) reach its output.

No subcommand calls the helper except `up` (which would start litellm), so the
helper is driven by sourcing the real lib modules in a zsh child, with
FERRY_BIN_PATH pointed at the REAL built ./ferry so the share server and relay
it starts are the real ones. Everything runs under a throwaway HOME/TMPDIR on
two free ports; the processes started are killed by sandbox port only (the live
host's share server and relay carry the same kill markers).
"""
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
LIB = os.path.join(REPO, "lib")
TOKEN = "SENTINEL-relay-token-do-not-print-9f3a"


def read(path):
    with open(path) as f:
        return f.read()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def listeners(port):
    r = subprocess.run(["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                       capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


class EnsureShareRelay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ferry-ensure-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".config", "ferry"))
        with open(os.path.join(self.home, ".config", "ferry", "relay-token"), "w") as f:
            f.write(TOKEN + "\n")
        self.share, self.relay = free_port(), free_port()
        while self.relay == self.share:
            self.relay = free_port()
        self.env = dict(os.environ, HOME=self.home, TMPDIR=self.tmp,
                        FERRY_SHARE_PORT=str(self.share), FERRY_RELAY_PORT=str(self.relay))
        self.env.pop("FERRY_SHARE_RELAY", None)
        self.holders = []

    def tearDown(self):
        for port in (self.share, self.relay):
            for pid in [p for p in listeners(port) if p != os.getpid()]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for h in self.holders:
            h.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        script = (f"cd {LIB} && source ./ferry-core.zsh && source ./ferry-serve.zsh && "
                  f"source ./ferry-share.zsh && FERRY_BIN_PATH={FERRY} && "
                  "_ferry_ensure_share_relay && echo RC=0 || echo RC=$?")
        r = subprocess.run(["zsh", "-c", script], env=env, capture_output=True,
                           text=True, timeout=90)
        m = re.search(r"RC=(\d+)\s*$", r.stdout)
        self.assertIsNotNone(m, f"no RC line: {r.stdout!r} {r.stderr!r}")
        return int(m.group(1)), r.stdout + r.stderr

    def test_starts_both_then_reports_already(self):
        rc, out = self.call()
        self.assertEqual(rc, 0, out)
        self.assertTrue(listeners(self.share), "share server not listening")
        self.assertTrue(listeners(self.relay), "relay not listening")
        self.assertNotIn(TOKEN, out, "relay token leaked into output")
        before = (listeners(self.share), listeners(self.relay))
        rc, out = self.call()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("already on"), 2, out)
        self.assertNotIn("starting", out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(before, (listeners(self.share), listeners(self.relay)),
                         "second call replaced a listener")

    def test_stranger_on_relay_port_warns_and_rc1(self):
        s = socket.socket()
        s.bind(("0.0.0.0", self.relay))
        s.listen(1)
        self.holders.append(s)
        rc, out = self.call()
        self.assertEqual(rc, 1, out)
        self.assertIn("not ferry's relay", out)
        self.assertIn("PID", out)
        self.assertTrue(listeners(self.share), "share server should still start")
        self.assertNotIn(TOKEN, out)

    def test_no_token_file_skips_relay(self):
        os.unlink(os.path.join(self.home, ".config", "ferry", "relay-token"))
        rc, out = self.call()
        self.assertEqual(rc, 0, out)
        self.assertTrue(listeners(self.share), "share half stays unconditional")
        self.assertFalse(listeners(self.relay), "relay must stay down without a token file")
        self.assertIn("relay not set up on this host (run 'ferry relay' once to enable it); skipping", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "relay-token")))

    def test_opt_out(self):
        rc, out = self.call({"FERRY_SHARE_RELAY": "0"})
        self.assertEqual(rc, 0, out)
        self.assertIn("skipped", out)
        self.assertFalse(listeners(self.share) or listeners(self.relay))


class Callers(unittest.TestCase):
    def test_cmd_up_calls_helper_except_schematron(self):
        src = read(os.path.join(LIB, "ferry-serve.zsh"))
        up = src[src.index("\ncmd_up() {"):src.index("\ncmd_down() {")]
        self.assertEqual(up.count("_ferry_ensure_share_relay"), 1)
        self.assertRegex(up, r'\[\[ "\$LAUNCH_MODE" != "schematron" \]\]')
        self.assertIn("_ferry_ensure_share_relay ||", up)

    def test_built_ferry_in_sync(self):
        built = read(FERRY)
        self.assertIn("_ferry_ensure_share_relay()", built)

    def test_host_reset_ensures_relay_and_autostart(self):
        s = read(os.path.join(REPO, "host-reset.sh"))
        self.assertIn('RELAY_PORT="${FERRY_RELAY_PORT:-8098}"', s)
        i = s.index('ok "share server is on')
        blk = s[i:s.index("--- 5b.")]
        self.assertIn('"$FERRY_BIN" relay >/dev/null 2>&1', blk)
        self.assertIn("ferry-relay-marker", blk)
        self.assertNotIn("pkill", blk, "relay must never be restarted by host-reset")
        self.assertIn('"$HOME/.config/ferry/relay-token"', blk)
        self.assertIn("relay not set up on this host", blk)
        self.assertNotIn("die ", blk.split("relay_holder=")[1], "relay is optional: no die")
        self.assertIn("warn \"relay did not bind", blk)
        step7 = s.index("--- 7. Verify")
        failed = s.index("if [[ $RESET_FAILED -eq 1 ]]")
        inst = s.index('"$FERRY_BIN" autostart install --quiet ||')
        self.assertTrue(step7 < inst < failed, "autostart install must sit between step 7 and the RESET_FAILED exit")
        self.assertIn('"${FERRY_AUTOSTART:-1}" != "0"', s[step7:failed])
        self.assertIn("warning: could not install the login autostart", s[step7:failed])
        self.assertEqual(s.count("autostart install --quiet"), 1)


if __name__ == "__main__":
    unittest.main()
