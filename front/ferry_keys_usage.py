"""Per-key request and token counters for client keys, shared by every
front-door worker through one SQLite file (WAL).

Why a file and not memory: the front door runs FERRY_FRONT_WORKERS (default
4) processes, and a per-process counter would let a key through at 4x its
RPM. admit() does its read-check-increment inside BEGIN IMMEDIATE, which
takes SQLite's write lock, so two workers cannot both see "one left".

Windows: RPM is a fixed calendar minute (epoch // 60); budgets are calendar
months in UTC. Tokens are added on completion, so every request already in
flight when the budget is reached has passed admission: requests already in
flight can overshoot the budget, bounded by the key's concurrency, not by one
request (documented, accepted).

Nothing secret lives here (names and counts), but the file is kept 0600
anyway: a Usage's first connection chmods the db and any existing -wal/-shm
sidecars to 0600, and SQLite creates new sidecars with the db file's mode.
"""
from __future__ import annotations

import calendar
import math
import os
import sqlite3
import time
from collections import namedtuple

Verdict = namedtuple("Verdict", "ok reason retry_after")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS minute (name TEXT NOT NULL, "
    "minute_epoch INTEGER NOT NULL, n INTEGER NOT NULL, "
    "PRIMARY KEY (name, minute_epoch))",
    "CREATE TABLE IF NOT EXISTS month (name TEXT NOT NULL, "
    "yyyymm INTEGER NOT NULL, tokens INTEGER NOT NULL, "
    "PRIMARY KEY (name, yyyymm))",
)


class UsageError(Exception):
    """The usage DB cannot be opened or written; fk- keys are refused (503)."""


def usage_path(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("FERRY_KEYS_DB") or os.path.join(
        os.path.expanduser("~"), ".config", "ferry", "keys-usage.sqlite")


def month_of(now: float) -> int:
    t = time.gmtime(now)
    return t.tm_year * 100 + t.tm_mon


def _months_back(yyyymm: int, n: int) -> int:
    year, month = divmod(yyyymm, 100)
    index = year * 12 + (month - 1) - n
    return (index // 12) * 100 + index % 12 + 1


def seconds_to_next_month(now: float) -> int:
    t = time.gmtime(now)
    year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    return max(1, int(math.ceil(calendar.timegm((year, month, 1, 0, 0, 0)) - now)))


class Usage:
    def __init__(self, path=None, clock=time.time) -> None:
        self.path = path or usage_path()
        self.clock = clock
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        try:
            if not self._ready:
                directory = os.path.dirname(self.path) or "."
                os.makedirs(directory, mode=0o700, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                os.close(fd)
                # O_CREAT's mode only applies to a NEW file. A file that
                # already exists (e.g. recreated under the umask by a stale
                # connection after the DB was deleted at runtime) is tightened
                # here; SQLite gives new -wal/-shm files the db file's mode,
                # and any existing ones are tightened too.
                os.chmod(self.path, 0o600)
                for suffix in ("-wal", "-shm"):
                    try:
                        os.chmod(self.path + suffix, 0o600)
                    except FileNotFoundError:
                        pass
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            if not self._ready:
                conn.execute("PRAGMA journal_mode=WAL")
                for statement in _SCHEMA:
                    conn.execute(statement)
                self._ready = True
            return conn
        except (OSError, sqlite3.Error) as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err

    def _now(self, now):
        return self.clock() if now is None else now

    def admit(self, name, rpm=None, budget_tokens=None, now=None) -> Verdict:
        now = self._now(now)
        minute = int(now // 60)
        month = month_of(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM minute WHERE minute_epoch < ?", (minute - 1,))
                row = conn.execute(
                    "SELECT n FROM minute WHERE name = ? AND minute_epoch = ?",
                    (name, minute)).fetchone()
                if rpm is not None and (row[0] if row else 0) >= rpm:
                    conn.execute("COMMIT")
                    return Verdict(False, "rpm", max(1, int(math.ceil(60 - now % 60))))
                row = conn.execute(
                    "SELECT tokens FROM month WHERE name = ? AND yyyymm = ?",
                    (name, month)).fetchone()
                if budget_tokens is not None and (row[0] if row else 0) >= budget_tokens:
                    conn.execute("COMMIT")
                    return Verdict(False, "budget", seconds_to_next_month(now))
                conn.execute(
                    "INSERT INTO minute (name, minute_epoch, n) VALUES (?, ?, 1) "
                    "ON CONFLICT (name, minute_epoch) DO UPDATE SET n = n + 1",
                    (name, minute))
                conn.execute("COMMIT")
                return Verdict(True, "", 0)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err
        finally:
            conn.close()

    def add_tokens(self, name, tokens, now=None) -> None:
        now = self._now(now)
        month = month_of(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM month WHERE yyyymm < ?", (_months_back(month, 12),))
            conn.execute(
                "INSERT INTO month (name, yyyymm, tokens) VALUES (?, ?, ?) "
                "ON CONFLICT (name, yyyymm) DO UPDATE SET tokens = tokens + excluded.tokens",
                (name, month, int(tokens)))
            conn.execute("COMMIT")
        except sqlite3.Error as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err
        finally:
            conn.close()

    def _scalar(self, sql, args) -> int:
        conn = self._connect()
        try:
            row = conn.execute(sql, args).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err
        finally:
            conn.close()

    def month_tokens(self, name, now=None) -> int:
        return self._scalar("SELECT tokens FROM month WHERE name = ? AND yyyymm = ?",
                            (name, month_of(self._now(now))))

    def minute_requests(self, name, now=None) -> int:
        return self._scalar("SELECT n FROM minute WHERE name = ? AND minute_epoch = ?",
                            (name, int(self._now(now) // 60)))

    def summary(self, now=None) -> dict:
        if not os.path.exists(self.path):
            return {}
        now = self._now(now)
        out = {}
        conn = self._connect()
        try:
            for name, tokens in conn.execute(
                    "SELECT name, tokens FROM month WHERE yyyymm = ?", (month_of(now),)):
                out.setdefault(name, {"month_tokens": 0, "minute_requests": 0})
                out[name]["month_tokens"] = int(tokens)
            for name, n in conn.execute(
                    "SELECT name, n FROM minute WHERE minute_epoch = ?", (int(now // 60),)):
                out.setdefault(name, {"month_tokens": 0, "minute_requests": 0})
                out[name]["minute_requests"] = int(n)
        except sqlite3.Error as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err
        finally:
            conn.close()
        return out
