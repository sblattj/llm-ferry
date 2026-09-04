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
import json
import os
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

    def test_reset_threads_a_stored_master_key_through_to_the_cli(self):
        """v1.22.0: a reset re-applies the key the bootstrap stored — as --key,
        and only when the profile has one."""
        reset = self.read(RESET)
        self.assertIn("master_key", reset)
        self.assertIn('key_args=(--key "$saved_key")', reset)
        # Both ferry calls ride the array; empty when no key was stored.
        self.assertEqual(reset.count('"${key_args[@]}"'), 2)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
