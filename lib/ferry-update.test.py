#!/usr/bin/env python3
"""Stdlib unittest for `ferry update` — the role-aware catch-up front door.

Run:  python3 lib/ferry-update.test.py

`ferry update` owns no update logic of its own. It answers ONE question — is this
machine a host or a client — and then runs the catch-up path that already exists
for that role: host-reset.sh out of the checkout, or client-reset.sh curled from
the host. So these tests are about the DECISION and the command it produces, not
about resetting anything.

Every test runs with --dry-run, deliberately. The real host path bounces the
route proxy and the real client path curls a LAN host; a suite that executed
either would take down the developer's own stack the first time it ran. --dry-run
exists as much for this file as for the user, and the control test below asserts
it really is inert by checking a canary the real path would have destroyed.

These run the REAL built `ferry` against a throwaway $HOME, because role
detection reads $HOME/.config/ferry/client.json and nothing else — the one thing
a mocked test could get wrong while still passing.
"""
import json
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".config", "ferry"))

    def run_update(self, *args, expect_rc=0):
        env = dict(os.environ, HOME=self.home, TMPDIR=self.tmp)
        p = subprocess.run([FERRY, "update", *args], env=env,
                           capture_output=True, text=True)
        if expect_rc is not None:
            self.assertEqual(p.returncode, expect_rc,
                             f"rc={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}")
        return p.stdout + p.stderr

    def make_client(self, host="ferry-host.local", port=None, share_port=None):
        prof = {"host": host}
        if port is not None:
            prof["port"] = port
        if share_port is not None:
            prof["share_port"] = share_port
        with open(os.path.join(self.home, ".config", "ferry", "client.json"), "w") as f:
            json.dump(prof, f)

    # ── role detection ────────────────────────────────────────────────────
    def test_no_client_json_is_host(self):
        """Absent client.json => host. This is ferry's existing rule, not a new one."""
        out = self.run_update("--dry-run")
        self.assertIn("host", out.lower())
        self.assertIn("host-reset.sh", out)

    def test_client_json_present_is_client(self):
        self.make_client()
        out = self.run_update("--dry-run")
        self.assertIn("client-reset.sh", out)
        self.assertNotIn("host-reset.sh", out)

    def test_client_command_targets_the_configured_host_and_share_port(self):
        self.make_client(host="box.local", share_port="9999")
        out = self.run_update("--dry-run")
        self.assertIn("box.local:9999/client-reset.sh", out)

    def test_client_share_port_defaults_to_8095(self):
        self.make_client(host="box.local")
        out = self.run_update("--dry-run")
        self.assertIn("box.local:8095/client-reset.sh", out)

    # ── forcing a role ────────────────────────────────────────────────────
    def test_force_host_on_a_client_machine(self):
        self.make_client()
        out = self.run_update("--dry-run", "--host")
        self.assertIn("host-reset.sh", out)
        self.assertNotIn("client-reset.sh", out)

    def test_force_client_on_a_host_machine_without_a_profile_errors(self):
        """--client with no host to talk to must say so, not curl a blank URL."""
        out = self.run_update("--dry-run", "--client", expect_rc=1)
        self.assertNotIn("http:///", out)
        self.assertIn("client.json", out)

    def test_client_profile_without_a_host_key_errors(self):
        with open(os.path.join(self.home, ".config", "ferry", "client.json"), "w") as f:
            json.dump({"port": "8090"}, f)
        out = self.run_update("--dry-run", expect_rc=1)
        self.assertNotIn("http:///", out)

    # ── flags ─────────────────────────────────────────────────────────────
    def test_full_is_passed_through_on_the_host(self):
        out = self.run_update("--dry-run", "--full")
        self.assertIn("host-reset.sh --full", out)

    def test_full_is_rejected_on_a_client(self):
        """--full reloads GPU lanes, which a client does not have."""
        self.make_client()
        out = self.run_update("--dry-run", "--full", expect_rc=1)
        self.assertIn("--full", out)

    def test_unknown_flag_errors(self):
        self.run_update("--dry-run", "--nonsense", expect_rc=1)

    # ── the control: --dry-run must actually be inert ──────────────────────
    def test_dry_run_touches_nothing(self):
        """A check that cannot fail proves nothing.

        The real host path rebuilds `ferry` and re-links ~/.local/bin/ferry. Drop
        a canary at that link path inside the throwaway $HOME: a --dry-run that
        silently executed the reset would replace or remove it.
        """
        binp = os.path.join(self.home, ".local", "bin")
        os.makedirs(binp)
        canary = os.path.join(binp, "ferry")
        with open(canary, "w") as f:
            f.write("CANARY")
        self.run_update("--dry-run")
        with open(canary) as f:
            self.assertEqual(f.read(), "CANARY",
                             "--dry-run executed the real reset path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
