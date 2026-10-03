#!/usr/bin/env python3
"""Stdlib unittest for how much of opencode the client scripts are allowed to touch.

Run:  python3 lib/ferry-clientbootstrap.test.py

client-bootstrap.sh has three scopes — full (the default), --profiles-only and
--no-opencode — client-reset.sh re-applies whichever one the bootstrap recorded
in client.json, and client-cleanup.sh removes whatever any of them left. The
narrow scopes exist for a laptop that already has an opencode setup of its own,
so the property under test is an ABSENCE: that ~/.config/opencode is not read,
written, or snapshotted. Cleanup's version of the same property is the mirror
image — it must take ferry's provider out and leave everything else standing.

An absence is only proved by looking, so these tests run the REAL scripts
end-to-end against a throwaway $HOME and a stub host server that serves
/v1/models and the repo's own `ferry`. Nothing here reimplements the scripts:
a regression that re-widens the scope fails here rather than on the laptop it
was supposed to leave alone.
"""
import difflib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOTSTRAP = os.path.join(REPO, "client-bootstrap.sh")
RESET = os.path.join(REPO, "client-reset.sh")
CLEANUP = os.path.join(REPO, "client-cleanup.sh")
FERRY = os.path.join(REPO, "ferry")
# The two skills the client scripts ship as heredocs. The repo copies are the
# source of truth (`ferry install` / host-reset.sh copy them straight out of
# the checkout); the client scripts are piped into zsh with no checkout to
# read from, so they carry a transcription that has to be kept identical.
GOAL_SKILL_SRC = os.path.join(REPO, "opencode", "skills", "using-the-goal-plugin", "SKILL.md")
FANOUT_SKILL_SRC = os.path.join(REPO, "opencode", "skills", "spawning-subagents", "SKILL.md")

# The lane names the takeover checks against the catalogue. Serving them keeps
# the run free of "host does not serve ..." warnings that would mask a real one.
LANES = ("heavy", "flash", "local-orch", "local-sub")


class StubHost(BaseHTTPRequestHandler):
    """The two endpoints a client bootstrap actually depends on."""

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path.startswith("/v1/models"):
            body = json.dumps({"data": [{"id": l} for l in LANES]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/ferry":
            with open(FERRY, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep the test output readable
        pass


class ClientHarness(unittest.TestCase):
    """Shared fixture: a stub host, a throwaway $HOME, a stub `opencode` on PATH."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHost)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-client-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        # A stub `opencode` on PATH: the guardrail install is gated on the binary
        # existing, so without this the full-scope assertions would pass vacuously.
        self.bin = os.path.join(self.home, "stubbin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "opencode")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub, 0o755)

    # --- helpers ------------------------------------------------------------
    def env(self, master_key=None, port=None):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["PATH"] = self.bin + os.pathsep + e.get("PATH", "")
        e["HOST_NAME"] = "127.0.0.1"
        e["HOST_PORT"] = str(port if port is not None else self.port)
        e["SHARE_PORT"] = str(port if port is not None else self.port)
        # A client is not expected to carry the host's own config pointer.
        e.pop("OPENCODE_CONFIG", None)
        e.pop("OPENCODE_TUI_CONFIG", None)
        # ...nor the author's XDG roots. Since v1.30.1 `ferry opencode` reads
        # both: XDG_CONFIG_HOME decides whether the config it writes IS the
        # global takeover target (and so whether tui.json is mirrored), and
        # XDG_CACHE_HOME is where orphaned plugin package directories are
        # purged from. Left inherited, this suite would write into the author's
        # ~/.config/opencode and delete out of their ~/.cache/opencode while
        # $HOME says otherwise — and the narrow-scope ABSENCE assertions below
        # would be checking the wrong directory entirely.
        e.pop("XDG_CONFIG_HOME", None)
        e.pop("XDG_CACHE_HOME", None)
        # ...and since v1.30.3, XDG_DATA_HOME: it is where ferry installs its own
        # copy of the goal plugin and where cleanup deletes that copy from. Left
        # inherited, a cleanup run under this suite would rm -rf the author's
        # real ~/.local/share/ferry/opencode-goal-plugin. The relocation test
        # below sets it back, deliberately, to a path inside the throwaway $HOME.
        e.pop("XDG_DATA_HOME", None)
        # ...nor a stray master key from the operator's shell.
        e.pop("FERRY_MASTER_KEY", None)
        if master_key is not None:
            e["FERRY_MASTER_KEY"] = master_key
        return e

    def run_script(self, script, *flags, env=None, expect_ok=True):
        p = subprocess.run(["zsh", script, *flags], env=env or self.env(),
                           capture_output=True, text=True, timeout=180)
        if expect_ok:
            self.assertEqual(p.returncode, 0,
                             f"{os.path.basename(script)} {' '.join(flags)} failed:\n"
                             f"{p.stdout}\n{p.stderr}")
        return p

    def path(self, *parts):
        return os.path.join(self.home, *parts)

    def read_json(self, *parts):
        with open(self.path(*parts)) as f:
            return json.load(f)

    def assert_skill_matches_repo(self, source, *parts):
        """The heredoc a client script writes IS the repo file, byte for byte.

        Only this assertion enforces it. The skills exist twice on purpose — the
        host installer copies them out of the checkout, the client scripts carry
        a heredoc because they are curl|zsh'd onto a machine with no checkout —
        so an edit to one and not the other drifts in silence, and the client
        keeps loading last month's wording forever."""
        installed = self.path(".config", "opencode", *parts)
        self.assertTrue(os.path.exists(installed), f"{installed} was not written")
        with open(source, "rb") as f:
            want = f.read()
        with open(installed, "rb") as f:
            got = f.read()
        if want == got:
            return
        rel = os.path.relpath(source, REPO)
        diff = "".join(difflib.unified_diff(
            want.decode("utf-8", "replace").splitlines(keepends=True),
            got.decode("utf-8", "replace").splitlines(keepends=True),
            fromfile=rel, tofile="heredoc in client-bootstrap.sh"))
        self.fail(f"the client-bootstrap.sh heredoc has drifted from {rel}. "
                  f"Regenerate it from the repo file rather than hand-editing:\n{diff}")

    def zshrc(self):
        with open(self.path(".zshrc")) as f:
            return f.read()

    def assert_opencode_dir_absent(self):
        """The whole claim of the narrow scopes, stated once."""
        d = self.path(".config", "opencode")
        self.assertFalse(os.path.exists(d),
                         f"{d} exists; the narrow scope wrote into opencode's own directory: "
                         f"{os.listdir(d) if os.path.isdir(d) else ''}")


class ClientScopeTest(ClientHarness):
    """What each bootstrap scope is allowed to write."""

    # --- --no-opencode ------------------------------------------------------
    def test_no_opencode_installs_the_cli_and_nothing_else(self):
        self.run_script(BOOTSTRAP, "--no-opencode")

        self.assertTrue(os.access(self.path(".local", "bin", "ferry"), os.X_OK))
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["opencode_mode"], "none")
        self.assertEqual(prof["host"], "127.0.0.1")
        self.assertEqual(prof["port"], str(self.port))

        self.assert_opencode_dir_absent()
        # ferry's OWN opencode profiles are opencode config too, and --no-opencode
        # means none of it.
        for name in ("opencode-cloud.json", "opencode-local.json"):
            self.assertFalse(os.path.exists(self.path(".config", "ferry", name)), name)

    def test_no_opencode_leaves_the_shell_alone_except_for_path(self):
        self.run_script(BOOTSTRAP, "--no-opencode")
        rc = self.zshrc()
        self.assertNotIn("ferry opencode profiles", rc)
        self.assertNotIn("alias host-code=", rc)
        self.assertNotIn("opencode-cloud()", rc)
        # ~/.local/bin on PATH is what makes the CLI runnable — not opencode config.
        self.assertIn(".local/bin", rc)

    # --- --profiles-only ----------------------------------------------------
    def test_profiles_only_writes_ferry_profiles_only(self):
        self.run_script(BOOTSTRAP, "--profiles-only")

        self.assert_opencode_dir_absent()
        self.assertEqual(
            self.read_json(".config", "ferry", "client.json")["opencode_mode"], "profiles")

        base = f"http://127.0.0.1:{self.port}/v1"
        cloud = self.read_json(".config", "ferry", "opencode-cloud.json")
        local = self.read_json(".config", "ferry", "opencode-local.json")
        self.assertEqual(cloud["provider"]["ferry"]["options"]["baseURL"], base)
        self.assertEqual(local["provider"]["ferry"]["options"]["baseURL"], base)
        # The lane split is the point of having two profiles at all.
        self.assertEqual(cloud["model"], "ferry/heavy")
        self.assertEqual(local["model"], "ferry/local-orch")

    def test_profiles_only_does_not_wrap_bare_opencode(self):
        self.run_script(BOOTSTRAP, "--profiles-only")
        rc = self.zshrc()
        self.assertIn("opencode-cloud()", rc)
        self.assertIn("opencode-local()", rc)
        # The bare wrapper is the one thing that would change what plain
        # `opencode` does, so it must not be defined in this scope.
        self.assertNotIn("\nopencode() {", rc)
        self.assertIn("alias host-code='opencode-cloud'", rc)

    def test_profiles_only_leaves_an_existing_opencode_config_byte_identical(self):
        """The strongest form of the claim: a real config, before and after."""
        cfg_dir = self.path(".config", "opencode")
        os.makedirs(cfg_dir)
        cfg = os.path.join(cfg_dir, "opencode.json")
        # Deliberately not a real vendor or model name: this repo is public, and
        # a fixture is as published as the README.
        original = json.dumps(
            {"model": "someprovider/some-model",
             "provider": {"someprovider": {"options": {"apiKey": "placeholder"}}}},
            indent=2) + "\n"
        with open(cfg, "w") as f:
            f.write(original)

        self.run_script(BOOTSTRAP, "--profiles-only")

        with open(cfg) as f:
            self.assertEqual(f.read(), original, "the user's opencode.json was rewritten")
        # A snapshot beside it would prove the takeover ran on this file even if
        # the rewrite happened to be identical.
        self.assertEqual(sorted(os.listdir(cfg_dir)), ["opencode.json"])

    def test_guardrails_are_opt_in_outside_full_scope(self):
        self.run_script(BOOTSTRAP, "--profiles-only")
        self.assert_opencode_dir_absent()

        # ...and opt-in-able, because they are additive files, not a takeover.
        self.run_script(BOOTSTRAP, "--profiles-only", "--with-guardrails")
        self.assertTrue(os.path.exists(self.path(".config", "opencode", "command", "fan-out.md")))
        self.assertTrue(os.path.exists(
            self.path(".config", "opencode", "skills", "spawning-subagents", "SKILL.md")))
        # Still no takeover of opencode's own config.
        self.assertFalse(os.path.exists(self.path(".config", "opencode", "opencode.json")))

    # --- the bundled skills -------------------------------------------------
    def test_full_scope_ships_the_goal_plugin_skill(self):
        """The reference for /goal and the goal_* tools rides with the plugin
        that the same run just wired into opencode.json."""
        self.run_script(BOOTSTRAP)
        self.assert_skill_matches_repo(
            GOAL_SKILL_SRC, "skills", "using-the-goal-plugin", "SKILL.md")

    def test_no_guardrails_still_ships_the_goal_plugin_skill(self):
        """The skill follows the PLUGIN, not the guardrails switch.

        README.md and the v1.32.0 release note both promise that a
        `--no-guardrails` client still gets it, and only this test holds the
        gate at client-bootstrap.sh's $OC_MODE check instead of its $GUARDRAILS
        one: folding the skill write into the guardrails branch would leave the
        rest of this suite green while contradicting both documents."""
        self.run_script(BOOTSTRAP, "--no-guardrails")
        self.assert_skill_matches_repo(
            GOAL_SKILL_SRC, "skills", "using-the-goal-plugin", "SKILL.md")
        # Control: the flag really took effect. Without it the test would still
        # pass if --no-guardrails were parsed and then ignored.
        for parts in (("skills", "spawning-subagents", "SKILL.md"),
                      ("skill", "spawning-subagents", "SKILL.md"),
                      ("command", "fan-out.md")):
            self.assertFalse(
                os.path.exists(self.path(".config", "opencode", *parts)),
                f"--no-guardrails still wrote {os.path.join(*parts)}")

    def test_full_scope_ships_the_spawning_subagents_skill(self):
        """The same sync guard for the older heredoc, which never had one.
        In sync as of this commit; the guard is what keeps it that way."""
        self.run_script(BOOTSTRAP)
        self.assert_skill_matches_repo(
            FANOUT_SKILL_SRC, "skills", "spawning-subagents", "SKILL.md")

    def test_no_opencode_does_not_ship_the_goal_plugin_skill(self):
        """--no-opencode wires no plugin, so it installs no skill either — and
        says so, rather than leaving the operator to notice the absence."""
        p = self.run_script(BOOTSTRAP, "--no-opencode")
        self.assert_opencode_dir_absent()
        self.assertIn("using-the-goal-plugin skill was not installed", p.stdout)

    def test_profiles_only_does_not_ship_the_goal_plugin_skill(self):
        """--profiles-only DOES wire the goal plugin, into ferry's own profiles.
        The skill still cannot follow: opencode scans only its own global config
        dir plus project/home .opencode dirs for skills — the directory holding
        an $OPENCODE_CONFIG profile is not one of them (opencode 1.18.29,
        packages/opencode/src/config/paths.ts:23-41 and
        packages/opencode/src/skill/index.ts:204-207). ~/.config/opencode is
        exactly what this mode exists to leave alone, so the skill is skipped —
        and --with-guardrails must not smuggle it in through the side door."""
        p = self.run_script(BOOTSTRAP, "--profiles-only")
        self.assert_opencode_dir_absent()
        self.assertIn("using-the-goal-plugin skill was NOT installed", p.stdout)

        self.run_script(BOOTSTRAP, "--profiles-only", "--with-guardrails")
        self.assertFalse(os.path.exists(self.path(
            ".config", "opencode", "skills", "using-the-goal-plugin", "SKILL.md")))

    def test_the_skill_is_reported_once_per_run_and_never_falsely(self):
        """A client has no llm-ferry checkout, so `ferry opencode` reports the
        skill as not installed — and this script runs it once per config
        target, in a fresh process each time, where ferry's own one-line guard
        cannot see the others. That printed the same line four times in full
        scope and three under --profiles-only, and in THAT scope its trailing
        promise was false: it said client-bootstrap.sh ships the file while
        this script says, a few lines later, that it did not.

        So the takeover runs are passed FERRY_GOAL_SKILL_QUIET=1 and this
        script states the outcome itself, once, for the scope it actually ran.
        The control that the variable is what silences it (and that it silences
        the report, not the install) is in lib/ferry-integrate.test.py:
        TestGoalSkillInstall runs the same command without it and asserts both
        lines."""
        # Counted inside the takeover section only: the closing banner names
        # the skill again on purpose, as part of "here is what you now have".
        def takeover(out):
            start = out.index(">>> Auto-configuring")
            return out[start:out.index(">>> UNIFIED FERRY CLI INSTALLED", start)]

        p = self.run_script(BOOTSTRAP)
        self.assertNotIn("no checkout", p.stdout)
        self.assertEqual(takeover(p.stdout).count("using-the-goal-plugin"), 1,
                         takeover(p.stdout))

        q = self.run_script(BOOTSTRAP, "--profiles-only")
        self.assertNotIn("no checkout", q.stdout)
        self.assertEqual(takeover(q.stdout).count("using-the-goal-plugin"), 1,
                         takeover(q.stdout))
        self.assertIn("skill was NOT installed", takeover(q.stdout))

    def test_reset_says_once_that_it_cannot_ship_the_skill(self):
        """A reset re-writes configs and never skill files — the client copy
        rides in this bootstrap's heredoc and there is no checkout to copy from
        — so the one thing an operator needs is that fact, once, not ferry's
        per-target line three or four times."""
        self.run_script(BOOTSTRAP)
        out = self.run_script(RESET).stdout
        self.assertNotIn("no checkout", out)
        self.assertEqual(out.count("using-the-goal-plugin is bootstrap-only"), 1, out)
        self.assertIn("Re-run client-bootstrap.sh", out)

    # --- the default is unchanged ------------------------------------------
    def test_full_scope_is_still_the_default(self):
        self.run_script(BOOTSTRAP)

        cfg = self.read_json(".config", "opencode", "opencode.json")
        self.assertEqual(cfg["provider"]["ferry"]["options"]["baseURL"],
                         f"http://127.0.0.1:{self.port}/v1")
        self.assertEqual(
            self.read_json(".config", "ferry", "client.json")["opencode_mode"], "full")
        rc = self.zshrc()
        self.assertIn("\nopencode() {", rc)
        self.assertIn("opencode-super()", rc)
        # The bare wrapper routes on the LAST lane used, and `super` is one of
        # the lanes it must know about.
        self.assertIn('"$lane" == "super"', rc)
        self.assertIn("alias host-code='opencode'", rc)
        self.assertTrue(os.path.exists(self.path(".config", "opencode", "command", "fan-out.md")))
        # v1.30.1: the goal plugin's TUI half is loaded from tui.json and
        # nowhere else, so the full takeover has to write one beside the config
        # it just took over (packages/opencode/src/config/tui.ts:157-210).
        tui = self.read_json(".config", "opencode", "tui.json")
        self.assertTrue(any("opencode-goal-pro-max-complete-plugin@" in str(p)
                            for p in tui["plugin"]),
                        tui)

    def test_full_scope_bare_opencode_follows_the_super_last_lane(self):
        """Behavioral last-lane routing: `super` written to last-lane, bare
        `opencode` run, the super profile is the one it picks.

        The stub `opencode` echoes $OPENCODE_CONFIG, so the routing decision is
        observed, not re-read from the text the bootstrap just wrote."""
        self.run_script(BOOTSTRAP)
        stub = os.path.join(self.bin, "opencode")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "CONFIG=$OPENCODE_CONFIG"\n')
        os.chmod(stub, 0o755)

        # Control first: with no last-lane file the default is the cloud pair.
        r = subprocess.run(["zsh", "-c", f"source {self.path('.zshrc')}; opencode"],
                           env=self.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("opencode-cloud.json", r.stdout)

        with open(self.path(".config", "ferry", "last-lane"), "w") as f:
            f.write("super\n")
        r = subprocess.run(["zsh", "-c", f"source {self.path('.zshrc')}; opencode"],
                           env=self.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("opencode-super.json", r.stdout)

    def test_an_unknown_flag_stops_before_touching_anything(self):
        p = self.run_script(BOOTSTRAP, "--wat", expect_ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Unknown flag", p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.path(".config")))

    # --- client-reset.sh honours the recorded scope -------------------------
    def test_reset_re_applies_the_saved_narrow_scope(self):
        self.run_script(BOOTSTRAP, "--profiles-only")
        self.assert_opencode_dir_absent()

        out = self.run_script(RESET).stdout
        self.assertIn("PROFILES ONLY", out)
        # The catch-up must not be the thing that finally widens the machine.
        self.assert_opencode_dir_absent()
        self.assertTrue(os.path.exists(self.path(".config", "ferry", "opencode-cloud.json")))

    def test_reset_defaults_to_full_for_a_profile_without_the_key(self):
        """Profiles written before opencode_mode existed must keep working."""
        self.run_script(BOOTSTRAP)
        prof_path = self.path(".config", "ferry", "client.json")
        prof = self.read_json(".config", "ferry", "client.json")
        del prof["opencode_mode"]
        with open(prof_path, "w") as f:
            json.dump(prof, f)
        shutil.rmtree(self.path(".config", "opencode"))

        self.run_script(RESET)
        self.assertTrue(os.path.exists(self.path(".config", "opencode", "opencode.json")),
                        "a legacy profile lost the full takeover it was set up with")

    def test_reset_flag_overrides_without_rewriting_the_profile(self):
        self.run_script(BOOTSTRAP)  # full
        before = self.read_json(".config", "ferry", "client.json")
        shutil.rmtree(self.path(".config", "opencode"))

        self.run_script(RESET, "--profiles-only")

        self.assert_opencode_dir_absent()
        self.assertEqual(self.read_json(".config", "ferry", "client.json"), before,
                         "client-reset.sh rewrote client.json; the override is per-run only")

    def test_reset_rejects_an_unparseable_scope(self):
        self.run_script(BOOTSTRAP, "--no-opencode")
        prof_path = self.path(".config", "ferry", "client.json")
        prof = self.read_json(".config", "ferry", "client.json")
        prof["opencode_mode"] = "everything"
        with open(prof_path, "w") as f:
            json.dump(prof, f)

        p = self.run_script(RESET, expect_ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("unrecognised opencode_mode", p.stdout + p.stderr)


class KeyedStubHost(StubHost):
    """A front door running the v1.22.0 master-key auth: /v1/models answers
    401 without the Bearer key and 200 with it. /ferry stays open — the share
    server is unauthenticated by design."""

    REQUIRED_KEY = "test-master-key"

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            if self.headers.get("Authorization", "") != f"Bearer {self.REQUIRED_KEY}":
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        StubHost.do_GET(self)


class MasterKeyTest(ClientHarness):
    """The optional shared key: bootstrap accepts it (env or --key), stores it
    in client.json, probes with it, hints when it is missing, never prints it."""

    @classmethod
    def setUpClass(cls):
        ClientHarness.setUpClass()
        cls.keyed = ThreadingHTTPServer(("127.0.0.1", 0), KeyedStubHost)
        cls.keyed_port = cls.keyed.server_address[1]
        cls.keyed_thread = threading.Thread(target=cls.keyed.serve_forever, daemon=True)
        cls.keyed_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.keyed.shutdown()
        cls.keyed.server_close()
        ClientHarness.tearDownClass()

    def test_the_probe_carries_the_key_and_the_profile_stores_it(self):
        # The stub host 401s every Bearer-less request, so a SUCCESS here is
        # only reachable by the probe carrying `Authorization: Bearer <key>`.
        p = self.run_script(BOOTSTRAP, "--no-opencode",
                            env=self.env(master_key=KeyedStubHost.REQUIRED_KEY,
                                         port=self.keyed_port))
        self.assertIn("SUCCESS: Connected", p.stdout)

        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["master_key"], KeyedStubHost.REQUIRED_KEY)
        # The rest of the profile keeps its shape with the key present.
        self.assertEqual(prof["host"], "127.0.0.1")
        self.assertEqual(prof["port"], str(self.keyed_port))
        self.assertEqual(prof["opencode_mode"], "none")
        # And the key never reaches the transcript.
        self.assertNotIn(KeyedStubHost.REQUIRED_KEY, p.stdout)
        self.assertNotIn(KeyedStubHost.REQUIRED_KEY, p.stderr)

    def test_the_key_can_come_as_a_flag_too(self):
        self.run_script(BOOTSTRAP, "--no-opencode", "--key", KeyedStubHost.REQUIRED_KEY,
                        env=self.env(port=self.keyed_port))
        self.assertEqual(self.read_json(".config", "ferry", "client.json")["master_key"],
                         KeyedStubHost.REQUIRED_KEY)

    def test_without_a_key_the_profile_has_no_master_key_field(self):
        # The field must be ABSENT, not empty: its absence is how every reader
        # knows this host takes no key.
        self.run_script(BOOTSTRAP, "--no-opencode")
        self.assertNotIn("master_key", self.read_json(".config", "ferry", "client.json"))

    def test_a_keyed_host_hints_at_the_key_when_probed_bare(self):
        p = self.run_script(BOOTSTRAP, "--no-opencode",
                            env=self.env(port=self.keyed_port), expect_ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("requires a key", p.stdout + p.stderr)
        self.assertIn("FERRY_MASTER_KEY", p.stdout + p.stderr)
        # Nothing was configured against a host that refuses every request.
        self.assertFalse(os.path.exists(self.path(".config", "ferry", "client.json")))

    def test_an_old_host_without_enroll_keeps_the_master_key(self):
        self.run_script(BOOTSTRAP, "--no-opencode",
                        env=self.env(master_key=KeyedStubHost.REQUIRED_KEY,
                                     port=self.keyed_port))
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["master_key"], KeyedStubHost.REQUIRED_KEY)
        self.assertNotIn("api_key", prof)
        self.assertNotIn("key_name", prof)


class EnrollingStubHost(KeyedStubHost):
    """A v1.39.0 host: POST /v1/ferry/keys/enroll mints a device key for the
    master, and /v1/models accepts either credential afterwards."""

    DEVICE_KEY = "fk-laptop-" + "a" * 32
    ENROLLS = []
    # "ok" mints DEVICE_KEY; "malformed" answers 200 with a key that is not a
    # device key; "unauthorized" answers 401 to every enroll.
    MODE = "ok"

    def do_GET(self):  # noqa: N802
        # Any well-formed fk- credential is accepted, not just DEVICE_KEY: the
        # supplied-device-key tests probe with their own fk-<slug>-<body>
        # tokens (never minted by this stub) to exercise client-bootstrap.sh's
        # read-back logic, not this host's auth.
        auth = self.headers.get("Authorization", "")
        if self.path.startswith("/v1/models") and auth.startswith("Bearer fk-"):
            return StubHost.do_GET(self)
        KeyedStubHost.do_GET(self)

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if (self.path != "/v1/ferry/keys/enroll"
                or self.headers.get("Authorization", "") != f"Bearer {self.REQUIRED_KEY}"
                or EnrollingStubHost.MODE == "unauthorized"):
            if self.path == "/v1/ferry/keys/enroll":
                EnrollingStubHost.ENROLLS.append(json.loads(raw or b"{}"))
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        doc = json.loads(raw)
        EnrollingStubHost.ENROLLS.append(doc)
        key = self.DEVICE_KEY if EnrollingStubHost.MODE == "ok" else "sk-Not_A_Device_Key"
        body = json.dumps({"name": doc["name"], "key": key}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DeviceKeyTest(ClientHarness):
    """v1.39.0 — a bootstrap given the master key trades it for a per-device
    key and stores only that; an older host keeps the master key path."""

    @classmethod
    def setUpClass(cls):
        ClientHarness.setUpClass()
        cls.enrolling = ThreadingHTTPServer(("127.0.0.1", 0), EnrollingStubHost)
        cls.enrolling_port = cls.enrolling.server_address[1]
        threading.Thread(target=cls.enrolling.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.enrolling.shutdown()
        cls.enrolling.server_close()
        ClientHarness.tearDownClass()

    # The script derives the enroll name from `hostname -s`, lower-cased. A
    # stub `hostname` on PATH pins it, so a real host name carrying `_` (which
    # a device-key name may not) cannot break these tests.
    STUB_HOSTNAME = "Test-Laptop"

    def setUp(self):
        super().setUp()
        EnrollingStubHost.ENROLLS.clear()
        EnrollingStubHost.MODE = "ok"
        self.addCleanup(setattr, EnrollingStubHost, "MODE", "ok")
        stub = os.path.join(self.bin, "hostname")
        with open(stub, "w") as f:
            f.write(f"#!/bin/sh\necho {self.STUB_HOSTNAME}\n")
        os.chmod(stub, 0o755)

    def keyed_env(self):
        return self.env(master_key=KeyedStubHost.REQUIRED_KEY, port=self.enrolling_port)

    def expected_name(self):
        return self.STUB_HOSTNAME.lower()

    def write_prior_profile(self, host, key_name):
        os.makedirs(self.path(".config", "ferry"), exist_ok=True)
        with open(self.path(".config", "ferry", "client.json"), "w") as f:
            json.dump({"host": host, "port": str(self.enrolling_port),
                       "api_key": "fk-old-" + "b" * 32, "key_name": key_name}, f)

    def assert_master_key_fallback(self, p):
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["master_key"], KeyedStubHost.REQUIRED_KEY)
        self.assertNotIn("api_key", prof)
        self.assertNotIn("key_name", prof)
        self.assertIn("Device-key enroll unavailable", p.stdout)
        self.assertNotIn(KeyedStubHost.REQUIRED_KEY, p.stdout + p.stderr)

    def test_a_prior_profile_for_another_host_does_not_replace(self):
        # key_name belongs to the host that minted it; asking THIS host to
        # replace that name could rotate some other machine's key.
        self.write_prior_profile("other-host.local", "someone-else")
        self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        self.assertEqual(EnrollingStubHost.ENROLLS, [{"name": self.expected_name()}])

    def test_a_malformed_enroll_reply_falls_back_to_the_master_key(self):
        EnrollingStubHost.MODE = "malformed"
        p = self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        self.assertEqual(len(EnrollingStubHost.ENROLLS), 1)
        self.assert_master_key_fallback(p)
        self.assertNotIn("sk-Not_A_Device_Key",
                         open(self.path(".config", "ferry", "client.json")).read())

    def test_an_unauthorized_enroll_falls_back_to_the_master_key(self):
        EnrollingStubHost.MODE = "unauthorized"
        p = self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        self.assertEqual(len(EnrollingStubHost.ENROLLS), 1)
        self.assert_master_key_fallback(p)

    def test_bootstrap_trades_the_master_for_a_device_key(self):
        p = self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["api_key"], EnrollingStubHost.DEVICE_KEY)
        self.assertEqual(prof["key_name"], self.expected_name())
        self.assertNotIn("master_key", prof)
        self.assertEqual(EnrollingStubHost.ENROLLS, [{"name": self.expected_name()}])
        for secret in (KeyedStubHost.REQUIRED_KEY, EnrollingStubHost.DEVICE_KEY):
            self.assertNotIn(secret, p.stdout + p.stderr)
        self.assertIn("device key", p.stdout)

    def test_a_rerun_rotates_the_same_name(self):
        self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        self.assertEqual(EnrollingStubHost.ENROLLS[-1],
                         {"name": self.expected_name(), "replace": True})

    def test_the_generated_configs_carry_the_device_key(self):
        self.run_script(BOOTSTRAP, "--profiles-only", env=self.keyed_env())
        with open(self.path(".config", "ferry", "opencode-cloud.json")) as f:
            text = f.read()
        self.assertIn(EnrollingStubHost.DEVICE_KEY, text)
        self.assertNotIn(KeyedStubHost.REQUIRED_KEY, text)

    def test_migration_leaves_no_master_key_anywhere_under_home(self):
        """A pre-1.39 client (master baked into every config) re-runs the
        bootstrap and gets a device key. `ferry opencode` / `ferry claude`
        back up each file before rewriting it; those backups must not keep the
        master. They are redacted, not deleted: the backups stay useful."""
        stub = os.path.join(self.bin, "claude")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub, 0o755)
        master = KeyedStubHost.REQUIRED_KEY
        # Run 1: a host without enroll, so the master is stored and baked in.
        EnrollingStubHost.MODE = "unauthorized"
        self.run_script(BOOTSTRAP, env=self.keyed_env())
        self.assertEqual(self.read_json(".config", "ferry", "client.json")["master_key"],
                         master)
        # Run 2: the host now enrolls.
        EnrollingStubHost.MODE = "ok"
        p = self.run_script(BOOTSTRAP, env=self.keyed_env())
        self.assertEqual(self.read_json(".config", "ferry", "client.json")["api_key"],
                         EnrollingStubHost.DEVICE_KEY)
        holders, redacted = [], []
        for root, _dirs, files in os.walk(self.home):
            for name in files:
                full = os.path.join(root, name)
                with open(full, "rb") as fh:
                    data = fh.read()
                if master.encode() in data:
                    holders.append(os.path.relpath(full, self.home))
                if b"<redacted: replaced by ferry device key>" in data:
                    redacted.append(os.path.relpath(full, self.home))
        self.assertEqual(holders, [], "the master key survived the migration in these files")
        # The backups were redacted, not removed.
        self.assertTrue(any(r.endswith(".jsonc") for r in redacted), redacted)
        self.assertTrue(any(r.endswith(".bak") for r in redacted), redacted)
        self.assertNotIn(master, p.stdout + p.stderr)

    def test_a_supplied_device_key_is_stored_as_api_key_without_enrolling(self):
        # --key fk-… is a device key someone minted with `ferry keys add`:
        # there is no master to trade, so no enroll, and it is never labelled
        # a master key.
        p = self.run_script(BOOTSTRAP, "--no-opencode", "--key", EnrollingStubHost.DEVICE_KEY,
                            env=self.env(port=self.enrolling_port))
        self.assertEqual(EnrollingStubHost.ENROLLS, [])
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["api_key"], EnrollingStubHost.DEVICE_KEY)
        self.assertNotIn("master_key", prof)
        self.assertEqual(prof["key_name"], "laptop")
        self.assertIn("Using the supplied device key.", p.stdout)
        self.assertNotIn("enroll unavailable", p.stdout)
        self.assertNotIn("storing the master key", p.stdout)

    def test_a_supplied_long_name_key_stores_no_key_name(self):
        # mint_token() truncates normalize_name(name) to SLUG_MAX=24 chars
        # then rstrips a trailing dash, so a read-back slug of 23 or 24 chars
        # is ambiguous: it may be the WHOLE stored name, or it may be a
        # longer name cut down to that length. Storing it as key_name risks a
        # later master bootstrap rotating the wrong (truncated) name and
        # orphaning this key, so it must be omitted whenever the slug could
        # have been truncated.
        long_key = "fk-" + ("x" * 24) + "-" + ("a" * 32)
        p = self.run_script(BOOTSTRAP, "--no-opencode", "--key", long_key,
                            env=self.env(port=self.enrolling_port))
        self.assertEqual(EnrollingStubHost.ENROLLS, [])
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["api_key"], long_key)
        self.assertNotIn("key_name", prof)
        self.assertIn("Using the supplied device key.", p.stdout)

    def test_a_supplied_short_name_key_still_stores_key_name(self):
        # A slug well under the truncation boundary (22 chars or fewer)
        # cannot have been produced by truncation, so it safely equals the
        # real stored key name and is kept.
        short_key = "fk-" + ("x" * 20) + "-" + ("a" * 32)
        self.run_script(BOOTSTRAP, "--no-opencode", "--key", short_key,
                        env=self.env(port=self.enrolling_port))
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["api_key"], short_key)
        self.assertEqual(prof["key_name"], "x" * 20)

    def test_client_json_is_private(self):
        for env in (self.keyed_env(), self.env()):
            with self.subTest(keyed=env.get("FERRY_MASTER_KEY") is not None):
                path = self.path(".config", "ferry", "client.json")
                if os.path.exists(path):
                    os.chmod(path, 0o644)   # a profile from an older bootstrap
                self.run_script(BOOTSTRAP, "--no-opencode", env=env)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_reset_threads_the_device_key_through(self):
        self.run_script(BOOTSTRAP, "--profiles-only", env=self.keyed_env())
        os.remove(self.path(".config", "ferry", "opencode-cloud.json"))
        out = self.run_script(RESET, env=self.env(port=self.enrolling_port)).stdout
        with open(self.path(".config", "ferry", "opencode-cloud.json")) as f:
            self.assertIn(EnrollingStubHost.DEVICE_KEY, f.read())
        self.assertNotIn(EnrollingStubHost.DEVICE_KEY, out)


class ClientCleanupTest(ClientHarness):
    """What client-cleanup.sh takes away — and, more importantly, what it leaves."""

    def test_it_undoes_a_full_bootstrap(self):
        self.run_script(BOOTSTRAP)
        self.assertTrue(os.path.exists(self.path(".local", "bin", "ferry")))

        self.run_script(CLEANUP)

        self.assertFalse(os.path.exists(self.path(".local", "bin", "ferry")))
        self.assertFalse(os.path.exists(self.path(".config", "ferry")))
        for gone in (("command", "fan-out.md"),
                     ("skills", "spawning-subagents", "SKILL.md")):
            self.assertFalse(os.path.exists(self.path(".config", "opencode", *gone)), gone)
        rc = self.zshrc()
        self.assertNotIn("ferry opencode profiles", rc)
        self.assertNotIn("alias host-code=", rc)
        self.assertNotIn("opencode-cloud()", rc)

    def test_it_removes_the_singular_skill_spelling_too(self):
        """`ferry opencode` installs to skill/, the bootstrap to skills/. Both go."""
        self.run_script(BOOTSTRAP)
        singular = self.path(".config", "opencode", "skill", "spawning-subagents")
        os.makedirs(singular)
        with open(os.path.join(singular, "SKILL.md"), "w") as f:
            f.write("---\nname: spawning-subagents\n---\n")

        self.run_script(CLEANUP)

        self.assertFalse(os.path.exists(os.path.join(singular, "SKILL.md")))

    def test_it_removes_the_goal_plugin_skill_under_both_spellings(self):
        """client-bootstrap.sh writes skills/, a host-side `ferry opencode`
        writes skill/. A machine that has been both must come out clean."""
        self.run_script(BOOTSTRAP)
        plural = self.path(".config", "opencode", "skills", "using-the-goal-plugin")
        singular = self.path(".config", "opencode", "skill", "using-the-goal-plugin")
        self.assertTrue(os.path.exists(os.path.join(plural, "SKILL.md")))
        os.makedirs(singular)
        with open(os.path.join(singular, "SKILL.md"), "w") as f:
            f.write("---\nname: using-the-goal-plugin\n---\n")

        self.run_script(CLEANUP)

        for d in (plural, singular):
            self.assertFalse(os.path.exists(os.path.join(d, "SKILL.md")), d)
            self.assertFalse(os.path.isdir(d), f"{d} should have been rmdir'd")

    def test_it_leaves_a_still_occupied_skill_dir_and_a_stranger_alone(self):
        """rmdir, never rm -rf: a directory the removal did not just empty stays
        standing, and a skill of the user's own is none of ferry's business."""
        self.run_script(BOOTSTRAP)
        ours = self.path(".config", "opencode", "skills", "using-the-goal-plugin")
        with open(os.path.join(ours, "NOTES.md"), "w") as f:
            f.write("my own notes\n")
        stranger = self.path(".config", "opencode", "skills", "my-own-skill")
        os.makedirs(stranger)
        with open(os.path.join(stranger, "SKILL.md"), "w") as f:
            f.write("---\nname: my-own-skill\n---\n")

        self.run_script(CLEANUP)

        self.assertFalse(os.path.exists(os.path.join(ours, "SKILL.md")))
        self.assertTrue(os.path.exists(os.path.join(ours, "NOTES.md")),
                        "rmdir emptied a directory that still held a user file")
        self.assertTrue(os.path.exists(os.path.join(stranger, "SKILL.md")),
                        "cleanup ate a skill ferry never installed")

    def test_it_strips_ferrys_provider_and_leaves_the_rest_of_the_config(self):
        self.run_script(BOOTSTRAP)
        cfg_path = self.path(".config", "opencode", "opencode.json")
        cfg = self.read_json(".config", "opencode", "opencode.json")
        # A key ferry never wrote, and a second provider that is not ferry's.
        cfg["mcp"] = {"mine": {"command": ["true"]}}
        cfg["provider"]["someprovider"] = {"options": {"apiKey": "placeholder"}}
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)

        self.run_script(CLEANUP)

        after = self.read_json(".config", "opencode", "opencode.json")
        self.assertNotIn("ferry", after.get("provider", {}))
        self.assertIn("someprovider", after["provider"])
        self.assertEqual(after["mcp"], {"mine": {"command": ["true"]}})

    def test_it_strips_a_ferry_provider_with_a_real_master_key(self):
        """v1.22.0: an authed setup writes the front door's master key, not
        'local', so the apiKey-literal fingerprint would strand it. The
        fingerprint is the provider OBJECT KEY ('ferry'), which is also what
        keeps a user's unrelated openai-compatible provider — same npm, its
        own baseURL, its own key — standing."""
        self.run_script(BOOTSTRAP)
        cfg_path = self.path(".config", "opencode", "opencode.json")

        def bake_authed_shape():
            cfg = self.read_json(".config", "opencode", "opencode.json")
            cfg["provider"]["ferry"]["options"]["apiKey"] = "sk-ferry-master-key"
            # The control: an openai-compatible provider that is NOT ferry's.
            cfg["provider"]["someprovider"] = {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://localhost:9999/v1",
                            "apiKey": "their-own-key"},
            }
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent=2)

        # Dry run detects the authed shape before the real pass removes it.
        bake_authed_shape()
        dry = self.run_script(CLEANUP, "--dry-run").stdout
        self.assertIn("would strip the ferry provider block", dry)
        self.assertIn("ferry", self.read_json(".config", "opencode",
                                              "opencode.json")["provider"])

        bake_authed_shape()
        self.run_script(CLEANUP)

        after = self.read_json(".config", "opencode", "opencode.json")
        self.assertNotIn("ferry", after.get("provider", {}))
        self.assertIn("someprovider", after["provider"])
        self.assertEqual(after["provider"]["someprovider"]["options"]["apiKey"],
                         "their-own-key")

    def test_a_config_with_no_ferry_provider_is_left_alone(self):
        """The --profiles-only case: nothing of ours is in there to remove."""
        cfg_dir = self.path(".config", "opencode")
        os.makedirs(cfg_dir)
        cfg = os.path.join(cfg_dir, "opencode.json")
        original = json.dumps({"model": "someprovider/some-model"}, indent=2) + "\n"
        with open(cfg, "w") as f:
            f.write(original)
        self.run_script(BOOTSTRAP, "--profiles-only")

        self.run_script(CLEANUP)

        with open(cfg) as f:
            self.assertEqual(f.read(), original)
        self.assertEqual(sorted(os.listdir(cfg_dir)), ["opencode.json"])
        # ...while ferry's own files are gone.
        self.assertFalse(os.path.exists(self.path(".config", "ferry")))

    def test_dry_run_changes_nothing(self):
        self.run_script(BOOTSTRAP)
        before = {
            "ferry_bin": os.path.exists(self.path(".local", "bin", "ferry")),
            "profiles": sorted(os.listdir(self.path(".config", "ferry"))),
            "zshrc": self.zshrc(),
            "opencode": sorted(os.listdir(self.path(".config", "opencode"))),
        }

        out = self.run_script(CLEANUP, "--dry-run").stdout
        self.assertIn("DRY RUN", out)

        self.assertEqual(before["ferry_bin"], os.path.exists(self.path(".local", "bin", "ferry")))
        self.assertEqual(before["profiles"], sorted(os.listdir(self.path(".config", "ferry"))))
        self.assertEqual(before["zshrc"], self.zshrc())
        self.assertEqual(before["opencode"], sorted(os.listdir(self.path(".config", "opencode"))))

    def test_full_refuses_without_yes_and_keeps_the_session_store(self):
        """--full deletes chat history, so a piped fat-finger must not reach it."""
        store = self.path(".local", "share", "opencode")
        os.makedirs(store)
        with open(os.path.join(store, "sessions.db"), "w") as f:
            f.write("not really a database")
        self.run_script(BOOTSTRAP)

        p = self.run_script(CLEANUP, "--full", expect_ok=False)

        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Refusing --full without --yes", p.stdout + p.stderr)
        self.assertTrue(os.path.exists(os.path.join(store, "sessions.db")))
        # The refusal is a full stop, not a partial run.
        self.assertTrue(os.path.exists(self.path(".local", "bin", "ferry")))

    def test_default_scope_keeps_the_session_store(self):
        store = self.path(".local", "share", "opencode")
        os.makedirs(store)
        with open(os.path.join(store, "sessions.db"), "w") as f:
            f.write("not really a database")
        self.run_script(BOOTSTRAP)

        out = self.run_script(CLEANUP).stdout

        self.assertTrue(os.path.exists(os.path.join(store, "sessions.db")))
        self.assertIn("Keeping it", out)

    def test_full_with_yes_removes_the_session_store(self):
        store = self.path(".local", "share", "opencode")
        os.makedirs(store)
        with open(os.path.join(store, "sessions.db"), "w") as f:
            f.write("not really a database")
        self.run_script(BOOTSTRAP)

        self.run_script(CLEANUP, "--full", "--yes")

        self.assertFalse(os.path.exists(store))

    def test_it_runs_clean_on_a_machine_that_was_never_bootstrapped(self):
        p = self.run_script(CLEANUP)
        self.assertIn("Not installed", p.stdout)


# The three things `ferry opencode` writes for the goal plugin, spelled out here
# so a drift in either direction fails: the canonical spec and the verbatim
# /goal command (lib/ferry-integrate.zsh's GOAL_PLUGIN / GOAL_COMMAND).
GOAL_SPEC = ("opencode-goal-pro-max-complete-plugin@https://github.com/sblattj/"
             "opencode-goal-pro-max-complete-plugin/archive/refs/tags/v1.2.0.tar.gz")
# What every ferry up to v1.43.0 wrote, before the plugin's repo was renamed
# (sblattj/OpenCode-goal-plugin is gone). Cleanup must still take it out.
OLD_GOAL_SPEC = ("opencode-goal-plugin@https://github.com/sblattj/OpenCode-goal-plugin"
                 "/archive/refs/tags/v0.11.0.tar.gz")
GOAL_COMMAND = {
    "description": "Set a session-scoped goal and auto-continue until complete.",
    "template": "$ARGUMENTS",
    "agent": "build",
}
LOCAL_FORK = "/Users/someone/src/OpenCode-goal-plugin/index.js"

# v1.30.3: opencode cannot load the TUI half out of its own package cache — that
# directory's name carries the spec verbatim, and Bun's plugin runner splits a
# module path at the first colon, so the bundle's bare `solid-js` import is
# never rewritten. ferry therefore keeps a colon-free COPY it owns and points
# tui.json at THAT. The marker file inside is what makes the copy ferry's to
# delete; its content is "<spec>\n<ref>\n<pkg>\n".
GOAL_TUI_MARKER = ".ferry-goal-plugin"
GOAL_PKG = "opencode-goal-pro-max-complete-plugin"
GOAL_REF = "v1.2.0"
# The managed copy's directory name did NOT follow the rename: it is still
# <data>/ferry/opencode-goal-plugin, whatever package it holds.
GOAL_TUI_DIRNAME = "opencode-goal-plugin"


class ClientCleanupGoalPluginTest(ClientHarness):
    """v1.30.2: cleanup takes the goal plugin back out of BOTH config files.

    v1.30.1 started mirroring the plugin into tui.json, and cleanup only ever
    stripped provider.ferry — so a cleaned client kept ferry's plugin entry in
    opencode.json and tui.json plus its /goal command, and the next bare
    `opencode` still loaded them. The line these tests defend is the one between
    what ferry WROTE (ours to remove) and what the user owns: a local-path fork
    of the plugin, and a /goal command they edited.

    v1.30.3 moves that line: ferry now installs its OWN colon-free copy of the
    package (opencode cannot load the TUI half out of its package cache) and
    points tui.json at it with a file:// URL. So one local path IS ferry's, and
    a third thing has to come back out — the directory itself, and only when it
    carries ferry's marker.
    """

    # --- helpers ------------------------------------------------------------
    def write_oc(self, cfg, name="opencode.json"):
        d = self.path(".config", "opencode")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w") as f:
            json.dump(cfg, f, indent=2)
        return p

    def oc_files(self):
        return sorted(os.listdir(self.path(".config", "opencode")))

    def raw(self, *parts):
        with open(self.path(*parts), "rb") as f:
            return f.read()

    def managed_dir(self, data_home=None):
        """Ferry's own copy of the plugin — the shell's
        ${XDG_DATA_HOME:-$HOME/.local/share}/ferry/opencode-goal-plugin."""
        base = data_home if data_home is not None else self.path(".local", "share")
        return os.path.join(base, "ferry", GOAL_TUI_DIRNAME)

    def install_managed(self, data_home=None, marker=True):
        """Plant that copy, with or without the marker that makes it ferry's."""
        d = self.managed_dir(data_home)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "package.json"), "w") as f:
            json.dump({"name": GOAL_PKG, "version": GOAL_REF.lstrip("v")}, f)
        if marker:
            with open(os.path.join(d, GOAL_TUI_MARKER), "w") as f:
                f.write(f"{GOAL_SPEC}\n{GOAL_REF}\n{GOAL_PKG}\n")
        return d

    def managed_entry(self, data_home=None):
        """Exactly what ferry writes into tui.json: pathlib's file:// URL."""
        return pathlib.Path(self.managed_dir(data_home)).as_uri()

    def section(self, out, needle):
        """One '>>> ...' section of cleanup's output. Several sections print
        'Not present — skipping.', so a bare assertIn proves nothing about
        which one did."""
        lines = out.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith(">>>") and needle in ln:
                body = [ln]
                for nxt in lines[i + 1:]:
                    if nxt.startswith(">>>"):
                        break
                    body.append(nxt)
                return "\n".join(body)
        self.fail(f"no section matching {needle!r} in:\n{out}")

    # --- end to end ---------------------------------------------------------
    def test_a_full_bootstrap_then_cleanup_leaves_no_goal_plugin_behind(self):
        """The real thing: bootstrap writes all three, cleanup removes all three."""
        self.run_script(BOOTSTRAP)
        before = self.read_json(".config", "opencode", "opencode.json")
        # Guard the premise — if the bootstrap stopped writing these, this test
        # would pass vacuously.
        self.assertEqual(before["plugin"], [GOAL_SPEC])
        self.assertEqual(before["command"]["goal"], GOAL_COMMAND)
        self.assertEqual(self.read_json(".config", "opencode", "tui.json")["plugin"],
                         [GOAL_SPEC])

        self.run_script(CLEANUP)

        after = self.read_json(".config", "opencode", "opencode.json")
        self.assertNotIn("plugin", after)
        self.assertNotIn("command", after)
        self.assertNotIn("ferry", after.get("provider", {}))
        self.assertFalse(os.path.exists(self.path(".config", "opencode", "tui.json")))

    # --- opencode.json: plugin ---------------------------------------------
    def test_the_canonical_spec_goes_and_a_local_fork_stays(self):
        self.write_oc({"plugin": [GOAL_SPEC, LOCAL_FORK, "unrelated-plugin"]})

        out = self.run_script(CLEANUP).stdout

        after = self.read_json(".config", "opencode", "opencode.json")
        self.assertEqual(after["plugin"], [LOCAL_FORK, "unrelated-plugin"])
        self.assertIn("Removed goal plugin entry", out)

    def test_every_older_spelling_goes_including_the_tuple_form(self):
        """ensure_goal_plugin() rewrote all of these onto machines over time."""
        self.write_oc({"plugin": [
            "opencode-goal-plugin",
            OLD_GOAL_SPEC,
            "github:sblattj/opencode-goal-pro-max-complete-plugin#v1.0.0",
            "@prevalentware/opencode-goal-plugin",
            "github:sblattj/OpenCode-goal-plugin#v0.9.1",
            "git+https://github.com/sblattj/OpenCode-goal-plugin.git",
            ["opencode-goal-plugin@https://github.com/willytop8/OpenCode-goal-plugin"
             "/archive/refs/tags/v0.8.0.tar.gz", {"enabled": True}],
            "keep-me",
        ]})

        self.run_script(CLEANUP)

        self.assertEqual(self.read_json(".config", "opencode", "opencode.json")["plugin"],
                         ["keep-me"])

    def test_a_local_fork_on_its_own_leaves_the_file_byte_identical(self):
        """A path is the ONLY way to name a private fork, so it is never ours."""
        p = self.write_oc({"plugin": [LOCAL_FORK], "model": "someprovider/m"})
        before = self.raw(".config", "opencode", "opencode.json")

        dry = self.run_script(CLEANUP, "--dry-run").stdout
        self.assertIn("would be left alone", dry)
        self.run_script(CLEANUP)

        self.assertEqual(before, self.raw(".config", "opencode", "opencode.json"))
        self.assertEqual(self.oc_files(), ["opencode.json"], "a snapshot was taken")

    # --- opencode.json: command.goal ---------------------------------------
    def test_a_verbatim_goal_command_goes_and_the_others_stay(self):
        self.write_oc({"plugin": [GOAL_SPEC],
                       "command": {"goal": dict(GOAL_COMMAND),
                                   "mine": {"template": "hello"}}})

        self.run_script(CLEANUP)

        after = self.read_json(".config", "opencode", "opencode.json")
        self.assertEqual(after["command"], {"mine": {"template": "hello"}})

    def test_an_edited_goal_command_survives(self):
        mine = dict(GOAL_COMMAND, template="$ARGUMENTS — and do it my way")
        self.write_oc({"plugin": [GOAL_SPEC], "command": {"goal": mine}})

        self.run_script(CLEANUP)

        after = self.read_json(".config", "opencode", "opencode.json")
        self.assertEqual(after["command"]["goal"], mine)
        # ...while the plugin entry beside it still went.
        self.assertNotIn("plugin", after)

    def test_the_emptied_plugin_and_command_keys_are_dropped_entirely(self):
        """An empty list/dict left behind is ferry's litter, not a user setting."""
        self.write_oc({"plugin": [GOAL_SPEC], "command": {"goal": dict(GOAL_COMMAND)},
                       "theme": "opencode"})

        out = self.run_script(CLEANUP).stdout

        after = self.read_json(".config", "opencode", "opencode.json")
        self.assertEqual(after, {"theme": "opencode"})
        self.assertIn("Removed the now-empty 'plugin' list", out)
        self.assertIn("Removed the now-empty 'command' block", out)

    # --- tui.json -----------------------------------------------------------
    def test_tui_json_is_deleted_when_it_held_nothing_but_our_entry(self):
        self.write_oc({"$schema": "https://opencode.ai/tui.json", "plugin": [GOAL_SPEC]},
                      name="tui.json")

        out = self.run_script(CLEANUP).stdout

        self.assertFalse(os.path.exists(self.path(".config", "opencode", "tui.json")))
        self.assertIn("it held nothing but ferry's plugin entry", out)
        # Deleted, not snapshotted: a snapshot would be the same trace by
        # another name.
        self.assertEqual(self.oc_files(), [])

    def test_tui_json_is_rewritten_with_a_snapshot_when_it_holds_more(self):
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": [GOAL_SPEC, LOCAL_FORK],
                       "theme": {"name": "mine"}}, name="tui.json")

        self.run_script(CLEANUP)

        after = self.read_json(".config", "opencode", "tui.json")
        self.assertEqual(after["plugin"], [LOCAL_FORK])
        self.assertEqual(after["theme"], {"name": "mine"})
        snaps = [f for f in self.oc_files()
                 if f.startswith("tui.") and f.endswith(".jsonc")]
        self.assertEqual(len(snaps), 1, self.oc_files())

    def test_a_tui_json_that_is_none_of_ours_is_left_byte_identical(self):
        self.write_oc({"theme": {"name": "mine"}, "plugin": ["someone-elses-plugin"]},
                      name="tui.json")
        before = self.raw(".config", "opencode", "tui.json")

        self.run_script(CLEANUP)

        self.assertEqual(before, self.raw(".config", "opencode", "tui.json"))
        self.assertEqual(self.oc_files(), ["tui.json"])

    def test_a_missing_tui_json_is_reported_not_an_error(self):
        self.write_oc({"model": "someprovider/m"})
        out = self.run_script(CLEANUP).stdout
        self.assertIn("tui.json ...", out)
        self.assertIn("Not present — skipping.", out)

    # --- ferry's own copy of the package (section 4c) -----------------------
    def test_the_managed_copy_and_its_tui_entry_both_go(self):
        """The v1.30.3 shape end to end: a file:// entry and the dir it names."""
        d = self.install_managed()
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": [self.managed_entry()]}, name="tui.json")
        self.assertTrue(os.path.isdir(d), "premise: the copy was planted")

        out = self.run_script(CLEANUP).stdout

        self.assertFalse(os.path.exists(d), out)
        self.assertIn("(ferry's TUI copy of the goal plugin)", out)
        self.assertFalse(os.path.exists(self.path(".local", "share", "ferry")),
                         "the emptied parent should have gone with it")
        self.assertFalse(os.path.exists(self.path(".config", "opencode", "tui.json")),
                         out)
        # The control: ~/.local/share belongs to the user, not to ferry.
        self.assertTrue(os.path.isdir(self.path(".local", "share")),
                        "cleanup climbed one directory too far")

    def test_a_sibling_under_the_ferry_data_dir_keeps_the_parent(self):
        """rmdir only ever removes a parent this script just emptied."""
        d = self.install_managed()
        sibling = self.path(".local", "share", "ferry", "other")
        with open(sibling, "w") as f:
            f.write("not ours\n")

        self.run_script(CLEANUP)

        self.assertFalse(os.path.exists(d))
        self.assertTrue(os.path.exists(sibling),
                        "a non-empty ferry data dir was removed")

    def test_a_managed_dir_without_the_marker_is_left_alone(self):
        """The path is a convention; the MARKER is the proof of ownership."""
        d = self.install_managed(marker=False)
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": [self.managed_entry()]}, name="tui.json")

        out = self.run_script(CLEANUP).stdout

        self.assertIn("carries no ferry marker — left alone.", out)
        self.assertTrue(os.path.isfile(os.path.join(d, "package.json")),
                        "an unmarked directory was deleted anyway")
        # ...while the entry still goes: tui.json is ferry's file either way.
        self.assertFalse(os.path.exists(self.path(".config", "opencode", "tui.json")),
                         out)

    def test_the_managed_entry_goes_and_the_rest_of_tui_json_survives(self):
        self.install_managed()
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": [self.managed_entry()],
                       "theme": {"name": "mine"}}, name="tui.json")

        self.run_script(CLEANUP)

        after = self.read_json(".config", "opencode", "tui.json")
        self.assertNotIn("plugin", after)
        self.assertEqual(after["theme"], {"name": "mine"})
        snaps = [f for f in self.oc_files()
                 if f.startswith("tui.") and f.endswith(".jsonc")]
        self.assertEqual(len(snaps), 1, self.oc_files())

    def test_a_users_paths_survive_in_both_files_but_the_managed_one_does_not(self):
        """Exactly ONE local path is ferry's. Every other one is a fork."""
        fork_url = "file:///Users/someone/src/my-fork"
        fork_abs = "/opt/forks/opencode-goal-plugin"
        entries = [self.managed_entry(), fork_url, fork_abs]
        self.install_managed()
        self.write_oc({"plugin": list(entries), "theme": "opencode"})
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": list(entries)}, name="tui.json")

        self.run_script(CLEANUP)

        oc = self.read_json(".config", "opencode", "opencode.json")
        tui = self.read_json(".config", "opencode", "tui.json")
        self.assertEqual(oc["plugin"], [fork_url, fork_abs])
        self.assertEqual(tui["plugin"], [fork_url, fork_abs])
        self.assertEqual(oc["theme"], "opencode")

    def test_the_tuple_form_of_the_managed_entry_is_removed(self):
        d = self.install_managed()
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": [[self.managed_entry(), {"enabled": True}]]},
                      name="tui.json")

        out = self.run_script(CLEANUP).stdout

        self.assertIn("Removed goal plugin entry", out)
        self.assertFalse(os.path.exists(self.path(".config", "opencode", "tui.json")),
                         out)
        self.assertFalse(os.path.exists(d), out)

    def test_dry_run_reports_the_managed_copy_and_changes_nothing(self):
        d = self.install_managed()
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": [self.managed_entry()]}, name="tui.json")
        before = self.raw(".config", "opencode", "tui.json")

        out = self.run_script(CLEANUP, "--dry-run").stdout

        self.assertIn("would remove the goal plugin entry from", out)
        self.assertIn(f"[dry-run] rm -rf {d}", out)
        self.assertTrue(os.path.isfile(os.path.join(d, GOAL_TUI_MARKER)), out)
        self.assertTrue(os.path.isfile(os.path.join(d, "package.json")), out)
        self.assertEqual(before, self.raw(".config", "opencode", "tui.json"))
        self.assertEqual(self.oc_files(), ["tui.json"])

    def test_a_second_run_finds_no_managed_copy_left(self):
        """Section 4c has to be as re-runnable as the two file sections."""
        self.install_managed()
        self.write_oc({"theme": "opencode", "plugin": [GOAL_SPEC]})
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": [self.managed_entry()]}, name="tui.json")
        first = self.run_script(CLEANUP).stdout
        self.assertIn("(ferry's TUI copy of the goal plugin)", first)

        out = self.run_script(CLEANUP).stdout

        self.assertIn("Not present — skipping.",
                      self.section(out, "ferry's own copy of the goal plugin"))
        self.assertIn("Nothing ferry-shaped found — file left unchanged.",
                      self.section(out, "opencode.json ..."))
        self.assertIn("Not present — skipping.", self.section(out, "tui.json ..."))

    def test_xdg_data_home_relocates_both_the_entry_and_the_directory(self):
        """The copy is INSTALLED against XDG_DATA_HOME, so cleanup must read it."""
        relocated = self.path("relocated-data")
        d = self.install_managed(data_home=relocated)
        decoy = self.install_managed()      # the default path, marker and all
        self.write_oc({"$schema": "https://opencode.ai/tui.json",
                       "plugin": [self.managed_entry(data_home=relocated)]},
                      name="tui.json")
        e = self.env()
        e["XDG_DATA_HOME"] = relocated

        out = self.run_script(CLEANUP, env=e).stdout

        self.assertFalse(os.path.exists(d), out)
        self.assertFalse(os.path.exists(self.path(".config", "opencode", "tui.json")),
                         out)
        # The control: with XDG_DATA_HOME pointing elsewhere, the default path is
        # not ferry's copy on this machine — removing it would be matching by
        # name instead of by location.
        self.assertTrue(os.path.isdir(decoy), "the un-relocated path was deleted")

    # --- dry run and idempotence -------------------------------------------
    def test_dry_run_reports_both_files_and_changes_neither(self):
        self.write_oc({"plugin": [GOAL_SPEC], "command": {"goal": dict(GOAL_COMMAND)}})
        self.write_oc({"$schema": "https://opencode.ai/tui.json", "plugin": [GOAL_SPEC]},
                      name="tui.json")
        before = (self.raw(".config", "opencode", "opencode.json"),
                  self.raw(".config", "opencode", "tui.json"))

        out = self.run_script(CLEANUP, "--dry-run").stdout

        self.assertIn("would remove the goal plugin entry from", out)
        self.assertIn("would remove the /goal command from", out)
        self.assertIn("would delete", out)
        self.assertEqual(before, (self.raw(".config", "opencode", "opencode.json"),
                                  self.raw(".config", "opencode", "tui.json")))
        self.assertEqual(self.oc_files(), ["opencode.json", "tui.json"])

    def test_a_second_cleanup_is_a_no_op(self):
        """Cleanup gets run twice all the time — the second must not re-snapshot."""
        self.run_script(BOOTSTRAP)
        self.run_script(CLEANUP)
        before = self.raw(".config", "opencode", "opencode.json")
        listing = self.oc_files()

        out = self.run_script(CLEANUP).stdout

        self.assertEqual(before, self.raw(".config", "opencode", "opencode.json"))
        self.assertEqual(listing, self.oc_files())
        self.assertIn("Nothing ferry-shaped found — file left unchanged.", out)


class ScriptContractTest(unittest.TestCase):
    """Cheap static checks for the two ways these scripts drift apart."""

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_both_scripts_accept_the_same_scope_flags(self):
        boot, reset = self.read(BOOTSTRAP), self.read(RESET)
        for flag in ("--no-opencode", "--profiles-only", "--full-opencode"):
            self.assertIn(flag + ")", boot, f"{flag} missing from client-bootstrap.sh")
            self.assertIn(flag + ")", reset, f"{flag} missing from client-reset.sh")

    def test_the_placeholders_survive_the_new_header(self):
        """`ferry share` injects by literal match; a reflow must not eat them."""
        for path in (BOOTSTRAP, RESET):
            text = self.read(path)
            self.assertIn("HOST_MDNS_PLACEHOLDER", text, path)
            self.assertIn("SHARE_PORT_PLACEHOLDER", text, path)

    def test_cleanup_knows_every_goal_spelling_ferry_can_write(self):
        """client-cleanup.sh re-implements is_goal_spec()/GOAL_COMMAND (it is
        piped into zsh, so it cannot import lib/). Both copies must list the
        same spellings, or a cleanup silently leaves one behind."""
        integrate = self.read(os.path.join(REPO, "lib", "ferry-integrate.zsh"))
        cleanup = self.read(CLEANUP)
        for spelling in ("opencode-goal-pro-max-complete-plugin",
                         "sblattj/opencode-goal-pro-max-complete-plugin",
                         "opencode-goal-plugin",
                         "@prevalentware/opencode-goal-plugin",
                         "willytop8/opencode-goal-plugin",
                         "github:willytop8/opencode-goal-plugin",
                         "sblattj/opencode-goal-plugin"):
            self.assertIn(f'"{spelling}"', integrate, spelling)
            self.assertIn(f'"{spelling}"', cleanup, spelling)
        for part in ("Set a session-scoped goal and auto-continue until complete.",
                     '"template": "$ARGUMENTS"',
                     '"agent": "build"'):
            self.assertIn(part, integrate, part)
            self.assertIn(part, cleanup, part)
        # v1.30.3: the same duplication, one layer down. ferry writes tui.json a
        # file:// URL to a copy of the package that it owns, and cleanup is the
        # only thing that removes that copy — so the two halves have to agree on
        # WHERE it lives and on the marker file that proves it is ours. Disagree,
        # and cleanup either orphans the copy or deletes a stranger's directory.
        for literal in ('".ferry-goal-plugin"', '"ferry", "opencode-goal-plugin"'):
            self.assertIn(literal, integrate, literal)
            self.assertIn(literal, cleanup, literal)

    def test_both_client_scripts_name_the_goal_plugin_skill(self):
        """The bootstrap installs it and the cleanup removes it, under both
        spellings opencode accepts. A rename in one that misses the other leaves
        the file loading on every session forever."""
        boot, cleanup = self.read(BOOTSTRAP), self.read(CLEANUP)
        self.assertIn("using-the-goal-plugin", boot)
        self.assertIn("skills/using-the-goal-plugin/SKILL.md", boot)
        for spelling in ("skills/using-the-goal-plugin/SKILL.md",
                         "skill/using-the-goal-plugin/SKILL.md"):
            self.assertIn(spelling, cleanup, spelling)

    def test_reset_threads_a_stored_master_key_through_to_the_cli(self):
        """v1.22.0: a reset re-applies the key the bootstrap stored — as --key,
        and only when the profile has one."""
        reset = self.read(RESET)
        self.assertIn("master_key", reset)
        self.assertIn('key_args=(--key "$saved_key")', reset)
        # Every ferry re-apply call rides the array — opencode's per-config
        # loop, the claude wrappers, the cline pre-seed (v1.40.0), and the
        # codex + prime steps (v1.41.0); empty when no key was stored.
        self.assertEqual(reset.count('"${key_args[@]}"'), 5)
        self.assertIn("c.get('api_key') or c.get('master_key'", reset)

    def test_client_to_host_never_promotes_a_device_key_to_master(self):
        """A device key authenticates one laptop to one host; client-to-host.sh
        must carry only a real master_key into the new host's secrets."""
        line = next(l for l in self.read(os.path.join(REPO, "client-to-host.sh")).splitlines()
                    if l.startswith('CLIENT_KEY="$(python3'))
        self.assertIn("get('master_key')", line)
        self.assertNotIn("api_key", line)


class ClientNameTest(ClientHarness):
    """v1.26.0 - client-bootstrap.sh writes this machine's own short hostname
    into client.json as "name", consumed by lib/ferry-core.zsh as CLIENT_NAME
    (Task 8) and baked into every generated config's fleet-identity header
    (Tasks 10/11), per docs/superpowers/specs/2026-09-04-fleets-design.md §6.
    """

    def expected_name(self):
        p = subprocess.run(["hostname", "-s"], capture_output=True, text=True)
        return p.stdout.strip().lower()

    def test_client_json_gains_the_lower_cased_short_hostname(self):
        self.run_script(BOOTSTRAP, "--no-opencode")
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["name"], self.expected_name())

    def test_the_profile_stays_valid_json_without_a_key(self):
        self.run_script(BOOTSTRAP, "--no-opencode")
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertNotIn("master_key", prof)
        self.assertEqual(prof["name"], self.expected_name())

    def test_the_profile_stays_valid_json_with_a_key(self):
        self.run_script(BOOTSTRAP, "--no-opencode",
                        env=self.env(master_key="sk-test-name-and-key"))
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["master_key"], "sk-test-name-and-key")
        self.assertEqual(prof["name"], self.expected_name())


class ClineTestCase(ClientHarness):
    """v1.40.0 — the Cline (VS Code) pre-seed rides the same bootstrap.

    client.json records a cline_mode exactly like opencode_mode/claude_mode:
    the default is "full" (seat-01: the target Macs have no VS Code yet, so
    pre-seeding a machine before first launch IS the supported flow — there is
    no CLI detection to fail), and --no-cline records "none" and must leave
    ~/.cline entirely alone. The full run's observable is the file the
    extension would otherwise have nothing pointing at: ~/.cline/data/
    globalState.json with act mode on the "openai" (OpenAI Compatible)
    provider and the base URL at the stub host.
    """

    def test_full_bootstrap_seeds_the_cline_provider(self):
        self.run_script(BOOTSTRAP)

        self.assertEqual(
            self.read_json(".config", "ferry", "client.json").get("cline_mode"),
            "full")
        gs_path = self.path(".cline", "data", "globalState.json")
        if not os.path.exists(gs_path):
            # The bootstrap's cline step drives the ferry monolith it just
            # DOWNLOADED from the host, so before the checkout's ./build.zsh
            # regenerates it with the cline module, the step warn-fails with
            # "Unknown command: cline" and nothing is pre-seeded.
            self.fail("~/.cline/data/globalState.json was not written — the "
                      "bootstrap's `ferry cline` step failed (expected until "
                      "the post-wave ./build.zsh puts cline in the monolith)")
        with open(gs_path) as f:
            gs = json.load(f)
        self.assertEqual(gs["actModeApiProvider"], "openai")
        self.assertEqual(gs["openAiBaseUrl"],
                         f"http://127.0.0.1:{self.port}/v1")

    def test_no_cline_records_none_and_never_creates_the_cline_dir(self):
        self.run_script(BOOTSTRAP, "--no-cline")

        self.assertEqual(
            self.read_json(".config", "ferry", "client.json").get("cline_mode"),
            "none")
        # The whole tree stays untouched — a user's chat history and
        # checkpoints live under ~/.cline, so "no cline scope" means the
        # directory is not even created.
        self.assertFalse(os.path.exists(self.path(".cline")),
                         "--no-cline created ~/.cline anyway")


CX_START = "# >>> ferry codex profiles >>>"
CX_END = "# <<< ferry codex profiles <<<"
USER_MODELS = """{
  // my own providers — keep this comment
  "providers": {
    "ollama": {
      "baseUrl": "http://localhost:11434",
      "apiKey": "none",
      "api": "openai-completions",
      "models": [{ "id": "llama3" }],
    },
  },
}
"""


class CodexPrimeTestCase(ClientHarness):
    """v1.41.0 — `ferry codex` and `ferry prime` are first-class clients.

    Same shape as the Cline tests above: client.json records codex_mode /
    prime_mode (default "full": no CLI detection, both pre-seed before the
    tool exists), --no-codex / --no-prime record "none" and touch nothing, a
    reset re-applies exactly the recorded scope (absent key reads as none),
    and cleanup unwires ONLY ferry's own wiring — ~/.codex and ~/.prime are
    the user's and never deleted.
    """

    def models_path(self):
        return self.path(".prime", "agent", "models.json")

    def prime_baks(self):
        d = self.path(".prime", "agent")
        return sorted(f for f in os.listdir(d) if f.endswith(".ferry.bak")) \
            if os.path.isdir(d) else []

    def write_user_models(self):
        os.makedirs(self.path(".prime", "agent"))
        with open(self.models_path(), "w") as f:
            f.write(USER_MODELS)

    # --- bootstrap ------------------------------------------------------------
    def test_full_bootstrap_wires_codex_and_prime(self):
        out = self.run_script(BOOTSTRAP).stdout
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof.get("codex_mode"), "full")
        self.assertEqual(prof.get("prime_mode"), "full")

        rc = self.zshrc()
        self.assertIn(CX_START, rc)
        self.assertIn("codex-ferry-flash()", rc)
        self.assertIn(f"http://127.0.0.1:{self.port}/v1", rc)
        cx = self.read_json(".config", "ferry", "codex.json")
        self.assertEqual(cx["wire_api"], "responses")
        self.assertEqual(cx["lanes"]["codex-ferry"], "heavy")

        models = self.read_json(".prime", "agent", "models.json")
        fp = models["providers"]["ferry"]
        self.assertEqual(fp["baseUrl"], f"http://127.0.0.1:{self.port}/v1")
        self.assertEqual(fp["api"], "openai-completions")
        self.assertIn("heavy", [m["id"] for m in fp["models"]])
        self.assertEqual(self.read_json(".config", "ferry", "prime.json")["provider"], "ferry")

        # install hints for the absent tools, never the installers run
        self.assertIn("npm i -g @openai/codex", out)
        self.assertIn("https://app.primeintellect.ai/prime-agent/install.sh", out)
        self.assertFalse(os.path.exists(self.path(".codex")),
                         "ferry codex wrote into ~/.codex")

    def test_no_codex_and_no_prime_record_none_and_touch_nothing(self):
        self.run_script(BOOTSTRAP, "--no-codex", "--no-prime")
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof.get("codex_mode"), "none")
        self.assertEqual(prof.get("prime_mode"), "none")
        self.assertNotIn(CX_START, self.zshrc())
        self.assertFalse(os.path.exists(self.path(".config", "ferry", "codex.json")))
        self.assertFalse(os.path.exists(self.path(".config", "ferry", "prime.json")))
        self.assertFalse(os.path.exists(self.path(".prime")),
                         "--no-prime created ~/.prime anyway")
        self.assertFalse(os.path.exists(self.path(".codex")))

    def test_the_two_switches_are_independent(self):
        self.run_script(BOOTSTRAP, "--no-prime")
        self.assertIn(CX_START, self.zshrc())
        self.assertFalse(os.path.exists(self.path(".prime")))
        self.assertEqual(self.read_json(".config", "ferry", "client.json")["codex_mode"], "full")

    # --- reset ----------------------------------------------------------------
    def test_reset_re_applies_codex_and_prime(self):
        self.run_script(BOOTSTRAP)
        # break both wirings, then let the catch-up repair them
        with open(self.path(".zshrc"), "w") as f:
            f.write("# nothing\n")
        os.remove(self.models_path())
        out = self.run_script(RESET).stdout
        self.assertIn("codex scope:    FULL", out)
        self.assertIn("prime scope:    FULL", out)
        self.assertIn(CX_START, self.zshrc())
        self.assertIn("ferry", self.read_json(".prime", "agent", "models.json")["providers"])

    def test_reset_skips_codex_and_prime_when_the_keys_are_absent(self):
        """A pre-v1.41 profile never opted in; a reset must not widen it."""
        self.run_script(BOOTSTRAP, "--no-codex", "--no-prime")
        prof_path = self.path(".config", "ferry", "client.json")
        prof = self.read_json(".config", "ferry", "client.json")
        del prof["codex_mode"], prof["prime_mode"]
        with open(prof_path, "w") as f:
            json.dump(prof, f)
        out = self.run_script(RESET).stdout
        self.assertIn("codex scope:    NONE", out)
        self.assertIn("prime scope:    NONE", out)
        self.assertNotIn(CX_START, self.zshrc())
        self.assertFalse(os.path.exists(self.path(".prime")))
        self.assertEqual(self.read_json(".config", "ferry", "client.json"), prof,
                         "reset rewrote client.json")

    def test_a_failing_codex_or_prime_step_is_a_warning_not_fatal(self):
        self.run_script(BOOTSTRAP)
        # An unparseable models.json makes `ferry prime` refuse (exit 1).
        with open(self.models_path(), "w") as f:
            f.write("this is not json")
        p = self.run_script(RESET)
        self.assertIn("WARNING: 'ferry prime' failed", p.stdout)
        with open(self.models_path()) as f:
            self.assertEqual(f.read(), "this is not json")

    # --- cleanup --------------------------------------------------------------
    def test_cleanup_strips_the_codex_block_and_leaves_dot_codex(self):
        os.makedirs(self.path(".codex"))
        with open(self.path(".codex", "config.toml"), "w") as f:
            f.write('model = "gpt-5"\n')
        with open(self.path(".zshrc"), "w") as f:
            f.write("export KEEP_ME=1\n")
        self.run_script(BOOTSTRAP)
        self.assertIn(CX_START, self.zshrc())

        self.run_script(CLEANUP)

        rc = self.zshrc()
        self.assertNotIn(CX_START, rc)
        self.assertNotIn("codex-ferry", rc)
        self.assertIn("KEEP_ME", rc)
        self.assertFalse(os.path.exists(self.path(".config", "ferry", "codex.json")))
        with open(self.path(".codex", "config.toml")) as f:
            self.assertEqual(f.read(), 'model = "gpt-5"\n')

    def test_cleanup_restores_the_pre_ferry_models_json_from_the_snapshot(self):
        self.write_user_models()
        self.run_script(BOOTSTRAP)
        self.assertTrue(self.prime_baks(), "ferry prime left no snapshot to restore")
        with open(self.models_path()) as f:
            self.assertIn('"ferry"', f.read())

        self.run_script(CLEANUP)

        with open(self.models_path()) as f:
            self.assertEqual(f.read(), USER_MODELS)
        self.assertEqual(self.prime_baks(), [])
        self.assertFalse(os.path.exists(self.path(".config", "ferry", "prime.json")))

    def test_cleanup_strips_only_the_ferry_provider_when_no_snapshot_exists(self):
        self.write_user_models()
        self.run_script(BOOTSTRAP)
        for b in self.prime_baks():
            os.remove(self.path(".prime", "agent", b))
        # the user keeps editing after ferry wired it: a comment + provider
        with open(self.models_path()) as f:
            text = f.read()
        with open(self.models_path(), "w") as f:
            f.write(text + "// trailing user comment\n")

        out = self.run_script(CLEANUP).stdout

        self.assertIn("Removed 'providers.ferry'", out)
        with open(self.models_path()) as f:
            after = f.read()
        self.assertIn("my own providers", after)
        self.assertIn("trailing user comment", after)
        self.assertNotIn('"ferry"', after)
        self.assertIn("ollama", after)
        self.assertTrue(os.path.isdir(self.path(".prime")))

    def test_cleanup_leaves_a_ferry_provider_it_cannot_prove_is_its_own(self):
        self.run_script(BOOTSTRAP)
        for b in self.prime_baks():
            os.remove(self.path(".prime", "agent", b))
        m = self.read_json(".prime", "agent", "models.json")
        m["providers"]["ferry"]["baseUrl"] = "http://elsewhere.example:9/v1"
        with open(self.models_path(), "w") as f:
            json.dump(m, f)
        self.run_script(CLEANUP)
        self.assertIn("ferry", self.read_json(".prime", "agent", "models.json")["providers"])

    def test_cleanup_dry_run_changes_neither_codex_nor_prime(self):
        self.write_user_models()
        self.run_script(BOOTSTRAP)
        rc_before = self.zshrc()
        with open(self.models_path()) as f:
            m_before = f.read()
        out = self.run_script(CLEANUP, "--dry-run").stdout
        self.assertEqual(self.zshrc(), rc_before)
        with open(self.models_path()) as f:
            self.assertEqual(f.read(), m_before)
        self.assertIn("[dry-run]", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
