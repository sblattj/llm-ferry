#!/usr/bin/env python3
"""Stdlib unittest for the HOST-side Claude Code CLI wiring.

Run:  python3 lib/ferry-claude.test.py

`ferry claude` is the Claude Code twin of `ferry opencode --wrappers`: it writes
one marker-delimited block into ~/.zshrc defining `claude-ferry()` (the cloud
pair), `claude-ferry-local()` (the GPU pair) and `claude-ferry-super()` (the
cheap cloud profile: heavy drives, super-flash on background AND subagents), so
a shell picks a lane by invoking the right function instead of exporting
ANTHROPIC_* itself. The
default action additionally records the host's endpoint in
~/.config/ferry/claude.json (host/port/lanes), snapshotting an existing one to
a .bak first; `--wrappers` installs the zshrc block and NOTHING else, because
host-reset.sh calls it once per refresh and must not churn the json.

The marker is what most of this suite is about. client-bootstrap.sh and
client-cleanup.sh both strip a block by EXACT string equality on
"# >>> ferry claude profiles >>>". A block written under any other spelling is
invisible to both, so the next bootstrap appends a SECOND definition of the
same two functions and the duplicate stays hidden until they disagree.

Runs the REAL `cmd_claude` against a throwaway $HOME — through the built
`ferry` monolith when it answers `ferry claude --help`, otherwise by sourcing
lib/ferry-core.zsh + lib/ferry-claude.zsh in a zsh subprocess.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
CLAUDE_MODULE = os.path.join(REPO, "lib", "ferry-claude.zsh")

CANON_START = "# >>> ferry claude profiles >>>"
CANON_END = "# <<< ferry claude profiles <<<"

INSTALL_HOST = "testhost"
INSTALL_PORT = "8090"

# The vars the wrappers are supposed to CREATE. The runner's own shell may
# carry ambient ANTHROPIC_* values (this host wires its own agents through
# ferry), which would fake both the behavioral assertions and the control.
ANTHROPIC_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_DISABLE_THINKING")


def _monolith_supports_claude():
    """True when the built `ferry` monolith answers `ferry claude --help`.

    The monolith is GENERATED (build.zsh concatenates lib/ferry-*.zsh), so it
    lags the module until someone rebuilds. When it is stale or absent, the
    functional tests fall back to sourcing the modules directly — the contract
    under test is the module, not the packaging step.
    """
    if not os.path.exists(FERRY):
        return False
    try:
        p = subprocess.run(
            ["zsh", FERRY, "claude", "--help"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "Unknown command" not in (p.stdout + p.stderr)


MONOLITH = _monolith_supports_claude()


class ClaudeHarness(unittest.TestCase):
    """Shared fixture: throwaway $HOME + TMPDIR, and the two ways to drive
    the real cmd_claude."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-claude-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.rc = os.path.join(self.home, ".zshrc")

    # --- helpers ------------------------------------------------------------
    def env(self, path_prefix=None):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["TMPDIR"] = self.home
        e["PATH"] = ((path_prefix + os.pathsep) if path_prefix else "") + e.get("PATH", "")
        for k in ANTHROPIC_VARS:
            e.pop(k, None)
        return e

    def run_install(self, *extra, host=INSTALL_HOST, port=INSTALL_PORT):
        """Drive the REAL `cmd_claude` — via the built monolith when it is in
        sync, otherwise by sourcing the lib/ modules in a zsh subprocess."""
        args = ("--host", host, "--port", port, *extra)
        if MONOLITH:
            cmd = [FERRY, "claude", *args]
        else:
            if not os.path.exists(CLAUDE_MODULE):
                self.fail(
                    "neither the built ferry monolith nor lib/ferry-claude.zsh "
                    "exists — the Claude wiring has not been built yet"
                )
            script = (
                f"source {REPO}/lib/ferry-core.zsh\n"
                f"source {REPO}/lib/ferry-claude.zsh\n"
                'cmd_claude "$@"\n'
            )
            cmd = ["zsh", "-c", script, "ferry-claude", *args]
        return subprocess.run(
            cmd, capture_output=True, text=True, env=self.env(), cwd=self.home,
        )

    def rc_text(self):
        if not os.path.exists(self.rc):
            return ""
        with open(self.rc) as f:
            return f.read()

    def count(self, needle):
        return self.rc_text().count(needle)

    def canonical_block(self):
        text = self.rc_text()
        m = re.search(re.escape(CANON_START) + r"(.*?)" + re.escape(CANON_END), text, re.S)
        self.assertIsNotNone(m, "no canonical claude profiles block in ~/.zshrc")
        return m.group(1)


class WrapperInstallTest(ClaudeHarness):
    """What `ferry claude` writes into ~/.zshrc."""

    def test_fresh_install_writes_one_block_with_all_three_functions(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.rc_text()
        self.assertEqual(text.count(CANON_START), 1)
        self.assertEqual(text.count(CANON_END), 1)
        self.assertIn("claude-ferry() {", text)
        self.assertIn("claude-ferry-local() {", text)
        self.assertIn("claude-ferry-super() {", text)
        self.assertEqual(text.count("claude-ferry() {"), 1)
        self.assertEqual(text.count("claude-ferry-local() {"), 1)
        self.assertEqual(text.count("claude-ferry-super() {"), 1)
        # The endpoint is baked into ALL THREE wrappers, and all authenticate as
        # the local ferry proxy rather than a real Anthropic account.
        self.assertEqual(text.count(f"http://{INSTALL_HOST}:{INSTALL_PORT}"), 3)
        self.assertEqual(text.count("ANTHROPIC_AUTH_TOKEN=local"), 3)

    def test_is_idempotent_and_result_parses(self):
        for _ in range(3):
            self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.count(CANON_START), 1)
        self.assertEqual(self.count(CANON_END), 1)
        self.assertEqual(self.count("claude-ferry-local() {"), 1)
        self.assertEqual(self.count("claude-ferry-super() {"), 1)
        r = subprocess.run(["zsh", "-n", self.rc], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"generated ~/.zshrc does not parse: {r.stderr}")

    def test_strips_a_legacy_alias_that_would_break_the_function(self):
        # An alias above a function of the same name makes zsh expand it inside
        # `name() {`, which is a parse error on every later `source ~/.zshrc`.
        with open(self.rc, "w") as f:
            f.write("alias claude-ferry='echo old'\n"
                    "alias claude-ferry-local='echo old'\n"
                    "alias claude-ferry-super='echo old'\n")
        self.assertEqual(self.run_install().returncode, 0)
        text = self.rc_text()
        self.assertNotIn("alias claude-ferry=", text)
        self.assertNotIn("alias claude-ferry-local=", text)
        self.assertNotIn("alias claude-ferry-super=", text)
        self.assertEqual(self.count(CANON_START), 1)

    def test_writes_no_bare_claude_function(self):
        # Plain `claude` belongs to whatever real installation the host has;
        # only the explicit lane-named wrappers are ferry's.
        self.assertEqual(self.run_install().returncode, 0)
        self.assertIsNone(
            re.search(r"^\s*claude\(\)", self.rc_text(), re.M),
            "bare claude() would shadow the host's own claude installation",
        )

    def test_the_thinking_kill_switch_is_local_only(self):
        """CLAUDE_CODE_DISABLE_THINKING must be set on the local wrapper only.

        The cloud lanes NEED thinking; the local GPU lanes are the ones it is
        switched off for. Both halves of that sentence are pinned here.
        """
        self.assertEqual(self.run_install().returncode, 0)
        block = self.canonical_block()
        self.assertEqual(block.count("CLAUDE_CODE_DISABLE_THINKING=1"), 1,
                         "the kill switch must appear exactly once, in local")
        local_idx = block.index("claude-ferry-local() {")
        cloud_body = block[:local_idx]
        self.assertNotIn("CLAUDE_CODE_DISABLE_THINKING", cloud_body)
        super_idx = block.index("claude-ferry-super() {")
        self.assertNotIn("CLAUDE_CODE_DISABLE_THINKING", block[super_idx:],
                         "the super profile is cloud lanes; they need thinking")

    def test_the_functions_actually_select_the_right_lanes(self):
        """Source the result and prove each wrapper exports the lane it names.

        A stub `claude` earlier on PATH echoes what it received, so this
        observes the wrapper's real effect rather than re-reading the text we
        just wrote.
        """
        self.assertEqual(self.run_install().returncode, 0)
        bindir = os.path.join(self.home, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "claude")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "BASE=$ANTHROPIC_BASE_URL MODEL=$ANTHROPIC_MODEL '
                    'BG=$ANTHROPIC_DEFAULT_HAIKU_MODEL '
                    'SUB=$CLAUDE_CODE_SUBAGENT_MODEL '
                    'THINK=${CLAUDE_CODE_DISABLE_THINKING:-unset}"\n')
        os.chmod(stub, 0o755)

        expectations = {
            "claude-ferry": ("http://testhost:8090", "MODEL=heavy", "BG=flash",
                             "SUB=flash", "THINK=unset"),
            "claude-ferry-local": ("http://testhost:8090", "MODEL=local-orch",
                                   "BG=local-sub", "SUB=local-sub", "THINK=1"),
            "claude-ferry-super": ("http://testhost:8090", "MODEL=heavy",
                                   "BG=super-flash", "SUB=super-flash", "THINK=unset"),
        }
        for fn, needles in expectations.items():
            r = subprocess.run(
                ["zsh", "-c", f"source {self.rc}; {fn}"],
                capture_output=True, text=True,
                env=self.env(path_prefix=bindir),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            for needle in needles:
                self.assertIn(needle, r.stdout, f"{fn} exported the wrong lane env")

        # Control: without sourcing, the bare stub sees no ANTHROPIC_BASE_URL —
        # so the assertions above are observing the wrappers, not an ambient value.
        r = subprocess.run(
            ["zsh", "-c", "claude"], capture_output=True, text=True,
            env=self.env(path_prefix=bindir),
        )
        self.assertNotIn("BASE=http", r.stdout,
                         "ANTHROPIC_BASE_URL leaked into an unsourced shell")


class ClaudeJsonTest(ClaudeHarness):
    """The default action records the endpoint; --wrappers must not touch it."""

    def json_path(self, name="claude.json"):
        return os.path.join(self.home, ".config", "ferry", name)

    def read_json(self, path):
        with open(path) as f:
            return json.load(f)

    def test_default_writes_claude_json_with_host_port_lanes(self):
        self.assertEqual(self.run_install().returncode, 0)
        cfg = self.read_json(self.json_path())
        self.assertEqual(cfg["host"], INSTALL_HOST)
        self.assertEqual(cfg["port"], INSTALL_PORT)
        self.assertTrue(cfg.get("lanes"), "claude.json must record the lane table")

    def test_second_run_snapshots_the_previous_config(self):
        self.assertEqual(self.run_install(host="onehost", port="8090").returncode, 0)
        self.assertEqual(self.run_install(host="twohost", port="8090").returncode, 0)

        # Both .bak spellings the installer could plausibly use: a plain
        # claude.json.bak, or claude.json.<UTC>.bak (timestamp first).
        snaps = sorted(glob.glob(self.json_path() + ".bak*")
                       + glob.glob(self.json_path() + "*.bak"))
        self.assertTrue(snaps, "the pre-existing claude.json was not snapshotted")
        with open(snaps[0]) as f:
            self.assertEqual(json.load(f).get("host"), "onehost",
                             "the .bak does not hold the PREVIOUS config")
        self.assertEqual(self.read_json(self.json_path())["host"], "twohost",
                         "the live config does not hold the latest host")

    def test_wrappers_flag_installs_but_leaves_claude_json_alone(self):
        self.assertEqual(self.run_install().returncode, 0)
        path = self.json_path()
        with open(path, "rb") as f:
            before = f.read()
        before_mtime = os.stat(path).st_mtime_ns

        self.assertEqual(self.run_install("--wrappers").returncode, 0)

        with open(path, "rb") as f:
            self.assertEqual(f.read(), before, "--wrappers rewrote claude.json")
        self.assertEqual(os.stat(path).st_mtime_ns, before_mtime,
                         "--wrappers rewrote (at least re-touched) claude.json")
        # ...and the flag did do its own job, so this test cannot pass vacuously.
        self.assertIn(CANON_START, self.rc_text())


class ScriptContractTest(unittest.TestCase):
    """Cheap static checks pinning the cross-seat wiring contracts."""

    def read(self, rel):
        with open(os.path.join(REPO, rel)) as f:
            return f.read()

    def test_client_bootstrap_gains_the_no_claude_scope(self):
        text = self.read("client-bootstrap.sh")
        self.assertIn("--no-claude", text,
                      "client-bootstrap.sh has no --no-claude scope")
        self.assertIn("claude_mode", text,
                      "client-bootstrap.sh does not record claude_mode in client.json")
        self.assertRegex(text, r"ferry[\"']? claude\b",
                         'client-bootstrap.sh never invokes the CLI\'s "claude" subcommand')

    def test_client_reset_honours_the_recorded_claude_mode(self):
        self.assertIn("claude_mode", self.read("client-reset.sh"))

    def test_client_cleanup_strips_the_claude_marker_block(self):
        self.assertIn(CANON_START, self.read("client-cleanup.sh"))

    def test_install_module_defines_the_claude_wrapper_installer(self):
        self.assertIn("_ferry_install_claude_wrappers", self.read("lib/ferry-install.zsh"))

    def test_host_reset_refreshes_the_claude_wrappers(self):
        self.assertIn("claude --wrappers", self.read("host-reset.sh"))

    # --- the super-lane wiring contract (sibling seats) ----------------------
    # These pin work that lands in OTHER files: client-bootstrap.sh and
    # client-reset.sh route an opencode-super profile, and host-reset.sh writes
    # the host's opencode-super.json. A failure here before the sibling seats
    # land is the contract being enforced loudly, not a bug in this module.

    def test_client_bootstrap_wires_the_super_profile(self):
        self.assertIn("opencode-super", self.read("client-bootstrap.sh"))

    def test_client_reset_reapplies_the_super_profile(self):
        self.assertIn("opencode-super", self.read("client-reset.sh"))

    def test_host_reset_writes_the_super_profile(self):
        self.assertIn("opencode-super", self.read("host-reset.sh"))

    def test_build_orders_claude_before_main(self):
        text = self.read("build.zsh")
        m = re.search(r"MODULES=\(([^)]*)\)", text)
        self.assertIsNotNone(m, "build.zsh MODULES array not found")
        mods = m.group(1).split()
        # build.zsh cats lib/ferry-$m.zsh, so the array holds STEMS ('claude'),
        # not filenames — accept either spelling; the contract is the ORDER.
        claude = next((i for i, n in enumerate(mods)
                       if n in ("claude", "ferry-claude.zsh")), None)
        main = next((i for i, n in enumerate(mods)
                     if n in ("main", "ferry-main.zsh")), None)
        self.assertIsNotNone(claude,
                             "build.zsh MODULES is missing the claude module")
        self.assertIsNotNone(main, "build.zsh MODULES is missing ferry-main.zsh")
        self.assertLess(
            claude, main,
            "the claude module must assemble before ferry-main.zsh (dispatch last)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
