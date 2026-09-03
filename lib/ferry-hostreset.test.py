#!/usr/bin/env python3
"""Stdlib unittest for host-reset.sh.

Run:  python3 lib/ferry-hostreset.test.py

host-reset.sh is the HOST counterpart of client-reset.sh, and its dangerous step
is not the one client-reset guards. A client validates a DOWNLOAD before it
overwrites a working CLI; the host has nothing to download, so its equivalent
risk is restarting the proxy against a litellm.yaml that parses but is wrong.
litellm does not check its config beyond parsing it: a duplicate key, a dangling
alias, or an unset env var all start cleanly and then fail at request time, on
one lane, looking exactly like a provider outage.

These tests run the REAL embedded validators — extracted out of host-reset.sh,
not reimplemented — so an edit to the script that breaks a check fails here.

The two heredocs are extracted positionally: the first is the pre-restart route
config validator, the second is the post-restart endpoint verifier.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "host-reset.sh")

# pyyaml lives in the USER site-packages on macOS, so a test that overrides HOME
# also hides it from the child interpreter — and the validator's import-guard
# then silently skips the very checks under test. Pin it back explicitly.
try:
    import yaml
    YAML_PATH = os.path.dirname(os.path.dirname(yaml.__file__))
except ImportError:  # pragma: no cover
    YAML_PATH = None


_UNSET = object()


def read_script():
    with open(SCRIPT) as f:
        return f.read()


def embedded_python(index):
    """Pull the Nth `python3 - ... <<'PYEOF' ... PYEOF` block out of host-reset.sh."""
    # Both heredocs carry a trailing `|| die ...` / `|| RESET_FAILED=1` on the
    # opening line, so the pattern has to tolerate anything up to the newline.
    blocks = re.findall(r"<<'PYEOF'[^\n]*\n(.*?)\nPYEOF\n", read_script(), re.S)
    if len(blocks) <= index:
        raise AssertionError(
            f"expected at least {index + 1} PYEOF blocks in host-reset.sh, found {len(blocks)}")
    return blocks[index]


def script_function(name):
    """Extract a top-level zsh function (header line through its closing brace)."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}$", read_script(), re.S | re.M)
    if not m:
        raise AssertionError(f"function {name}() not found in host-reset.sh")
    return m.group(0)


def run_python(source, argv, home=None, opencode_config=_UNSET, master_key=_UNSET):
    """Run an extracted block. `opencode_config` is EXPLICIT on purpose.

    The verifier reads $OPENCODE_CONFIG to find the host's live config, so a
    test that merely inherits the ambient environment silently checks the
    author's real config instead of its fixture -- and then passes or fails for
    reasons that have nothing to do with the code under test. (It really did:
    these cases were green while reading a live dotfiles config.) Default is
    UNSET; a case that wants the variable must say so.

    LITELLM_MASTER_KEY follows the same discipline (v1.22.0): the verifier now
    resolves it from the ambient environment FIRST, so an author shell that
    happens to export one would silently authenticate probes meant to test the
    keyless path. Popped unless a case asks for it.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(source)
        path = f.name
    try:
        env = dict(os.environ)
        env.pop("OPENCODE_CONFIG", None)
        env.pop("LITELLM_MASTER_KEY", None)
        if opencode_config is not _UNSET and opencode_config is not None:
            env["OPENCODE_CONFIG"] = opencode_config
        if master_key is not _UNSET and master_key is not None:
            env["LITELLM_MASTER_KEY"] = master_key
        if home:
            env["HOME"] = home
            if YAML_PATH:
                env["PYTHONPATH"] = YAML_PATH
        return subprocess.run([sys.executable, path, *map(str, argv)],
                              capture_output=True, text=True, env=env, timeout=60)
    finally:
        os.unlink(path)


GOOD_YAML = """
model_list:
  - model_name: orch
    litellm_params: {model: "prov/a", api_key: "os.environ/TEST_KEY"}
  - model_name: flash
    litellm_params: {model: "prov/b", api_key: "os.environ/TEST_KEY"}
  - model_name: flash-gem
    litellm_params: {model: "prov/c", api_key: "os.environ/TEST_KEY"}
router_settings:
  model_group_alias:
    super-flash: {model: "flash-gem", hidden: true}
  fallbacks:
    - flash: ["flash-gem"]
"""


class ValidatorCase(unittest.TestCase):
    """The pre-restart check on ~/.config/ferry/litellm.yaml."""

    @classmethod
    def setUpClass(cls):
        cls.src = embedded_python(0)
        cls.dir = tempfile.mkdtemp(prefix="ferry-hostreset-")
        cls.secrets = os.path.join(cls.dir, "secrets.env")
        with open(cls.secrets, "w") as f:
            f.write("export TEST_KEY=value\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def validate(self, yaml_text, secrets=None):
        p = os.path.join(self.dir, "cfg.yaml")
        with open(p, "w") as f:
            f.write(yaml_text)
        return run_python(self.src, [p, secrets or self.secrets])

    def test_accepts_a_good_config(self):
        r = self.validate(GOOD_YAML)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_reads_aliases_from_router_settings(self):
        # THE regression guard. litellm reads model_group_alias from
        # router_settings; a validator that looks only at the top level reports a
        # confident "0 aliases" on a config full of them, and the verify step then
        # calls every hidden lane missing. Caught by dry-run before shipping.
        r = self.validate(GOOD_YAML)
        self.assertIn("1 aliases", r.stdout,
                      "aliases under router_settings were not counted")

    def test_rejects_duplicate_keys(self):
        # PyYAML and litellm both keep the LAST of a duplicated key, silently.
        # That is how a `flash` deployment came to carry the model_info id of its
        # own fallback: two model_info blocks, one config, no error anywhere, and
        # a fallback chain that pointed a lane back at itself.
        r = self.validate("""
model_list:
  - model_name: flash
    litellm_params: {model: "prov/b", api_key: "os.environ/TEST_KEY"}
    model_info: {id: "flash-1"}
    model_info: {id: "flash-fallback-1"}
""")
        self.assertEqual(r.returncode, 1)
        self.assertIn("duplicate key", r.stderr)

    def test_rejects_a_dangling_alias(self):
        r = self.validate("""
model_list:
  - model_name: flash
    litellm_params: {model: "prov/b", api_key: "os.environ/TEST_KEY"}
router_settings:
  model_group_alias:
    super-flash: {model: "flash-typo", hidden: true}
""")
        self.assertEqual(r.returncode, 1)
        self.assertIn("flash-typo", r.stderr)

    def test_rejects_a_fallback_to_a_nonexistent_lane(self):
        r = self.validate("""
model_list:
  - model_name: orch
    litellm_params: {model: "prov/a", api_key: "os.environ/TEST_KEY"}
router_settings:
  fallbacks:
    - orch: ["orch-backup"]
""")
        self.assertEqual(r.returncode, 1)
        self.assertIn("orch-backup", r.stderr)

    def test_rejects_an_unset_env_var(self):
        r = self.validate("""
model_list:
  - model_name: flash
    litellm_params: {model: "prov/b", api_key: "os.environ/DEFINITELY_NOT_SET_XYZ"}
""")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DEFINITELY_NOT_SET_XYZ", r.stderr)

    def test_rejects_an_empty_model_list(self):
        r = self.validate("model_list: []\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("model_list", r.stderr)

    def test_a_missing_secrets_file_is_a_note_not_a_failure(self):
        # Keys may legitimately come from the shell instead.
        env_backup = os.environ.get("TEST_KEY")
        os.environ["TEST_KEY"] = "from-the-shell"
        try:
            r = self.validate(GOOD_YAML, secrets=os.path.join(self.dir, "nope.env"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("note:", r.stdout)
        finally:
            if env_backup is None:
                del os.environ["TEST_KEY"]
            else:
                os.environ["TEST_KEY"] = env_backup

    def test_the_secrets_reader_never_executes_the_file(self):
        # secrets.env is parsed as KEY=VALUE, never sourced. A validator that
        # shelled out to read it would hand arbitrary code the reset's privileges.
        self.assertNotIn("subprocess", self.src)
        self.assertNotIn("os.system", self.src)


class KeySeedCase(unittest.TestCase):
    """_seed_master_key: generate the front-door key only when it is wanted,
    missing, and not already defined — and never leak its value."""

    @classmethod
    def setUpClass(cls):
        cls.fn = script_function("_seed_master_key")

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-keyseed-")
        self.ferry_dir = os.path.join(self.home, ".config", "ferry")
        os.makedirs(self.ferry_dir)
        self.cfg = os.path.join(self.ferry_dir, "litellm.yaml")
        self.secrets = os.path.join(self.ferry_dir, "secrets.env")
        self.addCleanup(shutil.rmtree, self.home, True)

    def run_seed(self, config_text=None, shell_key=None, secrets_text=None, pre_mode=None):
        if config_text is not None:
            with open(self.cfg, "w") as f:
                f.write(config_text)
        if secrets_text is not None:
            with open(self.secrets, "w") as f:
                f.write(secrets_text)
            if pre_mode is not None:
                os.chmod(self.secrets, pre_mode)
        # The extracted function needs the ok/die helpers it calls; stubs with
        # the same contract keep the harness honest about what it depends on.
        driver = (
            'ok() { echo "OK: $*"; }\n'
            'warn() { echo "WARN: $*"; }\n'
            'die() { echo "DIE: $*" >&2; exit 1; }\n'
            f'{self.fn}\n'
            f'ROUTE_CONFIG="{self.cfg}"\n'
            f'SECRETS="{self.secrets}"\n'
            'set -u\n'
            '_seed_master_key\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".zsh", delete=False) as f:
            f.write(driver)
            path = f.name
        env = dict(os.environ)
        env.pop("LITELLM_MASTER_KEY", None)   # hermetic: author shells may export one
        if shell_key is not None:
            env["LITELLM_MASTER_KEY"] = shell_key
        try:
            return subprocess.run(["zsh", path], capture_output=True, text=True,
                                  env=env, timeout=60)
        finally:
            os.unlink(path)

    def seeded_value(self):
        with open(self.secrets) as f:
            lines = [l for l in f.read().splitlines()
                     if l.startswith("export LITELLM_MASTER_KEY=")]
        return lines[0].split("=", 1)[1] if len(lines) == 1 else None

    KEYED_CONFIG = ("general_settings:\n"
                    "  master_key: os.environ/LITELLM_MASTER_KEY\n"
                    "model_list: []\n")

    def test_generates_and_stores_when_the_config_references_the_key(self):
        r = self.run_seed(config_text=self.KEYED_CONFIG)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.secrets), "secrets.env was not created")
        value = self.seeded_value()
        self.assertIsNotNone(value, "secrets.env did not gain exactly one export line")
        self.assertRegex(value, r"^sk-[A-Za-z0-9_\-]+$")
        self.assertEqual(os.stat(self.secrets).st_mode & 0o777, 0o600)

    def test_the_generated_key_is_never_printed(self):
        # The key exists so the operator can hand it to clients — the script
        # printing it to a reset log would put a front-door credential in
        # scrollback. Assert the VALUE is absent from all output.
        r = self.run_seed(config_text=self.KEYED_CONFIG)
        value = self.seeded_value()
        self.assertTrue(value)
        self.assertNotIn(value, r.stdout + r.stderr)

    def test_seeding_is_idempotent(self):
        self.run_seed(config_text=self.KEYED_CONFIG)
        first = self.seeded_value()
        r = self.run_seed(config_text=self.KEYED_CONFIG)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.seeded_value(), first, "second run generated a second key")
        with open(self.secrets) as f:
            self.assertEqual(
                sum(1 for l in f if l.startswith("export LITELLM_MASTER_KEY=")), 1)
        self.assertEqual(os.stat(self.secrets).st_mode & 0o777, 0o600)

    def test_a_shell_key_wins_and_nothing_is_written(self):
        r = self.run_seed(config_text=self.KEYED_CONFIG, shell_key="sk-from-shell")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.secrets),
                         "seeded secrets.env despite LITELLM_MASTER_KEY being set")

    def test_an_existing_secrets_entry_is_respected(self):
        r = self.run_seed(config_text=self.KEYED_CONFIG,
                          secrets_text="export LITELLM_MASTER_KEY=sk-existing\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.secrets) as f:
            content = f.read()
        self.assertEqual(content, "export LITELLM_MASTER_KEY=sk-existing\n",
                         "existing key line was altered or duplicated")

    def test_a_config_without_the_reference_never_touches_secrets(self):
        # Keyless LAN installs must be untouched — the seeding exists solely to
        # satisfy a config that references the var.
        r = self.run_seed(config_text="model_list: []\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.secrets))

    def test_a_missing_config_never_touches_secrets(self):
        r = self.run_seed(config_text=None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.secrets))

    def test_writing_tightens_loose_perms_to_0600(self):
        # secrets.env carries every provider key; a seeded write must never
        # leave a previously-loose mode in place.
        self.run_seed(config_text=self.KEYED_CONFIG,
                      secrets_text="export GLM_API_KEY=x\n", pre_mode=0o644)
        self.assertEqual(os.stat(self.secrets).st_mode & 0o777, 0o600)


class VerifierCase(unittest.TestCase):
    """The post-restart check that the served lanes match the written configs."""

    @classmethod
    def setUpClass(cls):
        cls.src = embedded_python(1)

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-hostreset-home-")
        os.makedirs(os.path.join(self.home, ".config", "opencode"))
        os.makedirs(os.path.join(self.home, ".config", "ferry"))
        self.cfg = os.path.join(self.home, "litellm.yaml")
        with open(self.cfg, "w") as f:
            f.write(GOOD_YAML)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write_oc(self, name, lanes):
        path = {
            "default": os.path.join(self.home, ".config", "opencode", "opencode.json"),
            "cloud": os.path.join(self.home, ".config", "ferry", "opencode-cloud.json"),
            "local": os.path.join(self.home, ".config", "ferry", "opencode-local.json"),
        }[name]
        driver, worker, house = lanes
        with open(path, "w") as f:
            f.write(json.dumps({
                "model": f"ferry/{driver}",
                "small_model": f"ferry/{house}",
                "agent": {"build": {"model": f"ferry/{driver}"},
                          "general": {"model": f"ferry/{worker}"},
                          "title": {"model": f"ferry/{house}"}},
            }))

    def verify(self, port, opencode_config=_UNSET, master_key=_UNSET, secrets=None):
        # 5th argv: the secrets.env path, which the verifier reads (never
        # sources) to find LITELLM_MASTER_KEY when the shell has none.
        if secrets is None:
            secrets = os.path.join(self.home, ".config", "ferry", "secrets.env")
        return run_python(self.src, [port, self.cfg, 9992, 9993, secrets],
                          home=self.home, opencode_config=opencode_config,
                          master_key=master_key)

    def test_verifier_follows_opencode_config_when_set(self):
        # The host path: the live config lives OUTSIDE ~/.config/opencode, named
        # only by the env var. The verifier must follow it there.
        for n in ("cloud", "local"):
            self.write_oc(n, ("orch", "flash", "super-flash"))
        live = os.path.join(self.home, "elsewhere", "opencode.jsonc")
        os.makedirs(os.path.dirname(live))
        with open(live, "w") as f:
            f.write(json.dumps({"model": "ferry/renamed-away-lane", "agent": {}}))
        r = self.verify(self.serve(["orch", "flash", "flash-gem"]), opencode_config=live)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("renamed-away-lane", r.stderr,
                      "verifier ignored $OPENCODE_CONFIG and checked the stock path")

    def test_a_missing_config_is_a_failure(self):
        self.write_oc("default", ("orch", "flash", "super-flash"))
        # cloud + local deliberately absent
        r = self.verify(self.serve(["orch", "flash"]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING", r.stdout + r.stderr)

    def test_a_hidden_alias_counts_as_resolvable(self):
        # super-flash is `hidden: true`, so it NEVER appears in /v1/models. A
        # verifier that only trusted the listing would report the housekeeper lane
        # broken on every healthy run — and a check that always fails is a check
        # everyone learns to ignore.
        for n in ("default", "cloud", "local"):
            self.write_oc(n, ("orch", "flash", "super-flash"))
        r = self.verify(self.serve(["orch", "flash", "flash-gem"]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("super-flash", r.stdout)

    def test_a_lane_that_is_not_served_fails(self):
        for n in ("default", "cloud", "local"):
            self.write_oc(n, ("orch", "renamed-away-lane", "super-flash"))
        r = self.verify(self.serve(["orch", "flash", "flash-gem"]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("renamed-away-lane", r.stderr)

    def test_reports_a_dead_local_backend_without_failing(self):
        # litellm LISTS local-orch/local-sub whether or not an MLX server is
        # behind them, so "listed" is not "reachable". A proxy-only reset legally
        # leaves them down, so this reports loudly and still exits 0.
        for n in ("default", "cloud", "local"):
            self.write_oc(n, ("orch", "flash", "super-flash"))
        r = self.verify(self.serve(["orch", "flash", "flash-gem", "local-orch"]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DOWN", r.stdout)
        self.assertIn("--full", r.stdout)

    # --- v1.22.0: master_key gating on the catalogue route --------------------
    def test_verifier_sends_the_bearer_when_the_key_is_available(self):
        # A master_key-gated front door answers 401 to keyless probes. The
        # verifier must present LITELLM_MASTER_KEY and get the real listing.
        for n in ("default", "cloud", "local"):
            self.write_oc(n, ("orch", "flash", "super-flash"))
        r = self.verify(self.serve(["orch", "flash", "flash-gem"], bearer="sk-test"),
                        master_key="sk-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Lanes served: orch flash flash-gem", r.stdout)

    def test_verifier_finds_the_key_in_secrets_env(self):
        # Same, but the key arrives via secrets.env (the dumb KEY=VALUE read),
        # not the shell — the host-reset seeding path writes it there.
        secrets = os.path.join(self.home, ".config", "ferry", "secrets.env")
        with open(secrets, "w") as f:
            f.write("export GEMINI_API_KEY=other\nexport LITELLM_MASTER_KEY=sk-from-file\n")
        for n in ("default", "cloud", "local"):
            self.write_oc(n, ("orch", "flash", "super-flash"))
        r = self.verify(self.serve(["orch", "flash", "flash-gem"], bearer="sk-from-file"),
                        secrets=secrets)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Lanes served: orch flash flash-gem", r.stdout)

    def test_verifier_degrades_gracefully_when_the_key_is_missing(self):
        # Keyed proxy + no key anywhere: the reset must NOT crash on the 401.
        # The listing degrades to a note and named lanes become "unconfirmed"
        # (the same posture as unreadable aliases), because listing-unknown is
        # not the same as lane-missing.
        for n in ("default", "cloud", "local"):
            self.write_oc(n, ("orch", "flash", "super-flash"))
        r = self.verify(self.serve(["orch", "flash", "flash-gem"], bearer="sk-test"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("note:", r.stdout)
        self.assertIn("LITELLM_MASTER_KEY", r.stdout + r.stderr)
        self.assertIn("unconfirmed", r.stdout)
        self.assertNotIn("NOT served", r.stdout + r.stderr)

    def test_verifier_fails_when_the_key_is_rejected(self):
        # A WRONG key is a real problem — the running proxy was started under a
        # different key than this reset would serve. That must fail loudly.
        for n in ("default", "cloud", "local"):
            self.write_oc(n, ("orch", "flash", "super-flash"))
        r = self.verify(self.serve(["orch", "flash", "flash-gem"], bearer="sk-right"),
                        master_key="sk-wrong")
        self.assertEqual(r.returncode, 1)
        self.assertIn("PROBLEM", r.stdout + r.stderr)
        self.assertIn("rejected", r.stdout + r.stderr)

    # --- a throwaway /v1/models endpoint -------------------------------------
    def serve(self, lanes, bearer=None):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        body = json.dumps({"data": [{"id": m} for m in lanes]}).encode()

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if bearer is not None and self.headers.get("Authorization") != f"Bearer {bearer}":
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # shutdown() stops serve_forever but leaves the listener open; without
        # server_close every case leaks an fd and a ResourceWarning.
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]


class ScriptShapeCase(unittest.TestCase):
    """Properties of host-reset.sh itself, independent of the embedded Python."""

    def setUp(self):
        self.text = read_script()

    def test_exists_and_is_executable(self):
        self.assertTrue(os.path.exists(SCRIPT))
        self.assertTrue(os.access(SCRIPT, os.X_OK), "host-reset.sh is not executable")

    def test_zsh_syntax_is_clean(self):
        r = subprocess.run(["zsh", "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_refuses_to_run_on_a_client(self):
        # A client has no checkout to rebuild and no proxy to bounce. Running the
        # host reset there would fail somewhere deep and confusingly; it should
        # bounce at the top, and name the script that IS right for a client.
        home = tempfile.mkdtemp(prefix="ferry-fakeclient-")
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(os.path.join(home, ".config", "ferry"))
        with open(os.path.join(home, ".config", "ferry", "client.json"), "w") as f:
            json.dump({"host": "somewhere.local", "port": 8090}, f)
        env = dict(os.environ, HOME=home)
        r = subprocess.run(["zsh", SCRIPT], capture_output=True, text=True,
                           env=env, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("client-reset.sh", r.stdout)

    def test_client_detection_matches_ferrys_own(self):
        # ferry decides host-vs-client purely on the presence of client.json (see
        # CLIENT_MODE in lib/ferry-core.zsh). A second, disagreeing definition
        # here would let the two drift.
        with open(os.path.join(REPO, "lib", "ferry-core.zsh")) as f:
            core = f.read()
        self.assertIn('CLIENT_CONF="$HOME/.config/ferry/client.json"', core)
        self.assertIn(".config/ferry/client.json", self.text)

    def test_validates_before_it_restarts(self):
        # The whole point of the pre-flight: a bad config must cost a failed
        # reset, not a downed endpoint. If a future edit moves the restart above
        # the validation, the guard silently stops guarding.
        self.assertLess(self.text.index("Validating $ROUTE_CONFIG"),
                        self.text.index('"$FERRY_BIN" up'))

    def test_waits_for_the_endpoint_before_reapplying_the_takeover(self):
        # `ferry up --route` returns the moment it forks; it never polls for
        # readiness the way stack mode does. Re-applying the takeover against a
        # proxy that is not listening yet costs the takeover's OWN validation —
        # cmd_opencode queries /v1/models to check the lane pair it is about to
        # write, and on a refusal it degrades to "wiring the lane pair unchecked"
        # and writes anyway, so a wrong lane name lands in all three configs
        # unremarked. Observed on the first live run of this script.
        self.assertIn("Waiting for the endpoint", self.text)
        self.assertLess(self.text.index("Waiting for the endpoint"),
                        self.text.index("Re-applying the opencode takeover"))

    def test_the_endpoint_wait_probes_the_public_liveliness_route(self):
        # v1.22.0: with general_settings.master_key set, /v1/models answers 401
        # to an unauthenticated probe and the wait loop would report a healthy
        # proxy as never-ready. /health/liveliness stays public, and the wait
        # only asks "is the process up".
        self.assertIn("/health/liveliness", self.text)
        loop = self.text[self.text.index("Waiting for the endpoint"):
                         self.text.index("Re-applying the opencode takeover")]
        while_line = next(l for l in loop.splitlines()
                          if l.strip().startswith("while ! curl"))
        self.assertIn("health/liveliness", while_line)
        self.assertNotIn("/v1/models", while_line,
                         "the readiness wait must not probe the gated catalogue route")

    def test_master_key_is_seeded_before_it_is_validated(self):
        # The validator fails closed on a referenced-but-unset env var, so the
        # seeding step must run first or the first reset after adopting the
        # template dead-ends by design.
        self.assertIn("_seed_master_key() {", self.text)
        validate_idx = self.text.index("Validating $ROUTE_CONFIG")
        self.assertLess(self.text.index("_seed_master_key() {"), validate_idx,
                        "the helper must be defined before the validation section")
        call_idx = self.text.index("_seed_master_key\n", validate_idx)
        self.assertLess(call_idx, self.text.index("PYEOF"),
                        "seeding must run before the validator heredoc")

    def test_the_verifier_never_sources_secrets_and_presents_the_bearer(self):
        verifier = embedded_python(1)
        # Same doctrine as the validator: read secrets.env as KEY=VALUE, never
        # execute it.
        self.assertNotIn("subprocess", verifier)
        self.assertNotIn("os.system", verifier)
        self.assertIn('os.environ.get("LITELLM_MASTER_KEY")', verifier)
        self.assertIn('Authorization", f"Bearer', verifier)
        self.assertIn("secrets_path", verifier)
        # ...and the shell side must hand the secrets path to the verifier.
        self.assertIn('"$LOCAL_SUB_PORT" "$SECRETS" <<\'PYEOF\'', self.text)

    def test_frees_the_share_port_before_restarting_it(self):
        # `ferry share` scans UPWARD for a free port, so starting a second server
        # while the first still holds :8095 lands it on :8096 — serving happily,
        # injecting its own port, and leaving every published URL pointing at the
        # old process.
        self.assertIn("ferry-share-marker", self.text)
        self.assertLess(self.text.index('pkill -f "ferry-share-marker"'),
                        self.text.index('"$FERRY_BIN" share'))

    def test_builds_through_the_interpreter(self):
        # `./build.zsh` exits 126 in a checkout that lost its exec bit, aborting
        # the reset on something that is not actually broken.
        self.assertIn('zsh "$APP_DIR/build.zsh"', self.text)
        self.assertNotIn("./build.zsh >", self.text)

    def test_asserts_the_rebuild_actually_synced(self):
        self.assertIn("build.zsh\" --check", self.text)

    def test_relinks_rather_than_only_creating(self):
        # The failure is a link replaced by a plain COPY: it keeps working, keeps
        # its old behaviour forever, and nothing reports it as stale.
        self.assertIn("ln -sfn", self.text)
        self.assertIn("ferry-dash", self.text)
        self.assertIn("stale COPY", self.text)

    def test_the_default_target_honours_opencode_config(self):
        # THE v1.8.7 BUG. host-reset copied client-reset's `env -u
        # OPENCODE_CONFIG` onto every target. On a client that is right: a fresh
        # machine reads the stock path, and stripping the variable stops a stray
        # export collapsing three writes onto one file. On a HOST that variable
        # is frequently the whole mechanism - exported from a dotfiles repo to a
        # config outside ~/.config/opencode entirely - so stripping it wrote the
        # takeover to a file opencode never reads, printed "Config written", and
        # changed nothing. Found on the author's own host.
        self.assertIn('oc_default="${OPENCODE_CONFIG:-', self.text)
        self.assertRegex(
            self.text,
            r'"\$FERRY_BIN" opencode --host 127\.0\.0\.1 --port "\$PORT" --config "\$oc_default"',
            "the default target must be written WITHOUT env -u")

    def test_the_explicit_profiles_still_strip_it(self):
        # The two ferry profiles are addressed by absolute path on purpose, so an
        # inherited OPENCODE_CONFIG could only redirect them onto each other.
        self.assertIn("env -u OPENCODE_CONFIG", self.text)
        loop = self.text[self.text.index("for oc_target in"):]
        self.assertIn("env -u OPENCODE_CONFIG", loop)

    def test_the_default_target_is_not_written_twice(self):
        # If OPENCODE_CONFIG happens to point AT one of the ferry profiles, the
        # loop would write it a second time and snapshot it twice per run.
        self.assertIn('[[ "$oc_path" == "$oc_default" ]] && continue', self.text)

    def test_the_verifier_checks_the_live_config_not_the_stock_path(self):
        # v1.8.7's verifier read the three paths it had just written, so it went
        # green over an untouched live config. A check that can only confirm its
        # own output is not a check.
        self.assertIn('os.environ.get("OPENCODE_CONFIG") or', self.text)

    def test_passes_the_host_explicitly(self):
        # client-reset.sh learned this the hard way: never let the config's
        # baseURL be decided by an inference about which machine this is.
        self.assertIn('--host 127.0.0.1 --port "$PORT"', self.text)

    def test_installs_the_opencode_guardrails(self):
        # The /fan-out command and spawning-subagents skill shipped ONLY in
        # client-bootstrap.sh, so every client got them and the host never did -
        # while `ferry opencode` deliberately wires the host to its own endpoint,
        # so the host drives local lanes exactly like a client. The documented
        # mitigation for malformed/looping `task` calls had therefore never been
        # installed on the machine reporting the problem.
        self.assertIn("opencode/command/fan-out.md", self.text)
        self.assertIn("spawning-subagents", self.text)
        self.assertLess(self.text.index("fan-out.md"),
                        self.text.index("Re-applying the opencode takeover"),
                        "guardrails must be installed before the takeover reports success")

    def test_the_guardrails_go_to_the_global_opencode_paths(self):
        # command/ and skill/ are GLOBAL locations, independent of
        # $OPENCODE_CONFIG. Deriving them from the config's directory would put
        # them somewhere opencode never looks on exactly the hosts that need it.
        self.assertIn('$HOME/.config/opencode/command', self.text)
        self.assertIn('$HOME/.config/opencode/skill/spawning-subagents', self.text)

    def test_covers_all_three_opencode_targets(self):
        for target in ("opencode/opencode.json",
                       "ferry/opencode-cloud.json",
                       "ferry/opencode-local.json"):
            self.assertIn(target, self.text)
        self.assertIn("--local", self.text)

    def test_default_leaves_the_gpu_lanes_alone(self):
        # The default path must not reach `ferry down` / a bare `ferry up`, both
        # of which reload ~33GB of weights.
        self.assertIn('"$FERRY_BIN" up --route', self.text)
        down_line = self.text.index('"$FERRY_BIN" down')
        full_branch = self.text.index("if (( FULL )); then")
        else_branch = self.text.index("else", full_branch)
        self.assertLess(full_branch, down_line)
        self.assertLess(down_line, else_branch,
                        "`ferry down` must live inside the --full branch")

    def test_documents_the_full_flag(self):
        self.assertIn("--full", self.text)
        self.assertIn("--no-pull", self.text)

    def test_pull_is_fast_forward_only(self):
        # A reset must never rebase or merge the tree you develop in.
        self.assertIn("--ff-only", self.text)
        self.assertNotIn("git reset --hard", self.text)
        self.assertNotIn("git checkout .", self.text)

    def test_untracked_files_do_not_block_the_pull(self):
        # The repo normally carries a few untracked files; they cannot block a
        # fast-forward, so counting them would make the reset unrunnable.
        self.assertIn("git diff --quiet", self.text)
        self.assertNotIn("git status --porcelain", self.text)


if __name__ == "__main__":
    if not os.path.exists(SCRIPT):
        sys.exit("host-reset.sh not found")
    unittest.main(verbosity=2)
