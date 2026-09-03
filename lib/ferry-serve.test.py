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
import subprocess
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

    def test_launch_front_passes_workers(self):
        body = re.search(r"_ferry_launch_front\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body, "_ferry_launch_front is missing from ferry")
        self.assertIn('--workers "$workers"', body.group(1))
        # 4th arg overrides, else the FERRY_WORKERS-derived default
        self.assertIn('${4:-$FERRY_FRONT_WORKERS}', body.group(1))

    def test_workers_default_is_a_small_pool_overridable_by_env(self):
        # litellm's benchmark guidance is one worker per CPU; a LAN host needs
        # only a small pool. 4 is the shipped default, FERRY_WORKERS overrides
        # (1 restores the old single-process shape).
        self.assertIn('FERRY_FRONT_WORKERS="${FERRY_WORKERS:-4}"', self.src)

    def test_port_precedes_workers_on_the_launch_line(self):
        # _ferry_stop_litellm reaps by `(litellm|ferry_front\.py) .*--port N( |$)`.
        # --workers must come AFTER --port so the space in that pattern still
        # matches the longer cmdline; if someone reorders the flags the reap
        # silently misses the master and `ferry up` fights a zombie front.
        body = re.search(r"_ferry_launch_front\(\) \{(.*?)\n\}", self.src, re.S)
        text = body.group(1)
        self.assertIn("--port", text)
        self.assertIn("--workers", text)
        self.assertLess(text.index("--port"), text.index("--workers"))

    def test_plain_litellm_fallback_keeps_worker_parity(self):
        # If the catalogue front is unavailable and ferry falls back to the raw
        # `litellm` CLI, it must launch the SAME worker count, not silently
        # drop to one process (the CLI flag is --num_workers, not --workers).
        self.assertEqual(self.src.count('--num_workers "$FERRY_FRONT_WORKERS"'), 2)


class TestStatusTestCommand(unittest.TestCase):
    """The curl line `ferry status` prints must work when pasted."""

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as f:
            cls.src = f.read()

    def test_suggested_max_tokens_clears_the_reasoning_floor(self):
        # Every lane on this endpoint is a REASONING model, and reasoning tokens
        # come out of the same budget as the answer. At max_tokens 10 the whole
        # allowance is spent thinking: the reply is finish_reason=length with
        # content=null, so the one command status hands you to prove the stack is
        # alive reads as a dead lane. Measured live on the orch lane 2026-08-26
        # (reasoning_tokens 7, text_tokens 3, content None).
        m = re.search(r'Test with: curl.*?max_tokens\\":(\d+)', self.src)
        self.assertIsNotNone(m, "no suggested test command found in cmd_status")
        self.assertGreaterEqual(
            int(m.group(1)), 64,
            "suggested max_tokens is below the reasoning floor; the command it "
            "prints returns content=null on a healthy lane")


class TestMasterKeyProbes(unittest.TestCase):
    """v1.22.0: the front door can sit behind general_settings.master_key.

    Every probe that answers "is it up" must survive a 401, and every probe
    that reads catalogue content must present LITELLM_MASTER_KEY when the host
    has one — while a keyless LAN install (no template edit) probes exactly as
    it did before, so the bearer is conditional everywhere.
    """

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as f:
            cls.src = f.read()

    def wait_http_body(self):
        m = re.search(r"_ferry_wait_http\(\) \{.*?\n\}", self.src, re.S)
        self.assertIsNotNone(m, "_ferry_wait_http is missing from ferry")
        return m.group(0)

    def run_wait_http(self, mode, server_status, require_bearer=None,
                      env_key=None, timeout="3"):
        """Execute the REAL extracted function against a throwaway HTTP server."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        hit = {"auth": None}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                hit["auth"] = self.headers.get("Authorization")
                ok = require_bearer is None or hit["auth"] == f"Bearer {require_bearer}"
                self.send_response(server_status if ok else 401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}/probe"

        with tempfile.NamedTemporaryFile("w", suffix=".zsh", delete=False) as f:
            f.write("set -u\n" + self.wait_http_body() + "\n"
                    f"_ferry_wait_http '{url}' probe {timeout} {mode}\n"
                    "echo RC=$?\n")
            path = f.name
        env = dict(os.environ)
        env.pop("LITELLM_MASTER_KEY", None)   # hermetic: author shells may export one
        if env_key is not None:
            env["LITELLM_MASTER_KEY"] = env_key
        try:
            r = subprocess.run(["zsh", path], capture_output=True, text=True,
                               env=env, timeout=60)
        finally:
            os.unlink(path)
        self.assertIn("RC=", r.stdout, r.stderr)
        return int(r.stdout.strip().rsplit("RC=", 1)[1]), hit

    def test_a_200_is_ready_in_both_modes(self):
        rc, _ = self.run_wait_http("content", 200)
        self.assertEqual(rc, 0)
        rc, _ = self.run_wait_http("readiness", 200)
        self.assertEqual(rc, 0)

    def test_a_401_is_ready_only_in_readiness_mode(self):
        # THE v1.22.0 case: a master_key front door answers 401; readiness must
        # call that up. The content probe must still refuse it — a 401 on an
        # MLX lane's /v1/models would NOT mean warm.
        rc, _ = self.run_wait_http("readiness", 401)
        self.assertEqual(rc, 0)
        rc, _ = self.run_wait_http("content", 401)
        self.assertEqual(rc, 1)

    def test_the_bearer_is_sent_when_the_key_is_set(self):
        rc, hit = self.run_wait_http("readiness", 200, require_bearer="sk-test",
                                     env_key="sk-test")
        self.assertEqual(rc, 0)
        self.assertEqual(hit["auth"], "Bearer sk-test",
                         "LITELLM_MASTER_KEY was not presented on the probe")

    def test_keyless_probes_send_no_auth_header(self):
        # LAN installs without the template edit: no var, no header — byte for
        # byte the probes ferry sent before v1.22.0.
        rc, hit = self.run_wait_http("content", 200)
        self.assertEqual(rc, 0)
        self.assertIsNone(hit["auth"], "a keyless install must not send an auth header")

    def test_readiness_mode_accepts_401_even_with_a_wrong_key(self):
        # Readiness asks "is the process up"; a rejected key still proves it is.
        rc, _ = self.run_wait_http("readiness", 200, require_bearer="sk-right",
                                   env_key="sk-wrong")
        self.assertEqual(rc, 0)

    # --- structural: the call sites and their modes ---------------------------
    def test_front_wait_uses_the_public_liveliness_route_in_readiness_mode(self):
        self.assertRegex(
            self.src,
            r'_ferry_wait_http "http://127\.0\.0\.1:\$target_port/health/liveliness"'
            r' +"front" +120 readiness \|\| true')

    def test_mlx_lane_waits_keep_the_catalogue_probe(self):
        # The MLX backends are loopback-only and never authenticated, and their
        # /v1/models 200 IS the weights-resident signal (the port does not
        # accept connections until the weights are resident). Switching them to
        # a litellm-only route would report every cold load NOT READY forever.
        self.assertIn('_ferry_wait_http "http://127.0.0.1:$LOCAL_ORCH_PORT/v1/models"',
                      self.src)
        self.assertIn('_ferry_wait_http "http://127.0.0.1:$LOCAL_SUB_PORT/v1/models"',
                      self.src)
        for lane in ("$LOCAL_ORCH_PORT", "$LOCAL_SUB_PORT"):
            for m in re.finditer(r'_ferry_wait_http "http://127\.0\.0\.1:' +
                                 re.escape(lane) + r'[^"]*"', self.src):
                line = self.src[m.start():self.src.find("\n", m.start())]
                self.assertNotIn("readiness", line,
                                 "MLX lane probes must stay strict content probes")

    def test_wait_http_bearer_is_conditional(self):
        body = self.wait_http_body()
        self.assertIn('[[ -n "${LITELLM_MASTER_KEY:-}" ]] && hdr=', body)

    def test_catalogue_curls_send_the_bearer_only_when_set(self):
        # The stack-up banner, the client status listing, and the host status
        # listing all read /v1/models CONTENT, so each presents the bearer via
        # a guarded array — never an unconditional header.
        for guard in ("banner_auth=()", "status_auth=()", "models_auth=()"):
            self.assertRegex(
                self.src,
                re.escape(guard) + r"\n\s*\[\[ -n \"\$\{LITELLM_MASTER_KEY:-\}\" \]\] && " +
                re.escape(guard[:-3]) + r'=\(-H "Authorization: Bearer \$LITELLM_MASTER_KEY"\)')

    def test_client_connectivity_counts_401_as_online(self):
        # A keyless client probing a keyed host must read ONLINE — the probe is
        # readiness, not catalogue content.
        segment = self.src[self.src.index(">>> Probing network connectivity"):
                           self.src.index(">>> Connection Health")]
        self.assertIn('== "401"', segment)

    def test_printed_commands_reference_the_variable_never_the_value(self):
        # The pasted-curl hints must resolve the key in the PASTING shell
        # (\$LITELLM_MASTER_KEY stays literal in the output) — echoing the
        # value would leak a front-door credential into scrollback.
        self.assertIn(r'Bearer \$LITELLM_MASTER_KEY', self.src)
        for m in re.finditer(r'echo[^\n]*Bearer \\\$LITELLM_MASTER_KEY[^\n]*', self.src):
            line = m.group(0)
            self.assertNotIn("sk-", line.replace("\\$LITELLM_MASTER_KEY", ""),
                             "a printed command embeds a literal key value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
