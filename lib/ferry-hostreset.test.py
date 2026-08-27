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


def run_python(source, argv, home=None):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(source)
        path = f.name
    try:
        env = dict(os.environ)
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

    def verify(self, port):
        return run_python(self.src, [port, self.cfg, 9992, 9993], home=self.home)

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

    # --- a throwaway /v1/models endpoint -------------------------------------
    def serve(self, lanes):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        body = json.dumps({"data": [{"id": m} for m in lanes]}).encode()

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
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

    def test_neutralises_an_inherited_opencode_config(self):
        self.assertIn("env -u OPENCODE_CONFIG", self.text)

    def test_passes_the_host_explicitly(self):
        # client-reset.sh learned this the hard way: never let the config's
        # baseURL be decided by an inference about which machine this is.
        self.assertIn('--host 127.0.0.1 --port "$PORT"', self.text)

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
