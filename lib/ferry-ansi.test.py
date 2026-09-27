#!/usr/bin/env python3
"""Stdlib unittest: colour is for a terminal, never for a pipe.

Run:  python3 lib/ferry-ansi.test.py

v1.17.0 fixed `ferry drop`, whose passphrase came back wrapped in ANSI escapes
when stdout was redirected, so `ferry drop f | grep passphrase` scraped to a
subtly wrong secret. The class was wider: relay printed its `ferry expose
--token <token>` line, share its `curl … | zsh` bootstrap lines, and serve its
READY / ONLINE / OFFLINE status, all in unconditional colour. Every site now
goes through ONE helper, `_ferry_colors` in lib/ferry-core.zsh, which gates on
`[[ -t <fd> ]]` and honours NO_COLOR (https://no-color.org).

The live tests run the REAL built `ferry`. `subprocess` with capture gives a
pipe, which is the redirected case by construction; the tty controls run the
same command with stdout on a pseudo-terminal and assert that colour DOES
appear there, so an absent ESC byte on the pipe side cannot pass merely
because the command stopped printing colour altogether.

`ferry share` is deliberately NOT run live: SHARE_PORT is a hardcoded literal,
the command binds the LAN and serves the checkout, and on a shared host it
would collide with the real share server. Its three bootstrap lines are
covered by the source-level assertions plus the helper tests instead.
"""
import glob
import json
import os
import pty
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
LIB = os.path.join(REPO, "lib")
ESC = "\x1b"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def read_built():
    with open(FERRY) as f:
        return f.read()


def helper_source():
    """The `_ferry_colors` function, extracted verbatim from the built ferry."""
    m = re.search(r"^_ferry_colors\(\) \{\n.*?^\}\n", read_built(), re.S | re.M)
    if not m:
        raise AssertionError("_ferry_colors() not found in the built ferry")
    return m.group(0)


def run_on_pty(argv, env, cwd=None, timeout=60):
    """Run argv with stdout on a pseudo-terminal (stderr to a pipe); return the
    decoded stdout. Reads the master until EOF/EIO, which macOS raises when the
    child exits and the last slave fd closes."""
    master, slave = pty.openpty()
    p = subprocess.Popen(argv, stdout=slave, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, env=env, cwd=cwd)
    os.close(slave)
    out = bytearray()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    finally:
        os.close(master)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        p.stderr.close()
    return out.decode("utf-8", "replace")


def env_without_no_color(**extra):
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env.update(extra)
    return env


class HelperTest(unittest.TestCase):
    """`_ferry_colors` itself, driven in zsh under each stream shape."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-ansi-helper-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def script(self, body):
        path = os.path.join(self.dir, "probe.zsh")
        with open(path, "w") as f:
            f.write("set -eu\n" + helper_source() + body)
        return ["zsh", path]

    PROBE = '_ferry_colors\nprint -rn -- "${C_GREEN}g${C_YELLOW}y${C_RED}r${C_RESET}"\n'

    def test_a_pipe_gets_no_colour(self):
        r = subprocess.run(self.script(self.PROBE), capture_output=True,
                           text=True, env=env_without_no_color())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "gyr")

    def test_a_tty_gets_colour(self):
        out = run_on_pty(self.script(self.PROBE), env_without_no_color())
        self.assertIn("\x1b[1;32mg", out)
        self.assertIn("\x1b[1;33my", out)
        self.assertIn("\x1b[1;31mr", out)
        self.assertIn("\x1b[0m", out)

    def test_no_color_disables_colour_even_on_a_tty(self):
        out = run_on_pty(self.script(self.PROBE), env_without_no_color(NO_COLOR="1"))
        self.assertNotIn(ESC, out)
        self.assertIn("gyr", out)

    def test_an_empty_no_color_does_not_disable_colour(self):
        # no-color.org: only a PRESENT AND NON-EMPTY NO_COLOR disables colour.
        out = run_on_pty(self.script(self.PROBE), env_without_no_color(NO_COLOR=""))
        self.assertIn(ESC, out)

    def test_fd_argument_tests_that_stream_not_stdout(self):
        # stdout is a tty, stderr is a pipe: `_ferry_colors 2` must see the pipe.
        body = ('_ferry_colors 2\nprint -rn -- "[${C_GREEN}two${C_RESET}]"\n'
                '_ferry_colors\nprint -rn -- "[${C_GREEN}one${C_RESET}]"\n')
        out = run_on_pty(self.script(body), env_without_no_color())
        self.assertIn("[two]", out)
        self.assertIn("[\x1b[1;32mone\x1b[0m]", out)

    def test_a_redirected_call_inside_a_tty_session_gets_no_colour(self):
        # The gate is evaluated where it is called: a function run as `f >file`
        # from a terminal session must still write plain text into the file.
        log = os.path.join(self.dir, "out.log")
        body = ('f() { _ferry_colors; print -rn -- "${C_GREEN}v${C_RESET}"; }\n'
                f'f > {log}\n')
        run_on_pty(self.script(body), env_without_no_color())
        with open(log) as fh:
            self.assertEqual(fh.read(), "v")


class SourceInvariantTest(unittest.TestCase):
    """One implementation in the tree, and every colour site routed through it."""

    ESCAPES = re.compile(r"\\033|\\e\[|\\x1[bB]|\\u001[bB]|\x1b|\$'\\e|\btput\b")

    def modules(self):
        return sorted(glob.glob(os.path.join(LIB, "ferry-*.zsh")))

    def test_no_escape_literal_outside_the_helper(self):
        helper_line = re.compile(r"^\s*C_GREEN=\$'\\033\[1;32m' C_YELLOW=")
        offenders = []
        for path in self.modules():
            with open(path) as f:
                for n, line in enumerate(f, 1):
                    if self.ESCAPES.search(line) and not helper_line.search(line):
                        offenders.append(f"{os.path.basename(path)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "escape literal outside _ferry_colors")

    def test_the_scan_can_see_an_escape(self):
        # Control: the pattern matches the helper's own line, so an empty
        # offenders list above means "none", not "the regex is broken".
        with open(os.path.join(LIB, "ferry-core.zsh")) as f:
            self.assertTrue(any(self.ESCAPES.search(l) for l in f))

    def test_every_module_that_colours_calls_the_helper(self):
        users = []
        for path in self.modules():
            with open(path) as f:
                src = f.read()
            if re.search(r"\$\{?C_(GREEN|YELLOW|RED|RESET)", src) \
                    and not path.endswith("ferry-core.zsh"):
                users.append(os.path.basename(path))
                self.assertIn("_ferry_colors", src, f"{path} colours without the gate")
        # Control: the four modules the follow-up doc named are all users.
        for m in ("ferry-drop.zsh", "ferry-relay.zsh", "ferry-serve.zsh", "ferry-share.zsh"):
            self.assertIn(m, users)

    def test_share_bootstrap_lines_are_gated(self):
        built = read_built()
        for script in ("client-bootstrap.sh | zsh", "client-reset.sh | zsh",
                       "client-cleanup.sh | zsh -s -- --dry-run"):
            line = [l for l in built.splitlines()
                    if script in l and "curl -fsSL http://$MDNS_NAME" in l]
            self.assertEqual(len(line), 1, script)
            self.assertIn("${C_GREEN}curl", line[0])
            self.assertTrue(line[0].rstrip().endswith('${C_RESET}"'), line[0])

    def test_built_ferry_is_in_sync(self):
        r = subprocess.run(["zsh", os.path.join(REPO, "build.zsh"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-ansi-home-")
        self.tmp = tempfile.mkdtemp(prefix="ferry-ansi-tmp-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def env(self, **extra):
        e = env_without_no_color(HOME=self.home, TMPDIR=self.tmp)
        e.pop("FERRY_RELAY_TOKEN", None)
        e.update(extra)
        return e

    def run_ferry(self, *args, timeout=60, **extra):
        return subprocess.run(["zsh", FERRY, *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=timeout,
                              cwd=self.tmp)


class RelayTokenTest(LiveBase):
    """The relay banner carries the shared TOKEN — the highest-stakes site."""

    def reap(self, port):
        r = subprocess.run(["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                           capture_output=True, text=True)
        for pid in r.stdout.split():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass

    def test_scraped_token_is_the_real_token(self):
        port = free_port()
        # Registered BEFORE the run: the relay disowns itself, so a failed
        # assertion must still reap it.
        self.addCleanup(self.reap, port)
        r = self.run_ferry("relay", "--port", str(port), "--bind", "127.0.0.1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(ESC, r.stdout, "ANSI leaked into piped relay output")

        line = [l for l in r.stdout.splitlines() if "ferry expose" in l and "--token" in l]
        self.assertEqual(len(line), 1, r.stdout)
        scraped = line[0].split("--token", 1)[1].strip()
        with open(os.path.join(self.home, ".config", "ferry", "relay-token")) as f:
            real = f.read().strip()
        self.assertTrue(real)
        self.assertEqual(scraped, real, "the scraped token is not the token")


class ClientStatusTest(LiveBase):
    """cmd_status in client mode against a dead port prints the coloured OFFLINE
    line fast (connection refused) — the serve module's status sites."""

    def setUp(self):
        super().setUp()
        cfg = os.path.join(self.home, ".config", "ferry")
        os.makedirs(cfg)
        dead = free_port()
        with open(os.path.join(cfg, "client.json"), "w") as f:
            json.dump({"host": "127.0.0.1", "port": dead, "share_port": dead}, f)

    def test_piped_status_is_plain(self):
        r = self.run_ferry("status")
        self.assertIn("Connection Health: OFFLINE", r.stdout, r.stdout + r.stderr)
        self.assertNotIn(ESC, r.stdout)

    def test_status_on_a_tty_is_coloured(self):
        out = run_on_pty(["zsh", FERRY, "status"], self.env(), cwd=self.tmp)
        self.assertIn("\x1b[1;31mOFFLINE\x1b[0m", out, out)

    def test_no_color_on_a_tty_is_plain(self):
        out = run_on_pty(["zsh", FERRY, "status"], self.env(NO_COLOR="1"), cwd=self.tmp)
        self.assertIn("Connection Health: OFFLINE", out, out)
        self.assertNotIn(ESC, out)


class DropTtyControlTest(LiveBase):
    """ferry-drop.test.py proves the piped passphrase is plain; this is its
    control — the same command on a terminal still colours it."""

    def drop(self, **extra):
        with open(os.path.join(self.tmp, "plain.txt"), "w") as f:
            f.write("x")
        out = run_on_pty(["zsh", FERRY, "drop", "plain.txt", "--to", "b.ferrydrop"],
                         self.env(**extra), cwd=self.tmp)
        return [l for l in out.splitlines() if "passphrase:" in l]

    def test_passphrase_is_coloured_on_a_tty(self):
        line = self.drop()
        self.assertEqual(len(line), 1)
        self.assertIn("\x1b[1;33m", line[0])

    def test_no_color_keeps_the_passphrase_plain_on_a_tty(self):
        line = self.drop(NO_COLOR="1")
        self.assertEqual(len(line), 1)
        self.assertNotIn(ESC, line[0])


if __name__ == "__main__":
    unittest.main()
