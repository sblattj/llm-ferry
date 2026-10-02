#!/usr/bin/env python3
"""Stdlib unittest for `ferry tmux` — the host half of tmux-over-ssh-through-the-relay.

Run:  python3 lib/ferry-tmux.test.py

Spawns the real built `ferry` with a throwaway $HOME holding a hand-written relay
state file. `--print` is the seam: it prints the exact ssh argv instead of
exec'ing it, so no test ever opens a real ssh session.
"""
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")


def vnc_port():
    with open(FERRY) as f:
        return int(re.search(r'^VNC_PORT="(\d+)"', f.read(), re.M).group(1))


def entry(label, user="alice", bind="0.0.0.0", kind="tmux", client="10.0.0.7"):
    e = {"client": client, "label": label, "kind": kind,
         "since": "2026-10-02 09:00:00", "bind": bind}
    if user is not None:
        e["user"] = user
    return e


class TmuxTestBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-tmux-home-")
        self.tmp = tempfile.mkdtemp(prefix="ferry-tmux-tmp-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "ferry-logs"), exist_ok=True)
        self.cfg = os.path.join(self.home, ".config", "ferry")
        os.makedirs(self.cfg, exist_ok=True)
        self.state_path = os.path.join(self.cfg, "relay-published.json")

    def write_state(self, obj):
        with open(self.state_path, "w") as f:
            json.dump(obj, f)

    def ferry(self, *args):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["TMPDIR"] = self.tmp
        return subprocess.run(["zsh", FERRY, *args], env=e, capture_output=True,
                              text=True, timeout=60)

    def argv(self, *args):
        r = self.ferry("tmux", "--print", *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return shlex.split(r.stdout.strip())


class TmuxPrintTest(TmuxTestBase):
    def test_print_one_entry_gives_the_exact_argv(self):
        self.write_state({"8101": entry("laptop")})
        argv = self.argv()
        self.assertEqual(argv[:8], ["ssh", "-p", "8101", "-o", "HostKeyAlias=ferry-tmux-laptop",
                                    "-t", "alice@127.0.0.1", argv[7]])
        self.assertEqual(len(argv), 8)
        remote = argv[7]
        self.assertIn("/opt/homebrew/bin", remote)
        self.assertIn("exec tmux new-session -A -s ferry", remote)
        self.assertTrue(remote.endswith("-s ferry"))

    def test_wildcard_bind_uses_loopback(self):
        self.write_state({"8101": entry("laptop", bind="0.0.0.0")})
        self.assertIn("alice@127.0.0.1", self.argv())

    def test_missing_bind_uses_loopback(self):
        e = entry("laptop")
        del e["bind"]
        self.write_state({"8101": e})
        self.assertIn("alice@127.0.0.1", self.argv())

    def test_loopback_bind(self):
        self.write_state({"8101": entry("laptop", bind="127.0.0.1")})
        self.assertIn("alice@127.0.0.1", self.argv())

    def test_specific_bind_is_used(self):
        self.write_state({"8101": entry("laptop", bind="10.0.0.5")})
        self.assertIn("alice@10.0.0.5", self.argv())

    def test_session_and_user_overrides(self):
        self.write_state({"8101": entry("laptop")})
        argv = self.argv("--session", "work.1", "--user", "bob")
        self.assertIn("bob@127.0.0.1", argv)
        self.assertTrue(argv[-1].endswith("-s work.1"))

    def test_select_by_label_and_by_port(self):
        self.write_state({"8101": entry("alpha", user="u1"),
                          "8102": entry("beta", user="u2")})
        a = self.argv("beta")
        self.assertIn("8102", a)
        self.assertIn("u2@127.0.0.1", a)
        self.assertIn("HostKeyAlias=ferry-tmux-beta", a)
        b = self.argv("8101")
        self.assertIn("HostKeyAlias=ferry-tmux-alpha", b)
        self.assertIn("u1@127.0.0.1", b)

    def test_vnc_and_tcp_entries_are_ignored_for_selection(self):
        self.write_state({"5901": entry("screen", kind="vnc"),
                          "8101": entry("laptop"),
                          "4290": entry("dev", kind="tcp")})
        self.assertIn("8101", self.argv())  # the only tmux entry is picked

    def test_ambiguity_lists_both_and_fails(self):
        self.write_state({"8101": entry("alpha"), "8102": entry("beta")})
        r = self.ferry("tmux", "--print")
        self.assertEqual(r.returncode, 1, r.stdout)
        out = r.stdout + r.stderr
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertNotIn("Traceback", out)

    def test_unknown_client_fails_and_lists_what_exists(self):
        self.write_state({"8101": entry("alpha")})
        r = self.ferry("tmux", "nope", "--print")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("alpha", r.stdout + r.stderr)

    def test_no_entries_fails_with_the_client_hint(self):
        self.write_state({"5901": entry("screen", kind="vnc")})
        r = self.ferry("tmux", "--print")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ferry expose-tmux", r.stdout + r.stderr)

    def test_no_state_file_fails_with_the_client_hint(self):
        r = self.ferry("tmux", "--print")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ferry expose-tmux", r.stdout + r.stderr)

    def test_bad_session_is_rejected(self):
        self.write_state({"8101": entry("laptop")})
        r = self.ferry("tmux", "--print", "--session", "x;y")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_bad_user_is_rejected(self):
        self.write_state({"8101": entry("laptop")})
        r = self.ferry("tmux", "--print", "--user", "a b")
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_missing_user_without_flag_fails(self):
        self.write_state({"8101": entry("laptop", user=None)})
        r = self.ferry("tmux", "--print")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("--user", r.stdout + r.stderr)

    def test_missing_user_with_flag_works(self):
        self.write_state({"8101": entry("laptop", user=None)})
        self.assertIn("carol@127.0.0.1", self.argv("--user", "carol"))

    def test_unknown_flag_fails(self):
        r = self.ferry("tmux", "--bogus")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Unknown option", r.stdout + r.stderr)

    def test_help(self):
        r = self.ferry("tmux", "--help")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ferry tmux", r.stdout)

    def test_client_mode_is_refused(self):
        with open(os.path.join(self.cfg, "client.json"), "w") as f:
            f.write("{}")
        self.write_state({"8101": entry("laptop")})
        r = self.ferry("tmux", "--print")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Host Mac", r.stdout + r.stderr)


class TmuxListTest(TmuxTestBase):
    def test_list_none_prints_the_hint(self):
        self.write_state({})
        r = self.ferry("tmux", "--list")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ferry expose-tmux", r.stdout)

    def test_list_two_entries_ignores_vnc(self):
        self.write_state({"8101": entry("alpha", user="u1", client="10.0.0.7"),
                          "8102": entry("beta", user="u2", client="10.0.0.8"),
                          "5901": entry("screen", kind="vnc")})
        r = self.ferry("tmux", "--list")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertRegex(lines[0], r"alpha.*10\.0\.0\.7.*8101.*u1.*2026-10-02")
        self.assertRegex(lines[1], r"beta.*10\.0\.0\.8.*8102.*u2")
        self.assertNotIn("screen", r.stdout)


class StatusReportsTmuxTest(TmuxTestBase):
    def test_status_hints_ferry_tmux_for_a_tmux_entry(self):
        self.write_state({"8101": entry("laptop")})
        # cmd_status's tmux block reads the state file alone; no relay need be up.
        r = self.ferry("status")
        self.assertIn("ferry tmux laptop", r.stdout)

    def test_status_without_tmux_entries_prints_no_tmux_hint(self):
        self.write_state({"4290": entry("dev", kind="tcp")})
        r = self.ferry("status")
        self.assertNotIn("ferry tmux", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
