#!/usr/bin/env python3
"""Stdlib unittest for the ferry-serve launch/log lifecycle.

Run:  python3 lib/ferry-serve.test.py

Covers the restart bug that silently blanks the observability pipeline
(2026-08-26): `ferry up` truncated the proxy log with `>` while the previous
litellm was still shutting down. uvicorn's straggler writes ("Application
shutdown complete", "Finished server process") went out on an fd whose offset
was already ~92KB in, so they landed past the truncation point and punched a
SPARSE HOLE of NUL bytes that restored the file's size. The newly launched
process, holding its own offset-0 fd, then wrote underneath that hole and never
reached EOF — the log's mtime ticked on every request while its size never
moved, and ferry-log-shipper (which attaches at EOF) shipped nothing.

Two halves:
  * TestSparseHoleSemantics reproduces the OS-level behaviour directly with
    os.open/os.write/os.ftruncate, so the fix is pinned to why it works rather
    than to the shape of a shell line.
  * TestShippedLaunchLines asserts the generated `ferry` still carries the safe
    pattern, which is what a future edit would quietly revert.
"""
import os
import re
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

# The real incident's log sat at 91,956 bytes. Any offset far past what the new
# process writes reproduces it; keep the fixture small.
OFFSET = 4096
STRAGGLER = b"INFO:     Finished server process [94599]\n"
NEWLINE = b'INFO:     127.0.0.1:50670 - "POST /v1/chat/completions HTTP/1.1" 200 OK\n'


class TestSparseHoleSemantics(unittest.TestCase):
    """The filesystem behaviour the fix is built on, asserted directly."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "cloud-proxy-8090.log")

    def _dying_writer(self):
        """An fd positioned deep into the log, as a shutting-down uvicorn has."""
        fd = os.open(self.log, os.O_WRONLY | os.O_CREAT, 0o644)
        os.write(fd, b"\n" * OFFSET)
        return fd

    def test_truncate_under_a_live_fd_resurrects_the_size(self):
        # This asserts the BROKEN behaviour on purpose: if it ever stops holding,
        # the platform changed and _ferry_reset_log is no longer load-bearing.
        dying = self._dying_writer()
        os.truncate(self.log, 0)                     # the old `> "$cloud_log"`
        os.write(dying, STRAGGLER)                   # lands at OFFSET -> sparse hole
        os.close(dying)
        self.assertEqual(os.path.getsize(self.log), OFFSET + len(STRAGGLER))

    def test_the_new_process_then_writes_behind_eof_forever(self):
        dying = self._dying_writer()
        os.truncate(self.log, 0)
        os.write(dying, STRAGGLER)
        os.close(dying)
        eof = os.path.getsize(self.log)              # where a tailer would attach

        fresh = os.open(self.log, os.O_WRONLY | os.O_CREAT)   # the relaunched proxy
        for _ in range(5):
            os.write(fresh, NEWLINE)
        os.close(fresh)

        # Size never moves, so a shipper attached at `eof` never sees a byte of it.
        self.assertEqual(os.path.getsize(self.log), eof)

    def test_unlink_then_append_keeps_the_log_clean(self):
        # _ferry_reset_log's contract: unlink, so the straggler keeps its now
        # nameless inode and the launch below gets a brand-new one.
        dying = self._dying_writer()
        os.unlink(self.log)
        os.write(dying, STRAGGLER)                   # goes to the unlinked inode
        os.close(dying)

        fresh = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.write(fresh, NEWLINE)
        before = os.path.getsize(self.log)
        os.write(fresh, NEWLINE)
        os.close(fresh)

        self.assertEqual(before, len(NEWLINE))       # no hole, no straggler bytes
        self.assertEqual(os.path.getsize(self.log), 2 * len(NEWLINE))
        with open(self.log, "rb") as fh:
            self.assertNotIn(b"\x00", fh.read())

    def test_append_mode_survives_a_second_racing_writer(self):
        # O_APPEND is why the launch line uses `>>`: two writers on the same log
        # (a straggler that reopened, a concurrent `ferry up`) interleave whole
        # lines instead of overwriting each other.
        a = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        b = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.write(a, NEWLINE)
        os.write(b, STRAGGLER)
        os.write(a, NEWLINE)
        os.close(a); os.close(b)
        self.assertEqual(os.path.getsize(self.log), 2 * len(NEWLINE) + len(STRAGGLER))


class TestShippedLaunchLines(unittest.TestCase):
    """`ferry` is generated from lib/; these guard the pattern against a revert."""

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as fh:
            cls.src = fh.read()

    def test_no_litellm_launch_truncates_its_log(self):
        self.assertEqual(self.src.count('host 0.0.0.0 > "$cloud_log"'), 0)

    def test_every_litellm_launch_appends(self):
        self.assertEqual(self.src.count('host 0.0.0.0 >> "$cloud_log"'), 3)

    def test_every_litellm_launch_resets_its_log_first(self):
        self.assertEqual(self.src.count('_ferry_reset_log "$cloud_log"'), 3)

    def test_the_mlx_launch_resets_and_appends(self):
        self.assertEqual(self.src.count('_ferry_reset_log "$log"'), 1)
        self.assertEqual(self.src.count('nohup mlx_vlm.server "${mlargs[@]}" >> "$log"'), 1)

    def test_reset_log_unlinks_rather_than_truncating(self):
        body = re.search(r"_ferry_reset_log\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body, "_ferry_reset_log is missing from ferry")
        self.assertIn("rm -f", body.group(1))

    def test_stop_litellm_waits_for_exit(self):
        body = re.search(r"_ferry_stop_litellm\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body, "_ferry_stop_litellm is missing from ferry")
        text = body.group(1)
        self.assertIn("pkill -f", text)
        self.assertIn("pgrep -f", text)       # confirms the exit, not just signals it
        self.assertIn("while", text)          # polls for exit, never a bare sleep
        self.assertIn("pkill -9 -f", text)    # escalates rather than hanging forever

    def test_stop_litellm_is_scoped_to_a_port_when_given_one(self):
        # `ferry up --port 8099` must not reap a lane serving :8090.
        body = re.search(r"_ferry_stop_litellm\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIn("--port ${port}", body.group(1))

    def test_cmd_up_reaps_only_its_target_port(self):
        self.assertIn('_ferry_stop_litellm "$target_port"', self.src)

    def test_cmd_down_reaps_every_proxy(self):
        self.assertIn("\n  _ferry_stop_litellm\n", self.src)

    def test_free_port_polls_instead_of_a_fixed_sleep(self):
        body = re.search(r"_ferry_free_port\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body)
        self.assertIn("while", body.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
