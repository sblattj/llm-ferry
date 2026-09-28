#!/usr/bin/env python3
"""`ferry keys` — the host CLI over the device-key store.

Run:  python3 lib/ferry-keys.test.py

The Python CLI (front/ferry_keys_cli.py) is exercised directly with
FERRY_KEYS_FILE / FERRY_KEYS_DB in a temp dir. The zsh wrapper
(lib/ferry-keys.zsh) is exercised through a tiny harness that sources ONLY that
module, so the built monolith's host-mode load path is never run on this
machine; the built `ferry` is run only in CLIENT mode (a temp HOME holding a
client.json), where it must refuse.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "front", "ferry_keys_cli.py")
MODULE = os.path.join(REPO, "lib", "ferry-keys.zsh")
FERRY = os.path.join(REPO, "ferry")
sys.path.insert(0, os.path.join(REPO, "front"))

import ferry_keys_usage as U  # noqa: E402

TOKEN_RE = re.compile(r"^fk-[a-z0-9-]+-[a-z2-7]{32}$")


class CliCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-keys-cli-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.keys = os.path.join(self.dir, "keys.json")
        self.db = os.path.join(self.dir, "keys-usage.sqlite")
        self.env = dict(os.environ, FERRY_KEYS_FILE=self.keys, FERRY_KEYS_DB=self.db,
                        HOME=self.dir)

    def cli(self, *args):
        return subprocess.run([sys.executable, CLI, *args], capture_output=True,
                              text=True, env=self.env, timeout=30)

    def doc(self):
        with open(self.keys) as fh:
            return json.load(fh)


class TestAdd(CliCase):
    def test_token_alone_on_stdout_and_only_its_hash_stored(self):
        p = self.cli("add", "MBP Work", "--rpm", "30", "--lanes", "flash,heavy",
                     "--budget-tokens", "100000", "--expires", "2027-01-01")
        self.assertEqual(p.returncode, 0, p.stderr)
        token = p.stdout.strip()
        self.assertRegex(token, TOKEN_RE)
        self.assertEqual(p.stdout, token + "\n")
        self.assertNotIn(token, p.stderr)
        self.assertIn("mbp-work", p.stderr)
        entry = self.doc()["keys"][0]
        self.assertEqual((entry["name"], entry["rpm"], entry["lanes"], entry["budget_tokens"],
                          entry["expires"]),
                         ("mbp-work", 30, ["flash", "heavy"], 100000, "2027-01-01T00:00:00Z"))
        with open(self.keys) as fh:
            self.assertNotIn(token, fh.read())

    def test_duplicate_is_exit_1_and_replace_rotates(self):
        first = self.cli("add", "laptop").stdout.strip()
        p = self.cli("add", "laptop")
        self.assertEqual(p.returncode, 1)
        self.assertIn("ferry keys:", p.stderr)
        self.assertIn("already exists", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        p = self.cli("add", "laptop", "--replace")
        second = p.stdout.strip()
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.doc()["keys"]), 1)
        self.assertIn("rotated 'laptop'", p.stderr)
        self.assertNotIn("created", p.stderr)

    def test_replace_of_a_new_name_says_created(self):
        p = self.cli("add", "fresh", "--replace")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("created 'fresh'", p.stderr)
        self.assertNotIn("rotated", p.stderr)

    def test_replace_reactivates_a_revoked_name(self):
        self.cli("add", "laptop")
        self.cli("revoke", "laptop")
        p = self.cli("add", "laptop", "--replace")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("rotated 'laptop'", p.stderr)
        self.assertIsNone(self.doc()["keys"][0]["revoked"])

    def test_bad_limits_are_usage_errors(self):
        for args in (("--rpm", "0"), ("--rpm", "x"), ("--budget-tokens", "-5"),
                     ("--expires", "tomorrow"), ("--lanes", ",")):
            with self.subTest(args=args):
                p = self.cli("add", "x", *args)
                self.assertEqual(p.returncode, 2)
                self.assertIn("error: argument " + args[0], p.stderr)
        self.assertFalse(os.path.exists(self.keys))


class TestListRevokeSet(CliCase):
    def test_list_shows_status_limits_and_usage_but_no_secrets(self):
        token = self.cli("add", "laptop", "--rpm", "5").stdout.strip()
        self.cli("add", "old")
        self.cli("revoke", "old")
        U.Usage(self.db).add_tokens("laptop", 1234)
        p = self.cli("list")
        self.assertEqual(p.returncode, 0, p.stderr)
        header, *rows = p.stdout.splitlines()
        self.assertEqual(header.split(), ["NAME", "STATUS", "CREATED", "EXPIRES", "LANES",
                                          "RPM", "BUDGET", "TOKENS(MONTH)", "REQ(LAST", "MIN)"])
        laptop = next(r for r in rows if r.startswith("laptop"))
        self.assertEqual(laptop.split()[1], "active")
        self.assertEqual(laptop.split()[5], "5")
        self.assertEqual(laptop.split()[7], "1234")
        self.assertEqual(next(r for r in rows if r.startswith("old")).split()[1], "revoked")
        self.assertNotIn(token, p.stdout)
        self.assertNotRegex(p.stdout, r"[0-9a-f]{64}")

    def test_empty_store_says_how_to_start(self):
        p = self.cli("list")
        self.assertEqual(p.returncode, 0)
        self.assertIn("ferry keys add", p.stdout)
        self.assertFalse(os.path.exists(self.db))

    def test_corrupt_store_is_a_one_line_error(self):
        with open(self.keys, "w") as fh:
            fh.write("{nope")
        p = self.cli("list")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stderr.startswith("ferry keys:"), p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_revoke_unknown_is_exit_1(self):
        p = self.cli("revoke", "ghost")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no key named", p.stderr)

    def test_confirmations_print_the_stored_name(self):
        self.cli("add", "MBP Work")
        p = self.cli("set", "MBP Work", "--rpm", "7")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("'mbp-work'", p.stderr)
        self.assertNotIn("MBP Work", p.stderr)
        p = self.cli("revoke", "MBP Work")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("'mbp-work'", p.stderr)
        self.assertNotIn("MBP Work", p.stderr)

    def test_set_changes_and_clears_limits(self):
        self.cli("add", "laptop", "--rpm", "5")
        self.assertEqual(self.cli("set", "laptop", "--rpm", "none",
                                  "--budget-tokens", "900").returncode, 0)
        entry = self.doc()["keys"][0]
        self.assertIsNone(entry["rpm"])
        self.assertEqual(entry["budget_tokens"], 900)
        self.assertEqual(self.cli("set", "laptop").returncode, 2)


class TestZshWrapper(CliCase):
    def harness(self, client_mode):
        path = os.path.join(self.dir, "harness.zsh")
        with open(path, "w") as fh:
            fh.write('APP_DIR="%s"\nCLIENT_MODE=%d\nsource "%s"\ncmd_keys "$@"\n'
                     % (REPO, client_mode, MODULE))
        return path

    def run_zsh(self, client_mode, *args):
        return subprocess.run(["zsh", self.harness(client_mode), *args],
                              capture_output=True, text=True, env=self.env, timeout=30)

    def test_host_mode_runs_the_cli(self):
        p = self.run_zsh(0, "add", "laptop")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout.strip(), TOKEN_RE)
        self.assertEqual(self.run_zsh(0, "revoke", "ghost").returncode, 1)

    def test_client_mode_refuses_before_touching_anything(self):
        p = self.run_zsh(1, "add", "laptop")
        self.assertEqual(p.returncode, 1)
        self.assertIn("runs on the host", p.stderr)
        self.assertFalse(os.path.exists(self.keys))

    def test_no_arguments_prints_usage_and_exits_1(self):
        p = self.run_zsh(0)
        self.assertEqual(p.returncode, 1)
        self.assertIn("ferry keys add", p.stdout)

    def test_built_ferry_on_a_client_refuses_and_help_writes_nothing(self):
        cfg = os.path.join(self.dir, ".config", "ferry")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "client.json"), "w") as fh:
            json.dump({"host": "127.0.0.1", "port": "1", "share_port": "1",
                       "name": "laptop"}, fh)
        p = subprocess.run(["zsh", FERRY, "keys", "list"], capture_output=True, text=True,
                           env=self.env, cwd=REPO, timeout=30)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("runs on the host", p.stderr)
        p = subprocess.run(["zsh", FERRY, "keys", "--help"], capture_output=True, text=True,
                           env=self.env, cwd=REPO, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertIn("ferry keys add", p.stdout)
        self.assertFalse(os.path.exists(self.keys))


if __name__ == "__main__":
    unittest.main(verbosity=2)
