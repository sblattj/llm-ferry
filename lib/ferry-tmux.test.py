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


class TmuxKeyAndUserSshdTest(TmuxTestBase):
    def make_key(self):
        path = os.path.join(self.cfg, "tmux_ed25519")
        with open(path, "w") as f:
            f.write("not a real key; only its existence is read\n")
        return path

    def test_no_identity_flag_without_the_key_file(self):
        self.write_state({"8101": entry("laptop")})
        argv = self.argv()
        self.assertNotIn("-i", argv)
        self.assertEqual(len(argv), 8)

    def test_identity_flag_appears_with_the_key_file(self):
        key = self.make_key()
        self.write_state({"8101": entry("laptop")})
        argv = self.argv()
        self.assertEqual(argv[argv.index("-i") + 1], key)
        self.assertNotIn("IdentitiesOnly=yes", argv)
        self.assertNotIn("-o IdentitiesOnly", " ".join(argv))
        self.assertEqual(argv[-2], "alice@127.0.0.1")

    def test_user_sshd_entry_gets_its_own_host_key_alias(self):
        e = entry("laptop")
        e["sshd"] = "user"
        self.write_state({"8101": e})
        self.assertIn("HostKeyAlias=ferry-tmux-laptop-user", self.argv())

    def test_system_sshd_entry_keeps_the_plain_alias(self):
        e = entry("laptop")
        e["sshd"] = "something-else"
        self.write_state({"8101": e})
        self.assertIn("HostKeyAlias=ferry-tmux-laptop", self.argv())


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


class TmuxDirTest(TmuxTestBase):
    def remote(self, *args):
        self.write_state({"8101": entry("laptop")})
        return self.argv(*args)[-1]

    def operand(self, remote):
        self.assertIn(" -s ferry -c ", remote)
        return remote.split(" -s ferry -c ", 1)[1]

    def shell(self, operand):
        return subprocess.run(["sh", "-c", "HOME=/h; printf %s " + operand],
                              capture_output=True, text=True, timeout=20)

    def test_without_dir_the_remote_is_unchanged(self):
        self.assertEqual(self.remote(),
                         "PATH=/opt/homebrew/bin:/usr/local/bin:$PATH exec tmux new-session -A -s ferry")

    def test_dir_with_space_is_single_quoted(self):
        remote = self.remote("--dir", "/tmp/x y")
        self.assertTrue(remote.endswith("-s ferry -c '/tmp/x y'"), remote)

    def test_tilde_is_the_remote_home(self):
        self.assertTrue(self.remote("--dir", "~").endswith('-s ferry -c "$HOME"'))
        self.assertTrue(self.remote("--dir", "~/code/foo").endswith('-s ferry -c "$HOME"/code/foo'))

    def test_tilde_expands_in_a_real_shell(self):
        r = self.shell(self.operand(self.remote("--dir", "~/code/foo")))
        self.assertEqual(r.stdout, "/h/code/foo", r.stderr)
        r = self.shell(self.operand(self.remote("--dir", "~")))
        self.assertEqual(r.stdout, "/h", r.stderr)
        r = self.shell(self.operand(self.remote("--dir", "~/my dir/$X")))
        self.assertEqual(r.stdout, "/h/my dir/$X", r.stderr)

    def test_hostile_value_stays_literal(self):
        marker = "/tmp/PWNED-%s" % os.urandom(6).hex()
        self.addCleanup(lambda: os.path.exists(marker) and os.remove(marker))
        value = "/tmp/a;touch " + marker
        r = self.shell(self.operand(self.remote("--dir", value)))
        self.assertEqual(r.stdout, value, r.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_bad_values_are_rejected(self):
        self.write_state({"8101": entry("laptop")})
        for bad in ("", "/tmp/a\nb", "/tmp/a\rb", "~bob/x", "~bob"):
            r = self.ferry("tmux", "--print", "--dir", bad)
            self.assertEqual(r.returncode, 1, repr(bad))
            self.assertIn("Error:", r.stdout, repr(bad))

    def test_dir_needs_a_value(self):
        r = self.ferry("tmux", "--dir")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Error: --dir needs a path", r.stdout)


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
