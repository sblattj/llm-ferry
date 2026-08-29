#!/usr/bin/env python3
"""Stdlib unittest for `ferry inbox` — reading client telemetry on the host.

Run:  python3 lib/ferry-inbox.test.py

The command's whole job is joining two files that each hold half the answer:
~/.config/ferry/client_logs.txt has the bodies and no timestamps, and the share
server's access log has the timestamps and is truncated on every restart. So the
tests here are mostly about the JOIN — that it aligns from the END, that it skips
receipts whose POST failed (no entry was written for those, and counting one would
shift every date by one), and that it says "undated" instead of guessing.

These run the REAL built `ferry` against a throwaway $HOME and $TMPDIR, because
the failure this suite exists to catch shipped once already: the receipt timestamp
is stamped by BaseHTTPRequestHandler as "28/Aug/2026 17:50:40" (a SPACE), the
first implementation parsed the Apache colon form, every line raised ValueError
and was skipped — and the listing still rendered perfectly, just with an empty
date column. Only running it against real data showed it.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
DELIM = "=== CLIENT LOG ENTRY ==="


def entries(*bodies):
    return "".join(f"{DELIM}\n{b}\n\n" for b in bodies)


def receipt(ip, stamp, code=200, path="/hq"):
    return f'{ip} - - [{stamp}] "POST {path} HTTP/1.1" {code} -\n'


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-inbox-home-")
        self.tmp = tempfile.mkdtemp(prefix="ferry-inbox-tmp-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.logdir = os.path.join(self.tmp, "ferry-logs")
        os.makedirs(self.logdir)
        os.makedirs(os.path.join(self.home, ".config", "ferry"))

    # --- helpers ------------------------------------------------------------
    def write_hq(self, text):
        with open(os.path.join(self.home, ".config", "ferry", "client_logs.txt"), "w") as f:
            f.write(text)

    def write_share_log(self, text, port=8095):
        with open(os.path.join(self.logdir, f"share-{port}.log"), "w") as f:
            f.write(text)

    def make_client(self):
        """A client profile is what flips ferry into CLIENT_MODE."""
        with open(os.path.join(self.home, ".config", "ferry", "client.json"), "w") as f:
            json.dump({"host": "somewhere.local", "port": "8090"}, f)

    def inbox(self, *args, expect_ok=True):
        env = os.environ.copy()
        env["HOME"] = self.home
        env["TMPDIR"] = self.tmp
        env.pop("OPENCODE_CONFIG", None)
        p = subprocess.run(["zsh", FERRY, "inbox", *args], env=env,
                           capture_output=True, text=True, timeout=120)
        if expect_ok:
            self.assertEqual(p.returncode, 0, f"ferry inbox {args} failed:\n{p.stdout}\n{p.stderr}")
        return p

    # --- the join -----------------------------------------------------------
    def test_it_dates_the_newest_entries_and_leaves_older_ones_undated(self):
        """k receipts belong to the k MOST RECENT entries — the log was truncated."""
        self.write_hq(entries("oldest", "older", "newer", "newest"))
        self.write_share_log(receipt("10.0.0.9", "28/Aug/2026 17:47:17")
                             + receipt("10.0.0.9", "28/Aug/2026 17:50:40"))

        out = self.inbox().stdout

        self.assertIn("4 entries", out)
        self.assertIn("(2 dated by the current share log)", out)
        newer = [l for l in out.splitlines() if "newer" in l][0]
        newest = [l for l in out.splitlines() if "newest" in l][0]
        self.assertIn("28/Aug 17:47", newer)
        self.assertIn("28/Aug 17:50", newest)
        self.assertIn("10.0.0.9", newest)
        # The two the log can't reach say so rather than borrowing a neighbour's date.
        oldest = [l for l in out.splitlines() if "oldest" in l][0]
        self.assertNotIn("28/Aug", oldest)
        self.assertIn("—", oldest)

    def test_the_space_separated_timestamp_parses(self):
        """The shipped-once bug: BaseHTTPRequestHandler uses a space, not a colon."""
        self.write_hq(entries("only one"))
        self.write_share_log(receipt("10.0.0.9", "28/Aug/2026 17:47:17"))
        self.assertIn("28/Aug 17:47", self.inbox().stdout)

    def test_the_apache_colon_timestamp_also_parses(self):
        self.write_hq(entries("only one"))
        self.write_share_log(receipt("10.0.0.9", "28/Aug/2026:17:47:17"))
        self.assertIn("28/Aug 17:47", self.inbox().stdout)

    def test_a_failed_post_is_reported_but_never_dates_an_entry(self):
        """A 500 means the handler raised and NO entry was written for it."""
        self.write_hq(entries("first", "second"))
        self.write_share_log(receipt("10.0.0.9", "28/Aug/2026 17:00:00", code=500)
                             + receipt("10.0.0.9", "28/Aug/2026 17:47:17")
                             + receipt("10.0.0.9", "28/Aug/2026 17:50:40"))

        out = self.inbox().stdout

        self.assertIn("1 POST(s) to /hq returned an error", out)
        self.assertIn("(2 dated by the current share log)", out)
        # Counting the 500 would have slid both dates one entry earlier.
        self.assertIn("28/Aug 17:47", [l for l in out.splitlines() if "first" in l][0])
        self.assertIn("28/Aug 17:50", [l for l in out.splitlines() if "second" in l][0])

    def test_it_reads_every_share_log_not_just_the_default_port(self):
        """`ferry share` scans upward when the port is taken, so the name varies."""
        self.write_hq(entries("one"))
        self.write_share_log(receipt("10.0.0.9", "28/Aug/2026 09:15:00"), port=8097)
        self.assertIn("28/Aug 09:15", self.inbox().stdout)

    # --- listing behaviour --------------------------------------------------
    def test_an_empty_body_is_shown_as_such(self):
        self.write_hq(entries("", "after the empty one"))
        self.assertIn("(empty body)", self.inbox().stdout)

    def test_default_caps_at_twenty_and_all_lifts_it(self):
        self.write_hq(entries(*[f"entry number {i}" for i in range(1, 26)]))

        capped = self.inbox().stdout
        self.assertNotIn("entry number 1\n", capped)
        self.assertIn("entry number 25", capped)
        self.assertIn("5 older entries not shown", capped)

        listed = [l.split("  ")[-1] for l in self.inbox("--all").stdout.splitlines()]
        self.assertIn("entry number 1", listed)

    def test_n_prints_whole_bodies_with_a_header(self):
        self.write_hq(entries("one-liner", "line one\nline two\nline three"))
        out = self.inbox("-n", "1").stdout
        self.assertIn("===== entry 2", out)
        self.assertIn("line three", out)
        self.assertNotIn("one-liner", out)

    def test_path_prints_both_files(self):
        out = self.inbox("--path").stdout
        self.assertIn(os.path.join(self.home, ".config/ferry/client_logs.txt"), out)
        self.assertIn("share-8095.log", out)

    def test_no_telemetry_yet_is_explained_not_crashed(self):
        out = self.inbox().stdout
        self.assertIn("No client telemetry yet", out)
        self.assertIn("ferry share", out)

    def test_the_legacy_checkout_copy_is_called_out(self):
        """A pre-1.8.10 client_logs.txt sits beside the checkout and is frozen.

        Reading that one instead of the live file is the trap this note exists to
        stop, so the note itself is worth a test. The fixture is gitignored, and it
        is removed again unless the checkout already had one.
        """
        self.write_hq(entries("live entry"))
        legacy = os.path.join(REPO, "client_logs.txt")
        pre_existing = os.path.exists(legacy)
        if not pre_existing:
            with open(legacy, "w") as f:
                f.write(f"{DELIM}\nfrom the old path\n\n")
            self.addCleanup(os.remove, legacy)
        self.assertIn("frozen pre-1.8.10 history", self.inbox().stdout)

    def test_an_unknown_flag_is_refused(self):
        p = self.inbox("--wat", expect_ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Unknown option", p.stdout + p.stderr)

    # --- host-only ----------------------------------------------------------
    def test_a_client_is_told_the_inbox_lives_on_the_host(self):
        self.make_client()
        p = self.inbox(expect_ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("only available on the LLM-Ferry Host", p.stdout + p.stderr)


class PathContractTest(unittest.TestCase):
    """The telemetry path is written in two places and they must not drift.

    The share server runs as its own python process from a heredoc, so it carries
    the path as a literal; `ferry inbox` reads $HQ_LOG from the shell. Two spellings
    of one path is exactly how a reader ends up tailing a file nothing writes.
    """

    def test_hq_log_matches_the_share_handlers_literal(self):
        with open(FERRY) as f:
            built = f.read()
        self.assertIn('HQ_LOG="$HOME/.config/ferry/client_logs.txt"', built)
        self.assertIn('CLIENT_LOG = os.path.expanduser("~/.config/ferry/client_logs.txt")', built)

    def test_the_delimiter_matches_what_the_handler_writes(self):
        with open(FERRY) as f:
            built = f.read()
        self.assertIn('=== CLIENT LOG ENTRY ===', built)
        self.assertIn('DELIM = "=== CLIENT LOG ENTRY ==="', built)


if __name__ == "__main__":
    unittest.main(verbosity=2)
