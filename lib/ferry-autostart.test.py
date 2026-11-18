#!/usr/bin/env python3
"""Stdlib unittest for `ferry autostart` / `ferry boot` (host survives a reboot).

Run:  python3 lib/ferry-autostart.test.py

Runs the REAL built ./ferry against a throwaway $HOME. launchctl is replaced by
a stub (FERRY_LAUNCHCTL) that logs its argv, and the LaunchAgents directory is a
temp dir (FERRY_LAUNCHAGENTS_DIR). Nothing here touches the real launchctl, and
`ferry boot` is only ever run with --dry-run, so no real `ferry up` happens.
"""
import os
import plistlib
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
LABEL = "com.llm-ferry.host"


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".config", "ferry"))
        self.agents = os.path.join(self.tmp, "LaunchAgents")
        self.plist = os.path.join(self.agents, LABEL + ".plist")
        self.log = os.path.join(self.tmp, "launchctl.log")
        self.printrc = os.path.join(self.tmp, "print-rc")
        self.stub = os.path.join(self.tmp, "launchctl-stub")
        with open(self.stub, "w") as f:
            f.write('#!/bin/sh\n'
                    'echo "$@" >> "%s"\n'
                    'if [ "$1" = print ]; then exit "$(cat "%s" 2>/dev/null || echo 1)"; fi\n'
                    'exit 0\n' % (self.log, self.printrc))
        os.chmod(self.stub, 0o755)
        self.set_print_rc(1)

    def set_print_rc(self, rc):
        with open(self.printrc, "w") as f:
            f.write(str(rc))

    def run_ferry(self, *args, expect_rc=0):
        env = dict(os.environ, HOME=self.home, TMPDIR=self.tmp,
                   FERRY_LAUNCHAGENTS_DIR=self.agents, FERRY_LAUNCHCTL=self.stub)
        p = subprocess.run([FERRY, *args], env=env, capture_output=True, text=True, timeout=60)
        if expect_rc is not None:
            self.assertEqual(p.returncode, expect_rc,
                             f"rc={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}")
        return p.stdout + p.stderr

    def stub_calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [l.strip() for l in f if l.strip()]

    def test_install_writes_valid_plist_and_bootstraps(self):
        self.run_ferry("autostart", "install")
        self.assertTrue(os.path.exists(self.plist))
        lint = subprocess.run(["plutil", "-lint", self.plist], capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        with open(self.plist, "rb") as f:
            d = plistlib.load(f)
        self.assertEqual(d["Label"], LABEL)
        self.assertEqual(d["ProgramArguments"][1:], ["boot"])
        self.assertTrue(d["RunAtLoad"])
        self.assertIs(d["AbandonProcessGroup"], True)
        self.assertIs(d["KeepAlive"], False)
        self.assertTrue(d["EnvironmentVariables"]["PATH"])
        self.assertEqual(d["EnvironmentVariables"]["HOME"], self.home)
        self.assertTrue(d["StandardOutPath"].endswith("/.config/ferry/boot.log"))
        calls = self.stub_calls()
        self.assertTrue(any(c.startswith("bootout gui/") and c.endswith(LABEL) for c in calls), calls)
        self.assertTrue(any(c.startswith("bootstrap gui/") and c.endswith(self.plist) for c in calls), calls)

    def test_second_install_is_a_noop_when_loaded(self):
        self.run_ferry("autostart", "install")
        self.set_print_rc(0)
        os.remove(self.log)
        out = self.run_ferry("autostart", "install")
        self.assertIn("already installed", out)
        self.assertFalse(any(c.startswith("bootstrap") for c in self.stub_calls()), self.stub_calls())

    def test_install_again_when_not_loaded_rebootstraps(self):
        self.run_ferry("autostart", "install")
        os.remove(self.log)
        self.run_ferry("autostart", "install")  # print still rc 1 => not loaded
        self.assertTrue(any(c.startswith("bootstrap") for c in self.stub_calls()))

    def test_uninstall_removes_plist(self):
        self.run_ferry("autostart", "install")
        self.assertTrue(os.path.exists(self.plist))
        self.run_ferry("autostart", "uninstall")
        self.assertFalse(os.path.exists(self.plist))
        self.assertTrue(any(c.startswith("bootout") for c in self.stub_calls()))

    def test_status_reports(self):
        out = self.run_ferry("autostart", "status")
        self.assertIn("installed: no", out)
        self.run_ferry("autostart", "install")
        out = self.run_ferry("autostart", "status")
        self.assertIn("installed: yes", out)

    def test_client_mode_refuses(self):
        with open(os.path.join(self.home, ".config", "ferry", "client.json"), "w") as f:
            f.write('{"host": "ferry-host.example"}')
        out = self.run_ferry("autostart", "install", expect_rc=1)
        self.assertIn("client", out)
        self.assertFalse(os.path.exists(self.plist))
        self.assertEqual(self.stub_calls(), [])

    def test_install_dry_run_changes_nothing(self):
        out = self.run_ferry("autostart", "install", "--dry-run")
        self.assertIn(LABEL, out)
        self.assertIn("AbandonProcessGroup", out)
        self.assertFalse(os.path.exists(self.plist))
        self.assertEqual(self.stub_calls(), [])

    def test_boot_dry_run_prints_steps_and_changes_nothing(self):
        out = self.run_ferry("boot", "--dry-run")
        self.assertIn("[dry-run]", out)
        self.assertIn("up", out)
        self.assertEqual(self.stub_calls(), [])
        self.assertFalse(os.path.exists(self.plist))

class BootGuardsTest(unittest.TestCase):
    """Real `ferry boot` (no --dry-run) in a sandbox HOME. Never reaches `ferry up`:
    the client case returns first, the no-config case skips up. The live :8090 may
    be listening here, so the second case accepts either branch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".config", "ferry"))
        self.ports = []

    def boot(self, extra_env=None):
        env = dict(os.environ, HOME=self.home, TMPDIR=self.tmp)
        env.update(extra_env or {})
        t0 = time.time()
        p = subprocess.run([FERRY, "boot"], env=env, capture_output=True, text=True, timeout=120)
        return p, time.time() - t0

    def tearDown(self):
        for port in self.ports:
            r = subprocess.run(["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                               capture_output=True, text=True)
            for pid in [int(x) for x in r.stdout.split()]:
                if pid != os.getpid():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_client_mode_boot_is_a_fast_noop(self):
        with open(os.path.join(self.home, ".config", "ferry", "client.json"), "w") as f:
            f.write('{"host": "192.0.2.10", "port": 8090}')
        p, dt = self.boot()
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("this machine is a client now; remove the agent with: ferry autostart uninstall", out)
        self.assertNotIn("starting the endpoint", out)
        self.assertNotIn("network up", out)
        self.assertLess(dt, 5, f"client boot took {dt:.1f}s")

    def test_no_route_config_does_not_run_up(self):
        ports = []
        for _ in range(2):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
            s.close()
        self.ports = ports
        p, _ = self.boot({"FERRY_SHARE_PORT": str(ports[0]), "FERRY_RELAY_PORT": str(ports[1])})
        out = p.stdout + p.stderr
        self.assertNotIn("starting the endpoint (ferry up)", out, out)
        self.assertNotIn("ferry up failed", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "litellm.yaml")))
        self.assertIn("relay not set up on this host", out)
        r = subprocess.run(["lsof", "-t", "-nP", f"-iTCP:{ports[1]}", "-sTCP:LISTEN"],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "", "relay must stay down without a token")
        if "endpoint already up" in out:
            self.assertNotIn("no route config", out)
            self.assertIn("boot ok", out, out)
            self.assertEqual(p.returncode, 0, out)
        else:
            self.assertIn("no route config yet; run ferry up by hand once", out)
            self.assertIn("boot finished with problems", out)
            self.assertEqual(p.returncode, 1, out)


if __name__ == "__main__":
    unittest.main()
