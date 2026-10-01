#!/usr/bin/env python3
"""Regression tests for the dispatcher's -h/--help guard (lib/ferry-main.zsh).

`ferry reload --help` used to RELOAD the live front door: the dispatcher called
`cmd_reload` without "$@", so the flag never reached anything. The guard now
answers -h/--help for every command before any cmd_ function runs.

The harness sources lib/ferry-usage.zsh, the four modules that own a
_ferry_<cmd>_usage function, then REPLACES every cmd_* with a stub that appends
its name and argv to a marker file, then sources lib/ferry-main.zsh with the
test's argv. No real command can run, so nothing here touches the live host.
The built `ferry` is not executed (its load-time bootstrap probes this machine,
and a broken guard would reach the real cmd_reload); `build.zsh --check` proves
the shipped monolith carries this same lib/ text.

Run: python3 lib/ferry-main.test.py
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "ferry-main.zsh")
USAGE = os.path.join(HERE, "ferry-usage.zsh")
USAGE_OWNERS = [os.path.join(HERE, m) for m in
                ("ferry-claude.zsh", "ferry-fleet.zsh", "ferry-auth-claude.zsh",
                 "ferry-keys.zsh")]

# Commands whose -h/--help is forwarded to a child script (see
# _FERRY_PASSTHROUGH_CMDS in lib/ferry-main.zsh).
PASSTHROUGH = {"dash"}
NOARG = {"reload", "status", "share", "log"}


def dispatch_table():
    """(label, cmd_function) for every real command arm of the case."""
    with open(MAIN) as f:
        text = f.read()
    body = text[text.index('case "$COMMAND" in'):]
    pairs = re.findall(r"^  ([a-z][a-z-]*)\)\s+(cmd_[a-z_]+)", body, re.M)
    return pairs


class DispatcherHelpGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ferry-main-test-")
        cls.table = dispatch_table()
        with open(USAGE) as f:
            usage_vars = sorted(set(re.findall(r"\$([A-Z_][A-Z_0-9]*)", f.read())))
        lines = ["set -eu", f'export HOME="{cls.tmp}/home"',
                 # Containment: a stray command substitution in the usage text
                 # (unquoted heredoc + backticks) once ran the REAL ferry up
                 # against a live host. No PATH to reach it by, and a function
                 # that records and refuses if anything calls it.
                 'export PATH="/usr/bin:/bin:/usr/sbin:/sbin"',
                 'ferry() { print -r -- "REAL-FERRY-CALLED $*" >> "$FERRY_TEST_MARKER"; return 97; }']
        lines += [f'{v}="dummy-{v}"' for v in usage_vars]
        lines.append(f"source {USAGE}")
        lines += [f"source {m}" for m in USAGE_OWNERS]
        for _, fn in cls.table:
            lines.append(f'{fn}() {{ print -r -- "{fn} $*" >> "$FERRY_TEST_MARKER"; }}')
        lines.append(f'source {MAIN} "$@"')
        cls.harness = os.path.join(cls.tmp, "harness.zsh")
        with open(cls.harness, "w") as f:
            f.write("\n".join(lines) + "\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_ferry(self, *argv):
        marker = os.path.join(self.tmp, "marker")
        if os.path.exists(marker):
            os.remove(marker)
        env = dict(os.environ, FERRY_TEST_MARKER=marker)
        proc = subprocess.run(["zsh", self.harness, *argv], capture_output=True,
                              text=True, env=env, timeout=30)
        invoked = ""
        if os.path.exists(marker):
            with open(marker) as f:
                invoked = f.read()
        return proc, invoked

    def test_table_is_complete(self):
        # Guards the parser above: if it found nothing, every loop below passes
        # vacuously.
        labels = {label for label, _ in self.table}
        self.assertGreaterEqual(len(labels), 30, labels)
        self.assertTrue({"reload", "install", "up", "dash", "msg"} <= labels)

    def test_control_stub_records_a_real_dispatch(self):
        # CONTROL: without --help the stub MUST be reached, or "stub not
        # invoked" below would prove nothing.
        proc, invoked = self.run_ferry("up", "--route")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(invoked.strip(), "cmd_up --route")
        proc, invoked = self.run_ferry("reload")
        self.assertEqual(invoked.strip(), "cmd_reload")

    def test_help_never_invokes_a_non_exempt_command(self):
        for label, fn in self.table:
            if label in PASSTHROUGH:
                continue
            for argv in ([label, "--help"], [label, "-h"],
                         [label, "--port", "1", "--help"], ["help", label]):
                with self.subTest(argv=argv):
                    proc, invoked = self.run_ferry(*argv)
                    self.assertEqual(invoked, "", f"{fn} ran for {argv}")
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn(label, proc.stdout)
                    self.assertNotIn("Unknown command", proc.stdout)

    def test_help_never_calls_the_real_ferry(self):
        for argv in (["help"], ["--help"], ["help", "up"], ["up", "--help"]):
            with self.subTest(argv=argv):
                proc, invoked = self.run_ferry(*argv)
                self.assertNotIn("REAL-FERRY-CALLED", invoked)

    def test_usage_heredoc_has_no_command_substitution(self):
        # The banner is an UNQUOTED heredoc (it needs $PORT etc.), so a backtick
        # or a dollar-paren in its text EXECUTES. Use plain quotes in help prose.
        with open(USAGE) as f:
            text = f.read()
        bodies = re.findall(r"cat <<EOF\n(.*?)\nEOF\n", text, re.S)
        self.assertTrue(bodies, "no unquoted cat <<EOF heredoc in ferry-usage.zsh")
        for body in bodies:
            # A backslash-escaped one (the existing \$(ferry env ...) example)
            # is literal and safe; an unescaped one executes.
            self.assertIsNone(re.search(r"(?<!\\)" + chr(96), body))
            self.assertIsNone(re.search(r"(?<!\\)\$\(", body))

    def test_help_output_is_the_commands_own_section(self):
        proc, _ = self.run_ferry("reload", "--help")
        self.assertIn("Restart ONLY the litellm front door", proc.stdout)
        self.assertNotIn("Ferrying models", proc.stdout)  # not the whole banner
        proc, _ = self.run_ferry("expose", "--help")
        self.assertNotIn("expose-vnc", proc.stdout)
        proc, _ = self.run_ferry("up", "-h")
        self.assertIn("Options for 'up':", proc.stdout)
        self.assertIn("--local-schematron", proc.stdout)
        proc, _ = self.run_ferry("fleet", "use", "--help")
        self.assertIn("ferry fleet use --clear", proc.stdout)
        proc, _ = self.run_ferry("claude", "--help")
        self.assertIn("Lane map:", proc.stdout)
        proc, _ = self.run_ferry("auth-claude", "login", "--help")
        self.assertIn("browser PKCE", proc.stdout)
        proc, _ = self.run_ferry("keys", "--help")
        self.assertIn("ferry keys revoke <name>", proc.stdout)

    def test_passthrough_forwards_help_to_the_command(self):
        proc, invoked = self.run_ferry("dash", "--grafana", "--help")
        self.assertEqual(invoked.strip(), "cmd_dash --grafana --help")
        # `ferry help dash` documents it without running the child.
        proc, invoked = self.run_ferry("help", "dash")
        self.assertEqual(invoked, "")
        self.assertIn("Live dashboard", proc.stdout)

    def test_argless_commands_reject_arguments(self):
        for label in sorted(NOARG):
            with self.subTest(label=label):
                proc, invoked = self.run_ferry(label, "--bogus")
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(invoked, "")
                self.assertIn("unexpected argument '--bogus'", proc.stderr)

    def test_unknown_command_with_help_is_still_unknown(self):
        proc, invoked = self.run_ferry("bogus", "--help")
        self.assertEqual(invoked, "")
        self.assertIn("Unknown command: bogus", proc.stdout)
        proc, _ = self.run_ferry("help", "bogus")
        self.assertIn("Unknown command: bogus", proc.stderr)

    def test_bare_help_and_top_level_help_print_the_banner(self):
        for argv in (["help"], ["--help"], ["help", "--help"]):
            with self.subTest(argv=argv):
                proc, invoked = self.run_ferry(*argv)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(invoked, "")
                self.assertIn("Ferrying models", proc.stdout)


if __name__ == "__main__":
    unittest.main()
