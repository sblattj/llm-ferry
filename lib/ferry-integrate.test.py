#!/usr/bin/env python3
"""Stdlib unittest for `ferry opencode` — the opencode config takeover.

Run:  python3 lib/ferry-integrate.test.py

`ferry opencode` is now the ONLY thing that writes an opencode config; the
client bootstrap used to json.dump the two ferry profiles from scratch on every
run, which silently ate any agent / permission / mcp block the user had added.
The takeover it performs is deliberately narrow, and each half of that narrowness
is a bug someone already hit:

  * FOUR keys are ferry's and get replaced outright — permission, model,
    small_model, agent. `agent` is replaced WHOLESALE rather than merged,
    because a stale per-agent pin is exactly the drift this command exists to
    end: a client carried a `compaction` pin naming a retired vendor model for
    months after that lane was re-pointed, resolving only through a
    `hidden: true` back-compat alias that /v1/models does not advertise.
  * EVERY other key is left alone. Nuking a user's mcp/lsp/theme block is the
    regression this file exists to catch.
  * `provider.ferry.models` declares exactly the LANE PAIR, never the served
    catalogue. The host advertises the router-only fallback deployments too;
    those are reached on overflow, not by a client picking one out of a menu,
    and a real model id must never reach a client config.
  * A snapshot of the previous file is written first, so the takeover is
    reversible — and it is written as .jsonc because opencode's schema sets
    allowComments/allowTrailingCommas, so a hand-maintained config legitimately
    carries comments that the json.dump rewrite cannot round-trip.

TestPhantomScoutPin guards a different class of bug: ferry pinned an `agent.scout`
that opencode has never had. It cost nothing and did nothing, which is why it
survived so long — an unknown key just lands in `agent`'s additionalProperties.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

# A representative host catalogue: the four role lanes plus the router-only
# fallback deployments that sit behind them. Only the roles may reach a config.
# `super-flash` is deliberately ABSENT — it is a hidden alias, so a real host
# does not advertise it either, and the housekeeper must wire up anyway.
CATALOGUE = ["heavy", "orch-fallback-1", "orch-fallback-2", "orch-fallback-3",
             "flash", "flash-fallback-1", "flash-fallback-2", "flash-fallback-3",
             "local-orch", "local-sub"]

BUILTIN_AGENTS = {"build", "plan", "general", "explore",
                  "title", "summary", "compaction"}

# Three roles, not two. The housekeeping agents fire on their own schedule and a
# compaction call carries the whole transcript, so they get their own lane rather
# than queueing behind a fan-out on the worker.
DRIVER_AGENTS = ("build", "plan")
WORKER_AGENTS = ("general", "explore")
HOUSE_AGENTS = ("title", "summary", "compaction")

SNAP_RE = re.compile(r"^[^/]+\.\d{8}T\d{6}Z(-\d+)?\.jsonc$")


class _ModelsHandler(BaseHTTPRequestHandler):
    # The Authorization header of the most recent request, for the auth tests.
    # Reset to None before every observed call so a stale value cannot pass.
    LAST_AUTH = None

    def do_GET(self):
        type(self).LAST_AUTH = self.headers.get("Authorization")
        if self.path != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({"data": [{"id": m} for m in CATALOGUE]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):        # keep the test output clean
        pass


class FerryOpencodeCase(unittest.TestCase):
    """Base: a fake host catalogue plus a scratch dir, and a runner."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-oc-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = os.path.join(self.dir, "opencode.json")

    def run_ferry(self, *extra, config=None, port=None, home=None):
        cfg = config or self.cfg
        cmd = ["zsh", FERRY, "opencode", "--host", "127.0.0.1",
               "--port", str(port if port is not None else self.port),
               "--config", cfg, *extra]
        # env -u OPENCODE_CONFIG equivalent: the command honours it as the
        # default target, and an inherited one would silently redirect the write.
        env = {k: v for k, v in os.environ.items() if k != "OPENCODE_CONFIG"}
        if home is not None:
            env["HOME"] = home
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              cwd=REPO, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         f"ferry opencode failed:\n{proc.stdout}\n{proc.stderr}")
        return proc.stdout

    def read(self, config=None):
        with open(config or self.cfg) as f:
            return json.load(f)

    def snapshots(self):
        return sorted(f for f in os.listdir(self.dir) if SNAP_RE.match(f))


class TestTakeoverScope(FerryOpencodeCase):
    """Exactly four keys are ferry's; everything else is the user's."""

    USER_CONFIG = {
        "$schema": "https://opencode.ai/config.json",
        "theme": "tokyonight",
        "username": "someone",
        "permission": {"bash": "ask", "edit": "ask"},
        "model": "anthropic/claude-opus-4",
        "small_model": "ferry/retired-vendor-model",
        "mcp": {"context7": {"type": "local", "command": ["npx", "ctx7"]}},
        "lsp": {"typescript": {"disabled": False}},
        "agent": {
            "build": {"model": "ferry/orch"},
            "compaction": {"model": "ferry/retired-vendor-model"},
            "scout": {"model": "ferry/flash"},
            "my-custom-agent": {"model": "ferry/flash", "prompt": "be terse"},
        },
    }

    def setUp(self):
        super().setUp()
        with open(self.cfg, "w") as f:
            json.dump(self.USER_CONFIG, f)

    def test_unrelated_keys_survive_untouched(self):
        self.run_ferry()
        cfg = self.read()
        for key in ("theme", "username", "mcp", "lsp"):
            self.assertEqual(cfg[key], self.USER_CONFIG[key],
                             f"takeover clobbered the user's {key!r}")

    def test_extra_lanes_stay_in_the_picker(self):
        # A host config declares the GPU pair so it is selectable without a hand
        # edit. Rebuilding provider.ferry.models wholesale deleted them, and the
        # deletion was invisible — the command still reported success, and the
        # lanes still resolved if you typed one, they were just gone from the UI.
        with open(self.cfg, "w") as f:
            json.dump({**self.USER_CONFIG, "provider": {"ferry": {"models": {
                "orch": {}, "local-orch": {}, "local-sub": {},
            }}}}, f)
        out = self.run_ferry()
        models = self.read()["provider"]["ferry"]["models"]
        self.assertEqual(set(models),
                         {"heavy", "flash", "super-flash", "orch",
                          "local-orch", "local-sub"})
        self.assertIn("Kept in picker", out)

    def test_a_hand_written_lane_label_is_preserved(self):
        # The label is the human's, on a lane ferry pins and on one it does not.
        # /v1/models carries only the lane id, so ferry has no better label to
        # offer and must never overwrite one.
        with open(self.cfg, "w") as f:
            json.dump({**self.USER_CONFIG, "provider": {"ferry": {"models": {
                "heavy": {"name": "heavy - driver"},
                "local-sub": {"name": "local-sub - this GPU"},
            }}}}, f)
        self.run_ferry()
        models = self.read()["provider"]["ferry"]["models"]
        self.assertEqual(models["heavy"]["name"], "heavy - driver")
        self.assertEqual(models["local-sub"]["name"], "local-sub - this GPU")

    def test_the_provider_name_tracks_the_host(self):
        # DERIVED from --host, so unlike a lane label it is regenerated: one
        # carried over would label the picker with a box the baseURL no longer
        # points at.
        with open(self.cfg, "w") as f:
            json.dump({**self.USER_CONFIG,
                       "provider": {"ferry": {"name": "Ferry (some-other-box)"}}}, f)
        self.run_ferry()
        self.assertEqual(self.read()["provider"]["ferry"]["name"], "Ferry (127.0.0.1)")

    def test_permission_becomes_the_bare_allow_enum(self):
        # opencode's schema: PermissionConfig is anyOf[PermissionActionConfig,
        # {read,edit,bash,...}], and PermissionActionConfig is the bare enum.
        self.run_ferry()
        self.assertEqual(self.read()["permission"], "allow")

    def test_model_and_small_model_are_lane_names(self):
        self.run_ferry()
        cfg = self.read()
        self.assertEqual(cfg["model"], "ferry/heavy")
        # small_model follows the HOUSEKEEPER. opencode's schema calls it the
        # model "for tasks like title generation" — the housekeeping role, not
        # the fan-out one.
        self.assertEqual(cfg["small_model"], "ferry/super-flash")

    def test_agent_section_is_replaced_wholesale(self):
        self.run_ferry()
        agent = self.read()["agent"]
        self.assertEqual(set(agent), BUILTIN_AGENTS)
        self.assertNotIn("my-custom-agent", agent, "agent must be replaced, not merged")
        self.assertNotIn("scout", agent, "scout is not an opencode agent")

    def test_stale_retired_lane_pin_is_gone(self):
        # The whole point: `compaction -> ferry/retired-vendor-model` resolved only
        # through a hidden back-compat alias. After a takeover, no key anywhere
        # in the file may name anything but the two lanes.
        self.run_ferry()
        blob = json.dumps(self.read())
        self.assertNotIn("retired-vendor-model", blob)

    def test_three_way_role_split(self):
        self.run_ferry()
        agent = self.read()["agent"]
        for a in DRIVER_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/heavy")
        for a in WORKER_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/flash")
        for a in HOUSE_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/super-flash")

    def test_no_default_leaves_the_takeover_keys_alone(self):
        self.run_ferry("--no-default")
        cfg = self.read()
        self.assertEqual(cfg["permission"], self.USER_CONFIG["permission"])
        self.assertEqual(cfg["model"], self.USER_CONFIG["model"])
        self.assertIn("my-custom-agent", cfg["agent"])
        self.assertIn("ferry", cfg["provider"])   # provider is still wired


class TestLaneNamesOnly(FerryOpencodeCase):
    """The served catalogue validates the pair; it never populates the config."""

    def test_only_the_three_roles_are_declared(self):
        self.run_ferry()
        models = self.read()["provider"]["ferry"]["models"]
        self.assertEqual(set(models), {"heavy", "flash", "super-flash"})

    def test_hidden_housekeeper_does_not_warn(self):
        # A hidden model_group_alias resolves on a request but is deliberately
        # absent from /v1/models (see CATALOGUE above, which omits super-flash
        # exactly as the live host does). Validating it against the catalogue
        # would warn on every correct setup.
        out = self.run_ferry()
        self.assertNotIn("does not serve", out)

    def test_fallback_deployments_never_reach_the_config(self):
        self.run_ferry()
        blob = json.dumps(self.read())
        for lane in ("flash-fallback-1", "flash-fallback-2", "flash-fallback-3",
                     "orch-fallback-1", "orch-fallback-2", "orch-fallback-3"):
            self.assertNotIn(lane, blob, f"{lane} is router-only, not client-selectable")

    def test_local_pair_carries_the_kv_limits(self):
        # The local lanes cap KV at 131072, so opencode's 32k output reservation
        # has to be pulled down to 8k or a ~100k prompt returns a clean 400.
        self.run_ferry("--local")
        cfg = self.read()
        models = cfg["provider"]["ferry"]["models"]
        # No third GPU lane exists, so the housekeeper shares the worker — and
        # the models map must not end up with a duplicate entry for it.
        self.assertEqual(set(models), {"local-orch", "local-sub"})
        for lane in models.values():
            self.assertEqual(lane["limit"], {"context": 131072, "output": 8192})
        self.assertEqual(cfg["model"], "ferry/local-orch")
        self.assertEqual(cfg["small_model"], "ferry/local-sub")
        for a in HOUSE_AGENTS:
            self.assertEqual(cfg["agent"][a]["model"], "ferry/local-sub")

    def test_unserved_lane_warns_instead_of_silently_wiring(self):
        out = self.run_ferry("--model", "no-such-lane")
        self.assertIn("does not serve", out)
        self.assertIn("no-such-lane", out)


class TestMasterKeyAuth(FerryOpencodeCase):
    """v1.22.0 — the front door can sit behind a litellm master_key.

    client.json gains an optional `master_key`; when it is set (or --key is
    passed) the generated provider carries it as the bearer AND the catalogue
    check sends it, because an authed door 401s a bare /v1/models and that
    would read as "host down" and wire the lanes unchecked. When absent, the
    legacy 'local' token is written and no Authorization header is sent, so a
    keyless LAN setup is byte-for-byte unchanged.
    """

    PROFILE_KEY = "sk-test-ferry-master"

    def client_home(self, master_key=None):
        home = tempfile.mkdtemp(prefix="ferry-oc-home-")
        self.addCleanup(shutil.rmtree, home, True)
        fdir = os.path.join(home, ".config", "ferry")
        os.makedirs(fdir)
        prof = {"host": "127.0.0.1", "port": str(self.port)}
        if master_key is not None:
            prof["master_key"] = master_key
        with open(os.path.join(fdir, "client.json"), "w") as f:
            json.dump(prof, f)
        return home

    def setUp(self):
        super().setUp()
        _ModelsHandler.LAST_AUTH = None

    def test_profile_master_key_reaches_the_generated_config(self):
        home = self.client_home(self.PROFILE_KEY)
        self.run_ferry(home=home)
        opts = self.read()["provider"]["ferry"]["options"]
        self.assertEqual(opts["apiKey"], self.PROFILE_KEY)

    def test_without_a_key_the_legacy_local_token_is_kept(self):
        home = self.client_home()
        self.run_ferry(home=home)
        self.assertEqual(self.read()["provider"]["ferry"]["options"]["apiKey"], "local")

    def test_the_key_flag_overrides_the_profile(self):
        home = self.client_home(self.PROFILE_KEY)
        self.run_ferry("--key", "sk-flag-key", home=home)
        self.assertEqual(self.read()["provider"]["ferry"]["options"]["apiKey"],
                         "sk-flag-key")

    def test_the_catalogue_check_authenticates_with_the_profile_key(self):
        home = self.client_home(self.PROFILE_KEY)
        self.run_ferry(home=home)
        self.assertEqual(_ModelsHandler.LAST_AUTH, f"Bearer {self.PROFILE_KEY}",
                         "an authed door 401s a bare catalogue check")

    def test_the_catalogue_check_prefers_the_flag_key(self):
        home = self.client_home(self.PROFILE_KEY)
        self.run_ferry("--key", "sk-flag-key", home=home)
        self.assertEqual(_ModelsHandler.LAST_AUTH, "Bearer sk-flag-key")

    def test_no_key_sends_no_authorization_header(self):
        home = self.client_home()
        self.run_ferry(home=home)
        self.assertIsNone(_ModelsHandler.LAST_AUTH,
                          "a keyless LAN setup must request the catalogue bare")

    def test_the_key_is_never_printed(self):
        home = self.client_home(self.PROFILE_KEY)
        out = self.run_ferry(home=home)
        self.assertNotIn(self.PROFILE_KEY, out)


class TestSuperProfile(FerryOpencodeCase):
    """`--super` — the cheap cloud profile: heavy drives, super-flash everywhere.

    The worker AND housekeeper roles collapse onto super-flash (general/explore/
    title/summary/compaction and small_model), while build/plan and the model
    stay on heavy. A later explicit flag must still win over the profile, and
    the hidden-lane catalogue exemption must extend to super-flash in its new
    worker role.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A bound-then-closed port: connection refused, deterministically, which
        # is the "wires unchecked" path — the config must land anyway.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.dead_port = s.getsockname()[1]
        s.close()

    def test_super_wires_the_full_pin_set_with_the_host_unreachable(self):
        out = self.run_ferry("--super", port=self.dead_port)
        self.assertIn("Could not query", out,
                      "the host was supposed to be unreachable")
        cfg = self.read()
        self.assertEqual(cfg["model"], "ferry/heavy")
        self.assertEqual(cfg["small_model"], "ferry/super-flash")
        agent = cfg["agent"]
        for a in DRIVER_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/heavy")
        for a in WORKER_AGENTS + HOUSE_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/super-flash")

    def test_a_later_explicit_small_model_flag_wins_over_super(self):
        self.run_ferry("--super", "--small-model", "flash")
        agent = self.read()["agent"]
        for a in WORKER_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/flash")
        for a in HOUSE_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/super-flash")
        cfg = self.read()
        self.assertEqual(cfg["model"], "ferry/heavy")
        # small_model follows the housekeeper, which --super pinned and the
        # later flag did not touch.
        self.assertEqual(cfg["small_model"], "ferry/super-flash")

    def test_super_gets_the_same_hidden_housekeeper_exemption(self):
        # The stub CATALOGUE omits super-flash exactly as the live host does
        # (see the CATALOGUE note). Under --super it is the WORKER lane too, so
        # the check must exempt it there as well or every correct --super setup
        # warns.
        out = self.run_ferry("--super")
        self.assertNotIn("does not serve", out)


class TestGoalPlugin(FerryOpencodeCase):
    PLUGIN = "@prevalentware/opencode-goal-plugin"

    def test_appended_when_absent(self):
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_user_plugins_are_preserved_and_ordered_first(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": ["some-other-plugin"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], ["some-other-plugin", self.PLUGIN])

    def test_not_duplicated_on_a_second_run(self):
        self.run_ferry()
        self.run_ferry()
        self.assertEqual(self.read()["plugin"].count(self.PLUGIN), 1)

    def test_a_version_pinned_entry_counts_as_present(self):
        # The leading @ is a scope, not a version separator — a naive split
        # would read "@prevalentware/...@0.1.30" as a different package and
        # append a second, conflicting copy.
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [f"{self.PLUGIN}@0.1.30"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [f"{self.PLUGIN}@0.1.30"])

    def test_the_pkg_plus_options_tuple_form_counts_as_present(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [[self.PLUGIN, {"enabled": True}]]}, f)
        self.run_ferry()
        self.assertEqual(len(self.read()["plugin"]), 1)

    # ── a LOCAL fork of the same plugin ───────────────────────────────────
    LOCAL_FORK = "/Users/someone/code/opencode-goal-plugin/dist/server.js"

    def test_a_local_path_to_the_same_plugin_counts_as_present(self):
        """opencode also accepts a filesystem path, and Bun cannot resolve a
        PRIVATE repo over `github:` - so a hard fork of this plugin can only be
        named by path. Matching the npm name alone re-appended upstream on every
        run, and opencode then loaded the fork AND the broken package the fork
        exists to replace. Found live in this host's own config, 2026-08-30."""
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [self.LOCAL_FORK]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.LOCAL_FORK])

    def test_a_near_miss_path_still_gets_the_plugin(self):
        """Control. If a path that merely RESEMBLES the plugin also counted as
        present, the assertion above would pass by matching everything, and
        ferry would quietly stop installing its own plugin."""
        near = "/Users/someone/code/opencode-goal-plugin-extras/dist/server.js"
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [near]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [near, self.PLUGIN])

    def test_a_single_file_named_for_the_plugin_counts_as_present(self):
        entry = "/Users/someone/plugins/opencode-goal-plugin.js"
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [entry]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [entry])

    def test_a_local_fork_in_the_tuple_form_counts_as_present(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [[self.LOCAL_FORK, {"enabled": True}]]}, f)
        self.run_ferry()
        self.assertEqual(len(self.read()["plugin"]), 1)

    def test_the_status_line_reports_what_actually_satisfies_the_check(self):
        """It printed GOAL_PLUGIN unconditionally, so a run that appended nothing
        still read as 'installed @prevalentware/...' - the operator's only
        on-screen evidence, and it disagreed with the file."""
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [self.LOCAL_FORK]}, f)
        out = self.run_ferry()
        self.assertIn(self.LOCAL_FORK, out)
        self.assertIn("upstream not added", out)

    def test_the_status_line_names_the_package_when_it_does_install_it(self):
        """Control for the pair above: on a config with no plugin at all the
        line must still name what ferry added, or the assertion above could pass
        by the line never mentioning the package under any circumstances."""
        out = self.run_ferry()
        self.assertIn(self.PLUGIN, out)
        self.assertNotIn("upstream not added", out)


class TestSnapshots(FerryOpencodeCase):
    JSONC = (
        "{\n"
        "  // a comment json.dump can never round-trip\n"
        '  "theme": "tokyonight",\n'
        '  "permission": { "bash": "ask" },\n'   # trailing comma next line
        "}\n"
    )

    def test_no_snapshot_when_there_was_no_config(self):
        self.run_ferry()
        self.assertEqual(self.snapshots(), [])

    def test_original_is_preserved_verbatim_including_comments(self):
        with open(self.cfg, "w") as f:
            f.write(self.JSONC)
        self.run_ferry()
        snaps = self.snapshots()
        self.assertEqual(len(snaps), 1)
        with open(os.path.join(self.dir, snaps[0])) as f:
            self.assertEqual(f.read(), self.JSONC)
        # ...and the JSONC still parsed, so the takeover actually applied
        self.assertEqual(self.read()["permission"], "allow")
        self.assertEqual(self.read()["theme"], "tokyonight")

    def test_snapshot_is_named_for_its_target(self):
        # A literal `opencode.<ts>.jsonc` for every target would collide across
        # the cloud/local/default profiles that share ~/.config/ferry.
        other = os.path.join(self.dir, "opencode-local.json")
        with open(other, "w") as f:
            json.dump({}, f)
        self.run_ferry("--local", config=other)
        self.assertTrue(all(s.startswith("opencode-local.") for s in self.snapshots()),
                        f"snapshot not named for its target: {self.snapshots()}")

    def test_same_second_reruns_do_not_collide(self):
        with open(self.cfg, "w") as f:
            json.dump({}, f)
        for _ in range(3):
            self.run_ferry()
        self.assertEqual(len(self.snapshots()), 3)

    def test_retention_prunes_the_oldest(self):
        with open(self.cfg, "w") as f:
            json.dump({}, f)
        for _ in range(5):
            self.run_ferry("--keep", "2")
        self.assertEqual(len(self.snapshots()), 2)

    def test_a_users_own_jsonc_file_is_not_pruned(self):
        # Retention matches the timestamp SHAPE, not a "opencode.*.jsonc" glob,
        # which would delete a user's notes file sitting in the same directory.
        bystander = os.path.join(self.dir, "opencode.notes.jsonc")
        with open(bystander, "w") as f:
            f.write("// mine\n{}\n")
        with open(self.cfg, "w") as f:
            json.dump({}, f)
        for _ in range(4):
            self.run_ferry("--keep", "1")
        self.assertTrue(os.path.exists(bystander))


class TestPhantomScoutPin(unittest.TestCase):
    """`scout` is not an opencode agent and must not come back.

    opencode 1.18.23's schema names exactly plan/build/general/explore/title/
    summary/compaction under `agent`, and the shipped binary contains no
    "scout" string at all. Ferry pinned `agent.scout` in two places; the pin
    landed in additionalProperties and was never read.
    """

    def _sources(self):
        for path in (FERRY,
                     os.path.join(REPO, "lib", "ferry-integrate.zsh"),
                     os.path.join(REPO, "client-bootstrap.sh")):
            with open(path) as f:
                yield path, f.read()

    def test_nothing_pins_a_scout_agent(self):
        for path, text in self._sources():
            self.assertNotIn('"scout"', text,
                             f"{os.path.basename(path)} pins a phantom scout agent")

    def test_generated_ferry_is_in_sync_with_lib(self):
        # `ferry` is a build artifact; an edit to lib/ that was never built is
        # invisible to every client, which fetches the single file.
        proc = subprocess.run(["zsh", os.path.join(REPO, "build.zsh"), "--check"],
                              capture_output=True, text=True, cwd=REPO, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_bootstrap_no_longer_writes_configs_itself(self):
        # One writer. If the bootstrap grows its own json.dump of a profile
        # again, it will silently eat user keys on every re-run.
        with open(os.path.join(REPO, "client-bootstrap.sh")) as f:
            text = f.read()
        self.assertNotIn("opencode-cloud.json\": {", text)
        self.assertIn("ferry\" opencode", text)


if __name__ == "__main__":
    if not os.path.exists(FERRY):
        sys.exit("built ./ferry not found — run ./build.zsh first")
    unittest.main(verbosity=2)
