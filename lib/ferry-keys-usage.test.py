#!/usr/bin/env python3
"""Stdlib unittest for front/ferry_keys_usage.py — RPM and monthly token counters.

Run:  python3 lib/ferry-keys-usage.test.py

The front door runs FERRY_FRONT_WORKERS (default 4) processes, so the counters
live in one shared SQLite file. The two-process test below is the one that
proves an RPM limit is a host-wide limit and not a per-worker one.

Every test points FERRY_KEYS_DB / FERRY_KEYS_FILE at a temp dir (inherited by
the worker subprocesses), so the real ~/.config/ferry files are never touched.
"""
import calendar
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONT = os.path.join(REPO, "front")
sys.path.insert(0, FRONT)

import ferry_keys_usage as U  # noqa: E402

# 2026-09-27T12:00:30Z — mid-minute, mid-month.
NOW = calendar.timegm((2026, 9, 27, 12, 0, 30))


class UsageCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-keys-usage-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "keys-usage.sqlite")
        patcher = mock.patch.dict(os.environ, {
            "FERRY_KEYS_DB": self.path,
            "FERRY_KEYS_FILE": os.path.join(self.dir, "keys.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.u = U.Usage(self.path)


class TestAdmit(UsageCase):
    def test_unlimited_key_is_admitted_and_counted(self):
        for _ in range(3):
            self.assertEqual(self.u.admit("a", now=NOW), U.Verdict(True, "", 0))
        self.assertEqual(self.u.minute_requests("a", now=NOW), 3)

    def test_rpm_limit_refuses_and_does_not_count_the_refusal(self):
        self.assertTrue(self.u.admit("a", rpm=2, now=NOW).ok)
        self.assertTrue(self.u.admit("a", rpm=2, now=NOW).ok)
        v = self.u.admit("a", rpm=2, now=NOW)
        self.assertEqual((v.ok, v.reason, v.retry_after), (False, "rpm", 30))
        self.assertEqual(self.u.minute_requests("a", now=NOW), 2)

    def test_rpm_window_rolls_over(self):
        self.u.admit("a", rpm=1, now=NOW)
        self.assertFalse(self.u.admit("a", rpm=1, now=NOW).ok)
        self.assertTrue(self.u.admit("a", rpm=1, now=NOW + 60).ok)

    def test_budget_refuses_at_or_over_and_retries_next_month(self):
        self.u.add_tokens("a", 100, now=NOW)
        v = self.u.admit("a", budget_tokens=100, now=NOW)
        self.assertEqual((v.ok, v.reason), (False, "budget"))
        self.assertEqual(v.retry_after,
                         calendar.timegm((2026, 10, 1, 0, 0, 0)) - NOW)
        self.assertTrue(self.u.admit("a", budget_tokens=101, now=NOW).ok)

    def test_rpm_is_checked_before_budget(self):
        self.u.add_tokens("a", 100, now=NOW)
        self.u.admit("a", now=NOW)
        v = self.u.admit("a", rpm=1, budget_tokens=10, now=NOW)
        self.assertEqual(v.reason, "rpm")

    def test_keys_are_independent(self):
        self.u.admit("a", rpm=1, now=NOW)
        self.assertTrue(self.u.admit("b", rpm=1, now=NOW).ok)


class TestTokens(UsageCase):
    def test_tokens_accumulate_per_month(self):
        self.u.add_tokens("a", 10, now=NOW)
        self.u.add_tokens("a", 5, now=NOW)
        self.assertEqual(self.u.month_tokens("a", now=NOW), 15)
        october = calendar.timegm((2026, 10, 2, 0, 0, 0))
        self.assertEqual(self.u.month_tokens("a", now=october), 0)

    def test_december_rolls_to_january(self):
        dec = calendar.timegm((2026, 12, 31, 23, 59, 0))
        self.assertEqual(U.month_of(dec), 202612)
        self.assertEqual(U.seconds_to_next_month(dec), 60)

    def test_prune_drops_old_rows(self):
        self.u.admit("a", now=NOW - 600)
        self.u.add_tokens("a", 7, now=calendar.timegm((2025, 1, 5, 0, 0, 0)))
        self.u.admit("a", now=NOW)
        self.u.add_tokens("a", 1, now=NOW)
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            minutes = conn.execute("SELECT minute_epoch FROM minute").fetchall()
            months = conn.execute("SELECT yyyymm FROM month").fetchall()
        finally:
            conn.close()
        self.assertEqual(minutes, [(int(NOW // 60),)])
        self.assertEqual(months, [(202609,)])

    def test_summary(self):
        self.u.admit("a", now=NOW)
        self.u.add_tokens("b", 9, now=NOW)
        self.assertEqual(self.u.summary(now=NOW), {
            "a": {"month_tokens": 0, "minute_requests": 1},
            "b": {"month_tokens": 9, "minute_requests": 0}})

    def test_summary_of_a_missing_file_does_not_create_it(self):
        self.assertEqual(self.u.summary(now=NOW), {})
        self.assertFalse(os.path.exists(self.path))


class TestFiles(UsageCase):
    def test_db_is_0600_and_wal(self):
        self.u.admit("a", now=NOW)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

    def permissive_umask(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_fresh_db_and_its_wal_sidecars_are_0600_under_a_loose_umask(self):
        import sqlite3
        self.permissive_umask()
        self.u.admit("a", now=NOW)
        # SQLite deletes -wal/-shm when the last connection closes; an open
        # reader keeps them on disk so their modes can be observed.
        hold = sqlite3.connect(self.path)
        self.addCleanup(hold.close)
        hold.execute("SELECT count(*) FROM minute").fetchone()
        self.u.add_tokens("a", 3, now=NOW)
        self.assertEqual(self.mode(self.path), 0o600)
        for suffix in ("-wal", "-shm"):
            self.assertTrue(os.path.exists(self.path + suffix), suffix)
            self.assertEqual(self.mode(self.path + suffix), 0o600, suffix)

    def test_a_db_recreated_after_a_heal_is_0600(self):
        # A cached Usage whose file was deleted lets sqlite3.connect recreate
        # it under the umask (0644 here); the fresh Usage the front builds
        # after the failure must tighten it back to 0600.
        self.permissive_umask()
        self.u.admit("a", now=NOW)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        with self.assertRaises(U.UsageError):
            self.u.admit("a", now=NOW)  # "no such table", file recreated
        self.assertEqual(self.mode(self.path), 0o644)  # control: the stale mode
        # A reader holding the file open leaves 0644 sidecars behind.
        import sqlite3
        hold = sqlite3.connect(self.path)
        self.addCleanup(hold.close)
        hold.execute("PRAGMA journal_mode=WAL").fetchone()
        hold.execute("SELECT count(*) FROM sqlite_master").fetchone()
        self.assertEqual(self.mode(self.path + "-shm"), 0o644)  # control
        fresh = U.Usage(self.path)
        self.assertTrue(fresh.admit("a", now=NOW).ok)
        self.assertEqual(self.mode(self.path), 0o600)
        for suffix in ("-wal", "-shm"):
            self.assertTrue(os.path.exists(self.path + suffix), suffix)
            self.assertEqual(self.mode(self.path + suffix), 0o600, suffix)

    def test_unopenable_db_raises_usage_error(self):
        bad = U.Usage(self.dir)  # a directory is not a database
        with self.assertRaises(U.UsageError):
            bad.admit("a", now=NOW)

    def test_env_path(self):
        self.assertEqual(U.usage_path({"FERRY_KEYS_DB": "/x/y.sqlite"}), "/x/y.sqlite")
        self.assertTrue(U.usage_path({}).endswith(
            os.path.join(".config", "ferry", "keys-usage.sqlite")))

    def test_default_path_is_read_from_the_environment_at_call_time(self):
        self.assertEqual(U.usage_path(), self.path)
        self.assertEqual(U.Usage().path, self.path)


WORKER = r"""
import os
import sys
sys.path.insert(0, sys.argv[1])
import ferry_keys_usage as U
assert os.environ["FERRY_KEYS_DB"] == sys.argv[2], "worker must inherit the temp FERRY_KEYS_DB"
u = U.Usage()
now = float(sys.argv[3])
print(sum(1 for _ in range(30) if u.admit("shared", rpm=25, now=now).ok))
"""


class TestTwoProcesses(UsageCase):
    def test_rpm_is_shared_across_processes(self):
        U.Usage(self.path).admit("warmup", now=NOW)  # create schema first
        procs = [subprocess.Popen([sys.executable, "-c", WORKER, FRONT, self.path, str(NOW)],
                                  stdout=subprocess.PIPE, text=True) for _ in range(2)]
        admitted = sum(int(p.communicate(timeout=60)[0].strip()) for p in procs)
        self.assertEqual(admitted, 25)
        self.assertEqual(self.u.minute_requests("shared", now=NOW), 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
