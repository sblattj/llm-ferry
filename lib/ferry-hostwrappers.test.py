#!/usr/bin/env python3
"""Stdlib unittest for the HOST-side opencode shell wrappers.

Run:  python3 lib/ferry-hostwrappers.test.py

`opencode-cloud` / `opencode-local` were written only by client-bootstrap.sh, so
every client got them and the host got nothing — host-bootstrap.sh has no
occurrence of "opencode" at all, and host-reset.sh never touched ~/.zshrc. The
host therefore ended up with the two profile FILES and no way to select between
them, and hand-wiring filled the gap under a marker nothing else can match.

The marker is what most of this suite is about. client-bootstrap.sh and
client-cleanup.sh both strip a block by EXACT string equality on
"# >>> ferry opencode profiles >>>". A block written under any other spelling is
invisible to both, so the next client bootstrap appends a SECOND definition of
the same two functions and the duplicate stays hidden until they disagree. The
legacy-absorption test below is the regression guard for exactly that.

Runs the REAL built `ferry` against a throwaway $HOME.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

CANON_START = "# >>> ferry opencode profiles >>>"
CANON_END = "# <<< ferry opencode profiles <<<"
LEGACY_START = "# >>> ferry opencode profiles (host) >>>"
LEGACY_END = "# <<< ferry opencode profiles (host) <<<"

LEGACY_BLOCK = f"""{LEGACY_START}
unalias opencode-cloud opencode-local 2>/dev/null

opencode-cloud() {{
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-cloud.json" command opencode "$@"
}}

opencode-local() {{
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-local.json" command opencode "$@"
}}
{LEGACY_END}
"""


class HostWrapperTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-hostwrap-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.rc = os.path.join(self.home, ".zshrc")

    def run_install(self):
        return subprocess.run(
            [FERRY, "opencode", "--wrappers"],
            capture_output=True, text=True,
            env=dict(os.environ, HOME=self.home, TMPDIR=self.home),
            cwd=self.home,
        )

    def rc_text(self):
        if not os.path.exists(self.rc):
            return ""
        with open(self.rc) as f:
            return f.read()

    def count(self, needle):
        return self.rc_text().count(needle)

    def test_fresh_zshrc_gets_one_block_with_both_functions(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.rc_text()
        self.assertEqual(text.count(CANON_START), 1)
        self.assertEqual(text.count(CANON_END), 1)
        self.assertIn("opencode-cloud() {", text)
        self.assertIn("opencode-local() {", text)

    def test_is_idempotent(self):
        for _ in range(3):
            self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.count(CANON_START), 1)
        self.assertEqual(self.count("opencode-local() {"), 1)

    def test_absorbs_the_legacy_host_marker(self):
        """The hand-wired '(host)' block must be replaced, not joined."""
        with open(self.rc, "w") as f:
            f.write("export FOO=1\n" + LEGACY_BLOCK + "export BAR=2\n")

        self.assertEqual(self.run_install().returncode, 0)
        text = self.rc_text()

        self.assertNotIn(LEGACY_START, text, "legacy block survived")
        self.assertNotIn(LEGACY_END, text)
        self.assertEqual(text.count(CANON_START), 1, "should be exactly one block")
        self.assertEqual(text.count("opencode-local() {"), 1,
                         "two competing definitions of the same function")

    def test_replaces_an_existing_canonical_block(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(self.rc, "a") as f:
            f.write("\nexport AFTER=1\n")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.count(CANON_START), 1)
        self.assertIn("export AFTER=1", self.rc_text())

    def test_preserves_unrelated_content(self):
        with open(self.rc, "w") as f:
            f.write("export PATH=/custom:$PATH\n"
                    "alias ll='ls -la'\n"
                    + LEGACY_BLOCK +
                    "source ~/.something\n")
        self.assertEqual(self.run_install().returncode, 0)
        text = self.rc_text()
        self.assertIn("export PATH=/custom:$PATH", text)
        self.assertIn("alias ll='ls -la'", text)
        self.assertIn("source ~/.something", text)

    def test_strips_legacy_aliases_that_would_break_the_functions(self):
        # An alias above a function of the same name makes zsh expand it inside
        # `name() {`, which is a parse error on every later `source ~/.zshrc`.
        with open(self.rc, "w") as f:
            f.write("alias opencode-cloud='opencode --cloud'\n"
                    "alias opencode-local='opencode --local'\n")
        self.assertEqual(self.run_install().returncode, 0)
        text = self.rc_text()
        self.assertNotIn("alias opencode-cloud=", text)
        self.assertNotIn("alias opencode-local=", text)

    def test_writes_no_bare_opencode_function(self):
        # A host that exports OPENCODE_CONFIG chose its default deliberately.
        self.assertEqual(self.run_install().returncode, 0)
        self.assertIsNone(
            re.search(r"^opencode\(\)", self.rc_text(), re.M),
            "bare opencode() would override the host's own OPENCODE_CONFIG choice",
        )

    def test_the_result_is_valid_zsh(self):
        """The block must actually parse — an alias/function collision would not."""
        with open(self.rc, "w") as f:
            f.write("alias opencode-cloud='oops'\n" + LEGACY_BLOCK)
        self.assertEqual(self.run_install().returncode, 0)
        r = subprocess.run(["zsh", "-n", self.rc], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"generated ~/.zshrc does not parse: {r.stderr}")

    def test_the_functions_actually_select_the_right_config(self):
        """Source the result and prove each wrapper exports the config it names.

        A stub `opencode` earlier on PATH echoes what it received, so this
        observes the wrapper's real effect rather than re-reading the text we
        just wrote.
        """
        self.assertEqual(self.run_install().returncode, 0)
        bindir = os.path.join(self.home, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "opencode")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "CONFIG=$OPENCODE_CONFIG"\n')
        os.chmod(stub, 0o755)

        for fn, expected in (("opencode-cloud", "opencode-cloud.json"),
                             ("opencode-local", "opencode-local.json")):
            r = subprocess.run(
                ["zsh", "-c", f"source {self.rc}; {fn}"],
                capture_output=True, text=True,
                env=dict(os.environ, HOME=self.home,
                         PATH=bindir + os.pathsep + os.environ["PATH"]),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(expected, r.stdout, f"{fn} selected the wrong config")

        # Control: without sourcing, the bare stub sees no ferry config — so the
        # assertions above are observing the wrappers, not an ambient value.
        r = subprocess.run(
            ["zsh", "-c", "opencode"], capture_output=True, text=True,
            env=dict(os.environ, HOME=self.home,
                     PATH=bindir + os.pathsep + os.environ["PATH"],
                     OPENCODE_CONFIG=""),
        )
        self.assertNotIn("opencode-cloud.json", r.stdout)


class HostResetWiringTest(unittest.TestCase):
    """host-reset.sh must actually call the installer — the bug was its absence."""

    def test_host_reset_invokes_the_wrapper_installer(self):
        with open(os.path.join(REPO, "host-reset.sh")) as f:
            text = f.read()
        self.assertIn("opencode --wrappers", text)

    def test_install_path_invokes_it_too(self):
        with open(os.path.join(REPO, "lib", "ferry-install.zsh")) as f:
            text = f.read()
        self.assertIn("_ferry_install_host_wrappers", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
