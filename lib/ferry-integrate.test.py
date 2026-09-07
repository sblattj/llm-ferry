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
import urllib.parse
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

# A representative host catalogue: the cloud role lanes plus selectable medium
# and the router-only
# fallback deployments that sit behind them. Only the roles may reach a config.
# `super-flash` is omitted to exercise hosts whose catalogue does not advertise
# every usable lane; title/summary must still wire up.
CATALOGUE = ["heavy", "medium", "orch-fallback-1", "orch-fallback-2", "orch-fallback-3",
             "flash", "flash-fallback-1", "flash-fallback-2", "flash-fallback-3",
             "local-orch", "local-sub"]

BUILTIN_AGENTS = {"build", "plan", "general", "explore",
                  "title", "summary", "compaction"}
# Not opencode built-ins: ferry declares these two itself, as subagents, to
# replace the disabled `general` worker.
CUSTOM_AGENTS = {"light", "standard"}

# Cloud defaults put `standard` on medium when a modern host advertises it,
# `light` and explore on flash, and compaction/housekeeping on super-flash.
# Older/unreachable hosts drop standard back to flash; local keeps its two GPU
# lanes and shares local-sub across every worker.
DRIVER_AGENTS = ("build", "plan")
LIGHT_AGENTS = ("light",)
STANDARD_AGENTS = ("standard",)
DISABLED_AGENTS = ("general",)
EXPLORE_AGENTS = ("explore",)
COMPACTION_AGENTS = ("compaction",)
HOUSE_AGENTS = ("title", "summary")
# The agents that carry a model and are NOT the driver. `general` is absent on
# purpose: it is disabled, so it has no model key at all.
WORKER_AGENTS = LIGHT_AGENTS + STANDARD_AGENTS + EXPLORE_AGENTS
NON_DRIVER_AGENTS = WORKER_AGENTS + COMPACTION_AGENTS + HOUSE_AGENTS

# opencode's task tool advertises each non-primary agent to the driver as
# "- <name>: <description>", so the complexity band has to live in the
# description text. These strings are the routing contract; assert them
# verbatim, because a paraphrase that loses the numeric band silently stops the
# driver from routing.
LIGHT_DESC = ("Worker for tasks rated 0-50 of 100 complexity: exploration "
              "follow-ups, small fixes, easy implementation, mechanical edits "
              "with clear instructions. Full tool access. Default worker; use "
              "standard only when the task clearly needs deeper judgment.")
STANDARD_DESC = ("Worker for tasks rated 51-100 of 100 complexity: multi-file "
                 "implementation, ambiguous debugging, design judgment. Full "
                 "tool access. Use light for anything rated 50 or below.")

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
        # HERMETIC BY DEFAULT. Since v1.30.1 `ferry opencode` reads
        # XDG_CONFIG_HOME (to decide whether the config it is writing IS the
        # global takeover target, and so whether to mirror the entry into
        # tui.json) and XDG_CACHE_HOME (to purge orphaned per-spec package
        # directories). Inheriting the author's real ones would let the suite
        # write into ~/.config/opencode and delete out of ~/.cache/opencode.
        # XDG_DATA_HOME joined them in v1.30.3: it is where ferry keeps the
        # colon-free COPY of the goal plugin that tui.json points at, and an
        # inherited one would both rewrite the author's real copy and decide
        # these cases' tui.json entries from a directory the test never wrote.
        self.xdg_config = os.path.join(self.dir, "xdg-config")
        self.xdg_cache = os.path.join(self.dir, "xdg-cache")
        self.xdg_data = os.path.join(self.dir, "xdg-data")
        os.makedirs(self.xdg_config, exist_ok=True)
        os.makedirs(self.xdg_cache, exist_ok=True)
        os.makedirs(self.xdg_data, exist_ok=True)

    def packages_root(self):
        return os.path.join(self.xdg_cache, "opencode", "packages")

    def run_ferry(self, *extra, config=None, port=None, home=None,
                  install=False, cache=None, xdg_config=None, xdg_data=None,
                  env_extra=None):
        cfg = config or self.cfg
        cmd = ["zsh", FERRY, "opencode", "--host", "127.0.0.1",
               "--port", str(port if port is not None else self.port),
               "--config", cfg, *extra]
        # The pre-install pass shells out to the real `opencode`; every case
        # that is not ABOUT it opts out, or the suite would hit the network.
        if not install and "--no-install" not in extra:
            cmd.append("--no-install")
        # env -u OPENCODE_CONFIG equivalent: the command honours it as the
        # default target, and an inherited one would silently redirect the write.
        skip = {"OPENCODE_CONFIG", "OPENCODE_TUI_CONFIG"}
        env = {k: v for k, v in os.environ.items() if k not in skip}
        env["XDG_CONFIG_HOME"] = xdg_config or self.xdg_config
        env["XDG_CACHE_HOME"] = cache or self.xdg_cache
        env["XDG_DATA_HOME"] = xdg_data or self.xdg_data
        if home is not None:
            env["HOME"] = home
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              cwd=REPO, timeout=180)
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
                         {"heavy", "medium", "flash", "super-flash", "orch",
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
        self.assertEqual(set(agent), BUILTIN_AGENTS | CUSTOM_AGENTS)
        self.assertNotIn("my-custom-agent", agent, "agent must be replaced, not merged")
        self.assertNotIn("scout", agent, "scout is not an opencode agent")

    def test_stale_retired_lane_pin_is_gone(self):
        # The whole point: `compaction -> ferry/retired-vendor-model` resolved only
        # through a hidden back-compat alias. After a takeover, no key anywhere
        # in the file may name anything but the two lanes.
        self.run_ferry()
        blob = json.dumps(self.read())
        self.assertNotIn("retired-vendor-model", blob)

    def test_modern_host_uses_the_requested_agent_lane_defaults(self):
        self.run_ferry()
        agent = self.read()["agent"]
        for a in DRIVER_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/heavy")
        for a in LIGHT_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/flash")
        for a in STANDARD_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/medium")
        for a in EXPLORE_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/flash")
        for a in COMPACTION_AGENTS + HOUSE_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/super-flash")

    def test_the_builtin_general_worker_is_disabled_not_deleted(self):
        # Deleting the key does NOT remove the agent: opencode ships `general`
        # as a built-in, and an unpinned built-in reappears inheriting the
        # primary model — i.e. the expensive driver lane would run the fan-out.
        self.run_ferry()
        agent = self.read()["agent"]
        for a in DISABLED_AGENTS:
            self.assertEqual(agent[a], {"disable": True},
                             "general must be disabled, with no model key")

    def test_the_two_workers_are_subagents_carrying_the_band_descriptions(self):
        # The description is the ONLY thing the driver sees at dispatch time
        # (opencode's task tool has no model parameter), so the band rule has
        # to be in it verbatim.
        self.run_ferry()
        agent = self.read()["agent"]
        self.assertEqual(agent["light"], {"description": LIGHT_DESC,
                                          "mode": "subagent",
                                          "model": "ferry/flash"})
        self.assertEqual(agent["standard"], {"description": STANDARD_DESC,
                                             "mode": "subagent",
                                             "model": "ferry/medium"})

    def test_no_default_leaves_the_takeover_keys_alone(self):
        self.run_ferry("--no-default")
        cfg = self.read()
        self.assertEqual(cfg["permission"], self.USER_CONFIG["permission"])
        self.assertEqual(cfg["model"], self.USER_CONFIG["model"])
        self.assertIn("my-custom-agent", cfg["agent"])
        self.assertIn("ferry", cfg["provider"])   # provider is still wired


class TestLaneNamesOnly(FerryOpencodeCase):
    """The served catalogue validates the pair; it never populates the config."""

    def test_role_lanes_and_selectable_medium_are_declared(self):
        self.run_ferry()
        models = self.read()["provider"]["ferry"]["models"]
        self.assertEqual(set(models), {"heavy", "medium", "flash", "super-flash"})

    def test_old_catalogue_does_not_advertise_medium_unless_explicitly_selected(self):
        old_catalogue = [lane for lane in CATALOGUE if lane != "medium"]
        with mock.patch(__name__ + ".CATALOGUE", old_catalogue):
            self.run_ferry()
            self.assertNotIn("medium", self.read()["provider"]["ferry"]["models"])

            self.run_ferry("--model", "medium")
            self.assertIn("medium", self.read()["provider"]["ferry"]["models"])

    def test_old_catalogue_keeps_standard_fallback_and_compaction_house_lane(self):
        old_catalogue = [lane for lane in CATALOGUE if lane != "medium"]
        with mock.patch(__name__ + ".CATALOGUE", old_catalogue):
            self.run_ferry()
        cfg = self.read()
        self.assertNotIn("medium", cfg["provider"]["ferry"]["models"])
        # No medium on the wire => standard falls back onto flash rather than
        # naming a lane this host cannot resolve.
        self.assertEqual(cfg["agent"]["standard"]["model"], "ferry/flash")
        self.assertEqual(cfg["agent"]["light"]["model"], "ferry/flash")
        self.assertEqual(cfg["agent"]["explore"]["model"], "ferry/flash")
        self.assertEqual(cfg["agent"]["compaction"]["model"], "ferry/super-flash")
        self.assertEqual(cfg["agent"]["general"], {"disable": True})

    def test_catalogue_omission_of_the_title_summary_lane_does_not_warn(self):
        # A compatible host can omit the title/summary lane from /v1/models.
        # It must still generate a valid config without a false warning.
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
            # The mlx servers behind the GPU pair take no image input, so the
            # pair must NOT advertise one: opencode would ship the bytes.
            self.assertNotIn("modalities", lane)
        self.assertEqual(cfg["model"], "ferry/local-orch")
        self.assertEqual(cfg["small_model"], "ferry/local-sub")
        for a in NON_DRIVER_AGENTS:
            self.assertEqual(cfg["agent"][a]["model"], "ferry/local-sub")
        # Two lanes only, so light and standard collapse onto the same one —
        # they stay two AGENTS (the band split is about which one the driver
        # picks), and general is disabled here exactly as it is on cloud.
        self.assertEqual(cfg["agent"]["light"]["mode"], "subagent")
        self.assertEqual(cfg["agent"]["standard"]["mode"], "subagent")
        self.assertEqual(cfg["agent"]["general"], {"disable": True})

    def test_an_explicit_small_model_overrides_every_worker_lane(self):
        # --small-model is one knob for the whole fan-out: the band split says
        # which worker the driver picks, not which lanes stay alive when the
        # operator has named one.
        self.run_ferry("--small-model", "medium")
        cfg = self.read()
        for a in WORKER_AGENTS:
            self.assertEqual(cfg["agent"][a]["model"], "ferry/medium")
        # The driver and the housekeeping lanes are untouched by it.
        self.assertEqual(cfg["model"], "ferry/heavy")
        for a in COMPACTION_AGENTS + HOUSE_AGENTS:
            self.assertEqual(cfg["agent"][a]["model"], "ferry/super-flash")

    def test_unserved_lane_warns_instead_of_silently_wiring(self):
        out = self.run_ferry("--model", "no-such-lane")
        self.assertIn("does not serve", out)
        self.assertIn("no-such-lane", out)

    def test_cloud_role_lanes_declare_image_and_pdf_input(self):
        # opencode gates attachments on `modalities.input`: a custom-provider
        # lane without it has capabilities.input.image == false, and opencode
        # then swaps a pasted screenshot for the text `ERROR: Cannot read
        # "x.png" (this model does not support image input). Inform the user.`
        # before the request ever reaches the front. The cloud role lanes read
        # images and PDFs (probed 2026-09-05), so each must say so. `medium`
        # deliberately has no modality declaration: its international GLM-5.3
        # backend is text-only, and a static client config cannot safely vary
        # that declaration with a fleet selected per request.
        self.run_ferry()
        models = self.read()["provider"]["ferry"]["models"]
        for lane in ("heavy", "flash", "super-flash"):
            self.assertEqual(models[lane]["modalities"],
                             {"input": ["text", "image", "pdf"],
                              "output": ["text"]}, lane)
        self.assertNotIn("modalities", models["medium"])
        self.assertNotIn("limit", models["medium"])

    def test_a_rerun_adds_modalities_to_a_config_written_before_them(self):
        # Every client config generated before this declaration existed has
        # the lane entries without it. The spec is rebuilt per lane on each
        # run (only a hand-written `name` survives), so a plain re-run must
        # upgrade the old shape rather than preserve it.
        with open(self.cfg, "w") as f:
            json.dump({"provider": {"ferry": {"models": {
                "heavy": {"name": "heavy - driver"},
                "flash": {"name": "flash"},
                "super-flash": {"name": "super-flash"},
            }}}}, f)
        self.run_ferry()
        models = self.read()["provider"]["ferry"]["models"]
        self.assertEqual(models["heavy"]["name"], "heavy - driver")
        self.assertIn("image", models["heavy"]["modalities"]["input"])
        self.assertIn("image", models["flash"]["modalities"]["input"])


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

    Every non-driver agent collapses onto super-flash (light/standard/explore/
    title/summary/compaction and small_model), while build/plan and the model
    stay on heavy. A later explicit flag must still win over the profile.
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
        for a in NON_DRIVER_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/super-flash")
        self.assertEqual(agent["general"], {"disable": True})

    def test_unreachable_catalogue_keeps_standard_fallback_and_compaction_house_lane(self):
        out = self.run_ferry(port=self.dead_port)
        self.assertIn("Could not query", out,
                      "the host was supposed to be unreachable")
        cfg = self.read()
        self.assertNotIn("medium", cfg["provider"]["ferry"]["models"])
        self.assertEqual(cfg["agent"]["standard"]["model"], "ferry/flash")
        self.assertEqual(cfg["agent"]["light"]["model"], "ferry/flash")
        self.assertEqual(cfg["agent"]["explore"]["model"], "ferry/flash")
        self.assertEqual(cfg["agent"]["compaction"]["model"], "ferry/super-flash")
        self.assertEqual(cfg["agent"]["general"], {"disable": True})

    def test_a_later_explicit_small_model_flag_wins_over_super(self):
        self.run_ferry("--super", "--small-model", "flash")
        agent = self.read()["agent"]
        for a in WORKER_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/flash")
        for a in COMPACTION_AGENTS + HOUSE_AGENTS:
            self.assertEqual(agent[a]["model"], "ferry/super-flash")
        cfg = self.read()
        self.assertEqual(cfg["model"], "ferry/heavy")
        # small_model follows the housekeeper, which --super pinned and the
        # later flag did not touch.
        self.assertEqual(cfg["small_model"], "ferry/super-flash")

    def test_a_later_explicit_housekeeper_flag_wins_over_super(self):
        self.run_ferry("--super", "--housekeeper", "flash")
        cfg = self.read()
        for a in WORKER_AGENTS:
            self.assertEqual(cfg["agent"][a]["model"], "ferry/super-flash")
        for a in COMPACTION_AGENTS + HOUSE_AGENTS:
            self.assertEqual(cfg["agent"][a]["model"], "ferry/flash")
        self.assertEqual(cfg["small_model"], "ferry/flash")

    def test_super_tolerates_catalogue_omission_of_its_shared_lane(self):
        # The stub catalogue omits super-flash. Under --super it is shared by
        # every non-driver agent, so its omission must not cause a false warning.
        out = self.run_ferry("--super")
        self.assertNotIn("does not serve", out)


class TestGoalPlugin(FerryOpencodeCase):
    # The entry ferry writes is the NAME-PREFIXED TARBALL form, and nothing
    # else. On opencode 1.18.29 a bare `github:`/URL spec has no npm name, so
    # `npa(pkg).name ?? pkg` yields the raw spec and Npm.add throws AFTER a
    # successful reify (packages/core/src/npm.ts:117-134) — the package is on
    # disk and the plugin never loads, with nothing logged. Any GIT spec whose
    # package.json declares build/prepack additionally dies in pacote's prepare
    # step inside the bun binary. The ref is still pinned because the spec
    # string IS opencode's cache key. Bump alongside GOAL_PLUGIN_REF.
    REF = "v0.10.1"
    PKG = "opencode-goal-plugin"
    BASE = "github:sblattj/OpenCode-goal-plugin"
    TARBALL = (f"https://github.com/sblattj/OpenCode-goal-plugin"
               f"/archive/refs/tags/{REF}.tar.gz")
    PLUGIN = f"{PKG}@{TARBALL}"
    GOAL_COMMAND = {
        "description": "Set a session-scoped goal and auto-continue until complete.",
        "template": "$ARGUMENTS",
        "agent": "build",
    }

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

    def test_a_pin_to_the_current_ref_is_left_alone(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [self.PLUGIN]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_an_unpinned_entry_is_rewritten_to_the_pinned_spec(self):
        """opencode keys its package cache on the spec STRING and never
        refreshes a cached git plugin, so an unpinned entry pins a machine to
        whatever commit it first fetched. Rewriting it is the rollout."""
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [self.BASE]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_an_older_pin_is_rewritten_to_the_current_ref(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [f"{self.BASE}#v0.9.0"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_an_unpinned_tuple_entry_is_repinned_and_keeps_its_options(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [[self.BASE, {"enabled": True}]]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [[self.PLUGIN, {"enabled": True}]])

    def test_repinning_is_idempotent_on_a_second_run(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [self.BASE]}, f)
        self.run_ferry()
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_the_pkg_plus_options_tuple_form_counts_as_present(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [[self.PLUGIN, {"enabled": True}]]}, f)
        self.run_ferry()
        self.assertEqual(len(self.read()["plugin"]), 1)

    # ── the plugin's /goal slash command (opencode's top-level `command`) ──
    def test_goal_command_is_added_when_command_is_absent(self):
        self.run_ferry()
        self.assertEqual(self.read()["command"]["goal"], self.GOAL_COMMAND)

    def test_an_unrelated_command_is_preserved(self):
        mine = {"description": "mine", "template": "hi"}
        with open(self.cfg, "w") as f:
            json.dump({"command": {"mine": mine}}, f)
        self.run_ferry()
        cmds = self.read()["command"]
        self.assertEqual(cmds["mine"], mine)
        self.assertEqual(cmds["goal"], self.GOAL_COMMAND)

    def test_a_customised_goal_command_is_left_unchanged(self):
        custom = {"description": "my own goal", "template": "$ARGUMENTS", "agent": "plan"}
        with open(self.cfg, "w") as f:
            json.dump({"command": {"goal": custom}}, f)
        self.run_ferry()
        self.assertEqual(self.read()["command"]["goal"], custom)

    def test_the_goal_command_is_a_no_op_on_a_second_run(self):
        self.run_ferry()
        self.run_ferry()
        self.assertEqual(self.read()["command"], {"goal": self.GOAL_COMMAND})

    def test_legacy_prevalentware_plugin_is_migrated_to_new_plugin(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": ["@prevalentware/opencode-goal-plugin"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_legacy_bare_opencode_goal_plugin_is_migrated(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": ["opencode-goal-plugin"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_legacy_willytop8_plugin_is_migrated(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": ["github:willytop8/OpenCode-goal-plugin"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_legacy_version_pinned_entry_is_migrated(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": ["@prevalentware/opencode-goal-plugin@0.1.30"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_legacy_tuple_entry_is_migrated(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [["@prevalentware/opencode-goal-plugin", {"enabled": True}]]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [[self.PLUGIN, {"enabled": True}]])

    def test_deduplicates_legacy_and_new_entries(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": ["opencode-goal-plugin", self.PLUGIN]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

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
        still read as 'installed opencode-goal-plugin' - the operator's only
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

    # ── v1.30.1: every spelling ferry ever wrote is an UNLOADABLE spec ─────
    # Each of these installed to disk on opencode 1.18.29 and was then silently
    # discarded, so a machine carrying one has never once run the plugin.
    def test_the_v1294_bare_github_pin_is_migrated(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [f"{self.BASE}#v0.9.1"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_a_name_prefixed_git_spec_is_migrated(self):
        """Name-prefixed fixes the npa defect but NOT the pacote prepare defect:
        the plugin has declared build+prepack since v0.9.1, so a git spec still
        dies with `git dep preparation failed`. Only the tarball is immune."""
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [f"{self.PKG}@{self.BASE}#v0.9.0"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_an_older_tarball_url_is_migrated(self):
        old = ("https://github.com/sblattj/OpenCode-goal-plugin"
               "/archive/refs/tags/v0.9.1.tar.gz")
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [old]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_an_older_named_tarball_is_migrated(self):
        old = f"{self.PKG}@https://github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.10.0.tar.gz"
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [old]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_a_git_plus_https_url_is_migrated(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [
                "git+https://github.com/sblattj/OpenCode-goal-plugin.git#v0.9.0"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_a_willytop8_tarball_is_migrated(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [
                "https://github.com/willytop8/opencode-goal-plugin/archive/refs/tags/v0.8.2.tar.gz"]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [self.PLUGIN])

    def test_a_bare_github_tuple_is_migrated_and_keeps_its_options(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [[f"{self.BASE}#v0.9.1", {"enabled": True}]]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [[self.PLUGIN, {"enabled": True}]])

    def test_a_tarball_tuple_is_migrated_and_keeps_its_options(self):
        old = f"{self.PKG}@https://github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.10.0.tar.gz"
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [[old, {"enabled": False}]]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [[self.PLUGIN, {"enabled": False}]])

    def test_every_dead_spelling_at_once_collapses_to_one_entry(self):
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [
                "some-other-plugin",
                f"{self.BASE}#v0.9.1",
                f"{self.PKG}@{self.BASE}#v0.9.0",
                "https://github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.9.1.tar.gz",
                "@prevalentware/opencode-goal-plugin",
            ]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], ["some-other-plugin", self.PLUGIN])

    def test_an_unrelated_plugin_naming_another_owner_is_untouched(self):
        """Control for the repo-substring rule: it must key on OUR repo, not on
        the words `opencode` and `plugin` appearing near each other."""
        other = "github:someoneelse/opencode-tidy-plugin#v1.0.0"
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [other]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [other, self.PLUGIN])

    def test_a_local_path_naming_our_repo_is_still_left_alone(self):
        """A fork checked out under a `sblattj/OpenCode-goal-plugin` directory
        matches the repo substring, and must STILL not be rewritten - a path is
        the only way to name a private fork."""
        fork = "/Users/someone/code/sblattj/OpenCode-goal-plugin/dist/server.js"
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [fork]}, f)
        self.run_ferry()
        self.assertEqual(self.read()["plugin"], [fork])


class TestGoalPluginTuiConfig(FerryOpencodeCase):
    """The TUI half of the plugin is read from tui.json and NOWHERE else.

    opencode.json's `plugin` array feeds the SERVER plugin loader. A module
    loaded with kind:"tui" - the goal plugin's sidebar panel - comes from
    tui.json/tui.jsonc in the global config dir, $OPENCODE_TUI_CONFIG, a project
    tui file or a .opencode dir (packages/opencode/src/config/tui.ts:157-210).
    Listing the spec only in opencode.json loads the server half and, silently,
    nothing else.
    """

    PLUGIN = TestGoalPlugin.PLUGIN
    BASE = TestGoalPlugin.BASE

    def setUp(self):
        super().setUp()
        # The full-takeover target: ${XDG_CONFIG_HOME}/opencode/opencode.json.
        # Deliberately NOT created here: several cases below assert that this
        # directory never comes into existence, and a setUp that made it would
        # turn those into assertions about setUp.
        self.oc_dir = os.path.join(self.xdg_config, "opencode")
        self.global_cfg = os.path.join(self.oc_dir, "opencode.json")
        self.tui = os.path.join(self.oc_dir, "tui.json")

    def read_tui(self, path=None):
        with open(path or self.tui) as f:
            return json.load(f)

    def test_created_beside_the_global_config(self):
        out = self.run_ferry(config=self.global_cfg)
        self.assertEqual(self.read_tui(), {
            "$schema": "https://opencode.ai/tui.json",
            "plugin": [self.PLUGIN],
        })
        # Nothing is installed yet on this machine, so there is no colon-free
        # copy to point at: the spec goes in as the BOOTSTRAP form and the
        # status line says so. See TestGoalPluginTuiCopy for the install path.
        self.assertIn("tui.json: spec, TUI copy pending install", out)

    def test_merged_into_an_existing_tui_config_without_touching_other_keys(self):
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(self.tui, "w") as f:
            json.dump({"theme": "gruvbox", "plugin": ["their-tui-plugin"]}, f)
        self.run_ferry(config=self.global_cfg)
        data = self.read_tui()
        self.assertEqual(data["theme"], "gruvbox")
        self.assertEqual(data["plugin"], ["their-tui-plugin", self.PLUGIN])

    def test_a_dead_spec_in_tui_json_is_migrated_too(self):
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(self.tui, "w") as f:
            json.dump({"plugin": [f"{self.BASE}#v0.9.1"]}, f)
        self.run_ferry(config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [self.PLUGIN])

    def test_the_previous_tui_config_is_snapshotted(self):
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(self.tui, "w") as f:
            f.write('{\n  // a comment json.dump could never round-trip\n  "theme": "gruvbox"\n}\n')
        original = open(self.tui).read()
        self.run_ferry(config=self.global_cfg)
        snaps = [f for f in os.listdir(self.oc_dir)
                 if re.match(r"^tui\.\d{8}T\d{6}Z(-\d+)?\.jsonc$", f)]
        self.assertEqual(len(snaps), 1, os.listdir(self.oc_dir))
        with open(os.path.join(self.oc_dir, snaps[0])) as f:
            self.assertEqual(f.read(), original)

    def test_not_created_when_the_config_lives_elsewhere(self):
        """The ferry lane profiles (~/.config/ferry/opencode-*.json) and the
        --profiles-only / --no-opencode client scopes must never bring
        ~/.config/opencode into existence; that ABSENCE is what they mean."""
        elsewhere = os.path.join(self.dir, "ferry", "opencode-cloud.json")
        os.makedirs(os.path.dirname(elsewhere), exist_ok=True)
        out = self.run_ferry(config=elsewhere)
        self.assertFalse(os.path.exists(os.path.join(self.xdg_config, "opencode")),
                         "writing a profile created the global opencode dir")
        self.assertIn("tui.json: skipped", out)

    def test_no_tui_config_suppresses_it_on_the_global_target(self):
        out = self.run_ferry("--no-tui-config", config=self.global_cfg)
        self.assertFalse(os.path.exists(self.tui))
        self.assertIn("tui.json: skipped", out)

    def test_an_explicit_tui_config_is_honoured_from_anywhere(self):
        elsewhere = os.path.join(self.dir, "ferry", "opencode-cloud.json")
        os.makedirs(os.path.dirname(elsewhere), exist_ok=True)
        target = os.path.join(self.dir, "custom-tui.json")
        self.run_ferry("--tui-config", target, config=elsewhere)
        self.assertEqual(self.read_tui(target)["plugin"], [self.PLUGIN])
        self.assertFalse(os.path.exists(os.path.join(self.xdg_config, "opencode")))

    def test_no_default_never_writes_a_tui_config(self):
        out = self.run_ferry("--no-default", config=self.global_cfg)
        self.assertFalse(os.path.exists(self.tui))
        self.assertNotIn("tui.json", out)


class TestGoalPluginCacheHygiene(FerryOpencodeCase):
    """opencode NEVER invalidates ~/.cache/opencode/packages, so ferry must.

    The per-spec directory is path.join(cache, "packages", <raw spec>) with
    sanitize() a no-op off Windows, so the spec's slashes are PATH SEPARATORS
    (packages/core/src/npm.ts:43-47,79). With a name-prefixed spec the install
    short-circuits on the mere EXISTENCE of <dir>/node_modules/<name>
    (npm.ts:125-127), so an interrupted install is stuck forever.
    """

    PLUGIN = TestGoalPlugin.PLUGIN
    PKG = TestGoalPlugin.PKG
    BASE = TestGoalPlugin.BASE

    # The exact directory opencode 1.18.29 creates for the canonical spec.
    # Node's path.join COLLAPSES the "//" after "https:"; python's os.path.join
    # does not, which is why the implementation runs it through normpath. This
    # literal is the observable: it was read off a real
    # `opencode plugin '<spec>' --global` run.
    CANONICAL_REL = os.path.join(
        "opencode-goal-plugin@https:", "github.com", "sblattj",
        "OpenCode-goal-plugin", "archive", "refs", "tags",
        f"{TestGoalPlugin.REF}.tar.gz")

    def pkgdir(self, *parts):
        return os.path.join(self.packages_root(), *parts)

    def seed(self, rel, populated=False):
        d = self.pkgdir(rel) if isinstance(rel, str) else self.pkgdir(*rel)
        target = os.path.join(d, "node_modules", self.PKG)
        os.makedirs(os.path.join(target, "dist"), exist_ok=True)
        if populated:
            with open(os.path.join(target, "package.json"), "w") as f:
                json.dump({"name": self.PKG, "version": "0.10.1"}, f)
        return d

    def test_the_canonical_cache_path_is_the_node_normalised_one(self):
        """Guards the normpath: a populated canonical dir at THIS exact path is
        recognised as a real install and kept."""
        d = self.seed(self.CANONICAL_REL, populated=True)
        self.run_ferry()
        self.assertTrue(os.path.exists(os.path.join(d, "node_modules", self.PKG,
                                                    "package.json")),
                        "a good install was purged")

    def test_an_interrupted_canonical_install_is_removed(self):
        """Control for the case above, varying exactly one factor: the same
        directory WITHOUT package.json is the interrupted state that opencode's
        existence-only short-circuit treats as installed forever."""
        d = self.seed(self.CANONICAL_REL, populated=False)
        out = self.run_ferry()
        self.assertFalse(os.path.exists(d), "the empty install was kept")
        self.assertIn("Cache purged", out)

    def test_the_known_dead_directories_are_removed(self):
        dead = [
            os.path.join("github:sblattj", "OpenCode-goal-plugin#v0.9.1"),
            os.path.join("github:sblattj", "OpenCode-goal-plugin"),
            "opencode-goal-plugin@latest",
        ]
        made = [self.seed(d, populated=True) for d in dead]
        self.run_ferry()
        for d in made:
            self.assertFalse(os.path.exists(d), f"{d} survived")

    def test_an_unrelated_package_directory_is_untouched(self):
        """The safety rule: only paths carrying our package name may be deleted."""
        keep = self.seed("foo", populated=True)
        self.seed("opencode-goal-plugin@latest", populated=True)
        self.run_ferry()
        self.assertTrue(os.path.exists(keep), "an unrelated cache dir was deleted")

    def test_a_migrated_away_spec_loses_its_cache_directory(self):
        old_spec = f"{self.PKG}@https://github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.10.0.tar.gz"
        d = self.seed(os.path.join(
            "opencode-goal-plugin@https:", "github.com", "sblattj",
            "OpenCode-goal-plugin", "archive", "refs", "tags",
            "v0.10.0.tar.gz"), populated=True)
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [old_spec]}, f)
        self.run_ferry()
        self.assertFalse(os.path.exists(d))

    def test_migrating_an_old_tarball_keeps_a_good_canonical_install(self):
        """Purging must delete the PER-SPEC directory, never the whole
        `opencode-goal-plugin@https:` subtree they share - that would destroy a
        working install while cleaning up a stale config entry, and an offline
        laptop would be left with nothing."""
        good = self.seed(self.CANONICAL_REL, populated=True)
        stale = self.seed(os.path.join(
            "opencode-goal-plugin@https:", "github.com", "sblattj",
            "OpenCode-goal-plugin", "archive", "refs", "tags",
            "v0.10.0.tar.gz"), populated=True)
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [
                f"{self.PKG}@https://github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.10.0.tar.gz"]}, f)
        self.run_ferry()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(os.path.join(good, "node_modules",
                                                    self.PKG, "package.json")))

    def test_keep_cache_leaves_everything_alone(self):
        d = self.seed("opencode-goal-plugin@latest", populated=True)
        out = self.run_ferry("--keep-cache")
        self.assertTrue(os.path.exists(d))
        self.assertNotIn("Cache purged", out)

    def test_no_default_never_purges(self):
        d = self.seed("opencode-goal-plugin@latest", populated=True)
        self.run_ferry("--no-default")
        self.assertTrue(os.path.exists(d))


# A stand-in for the real `opencode` binary. Records the argv and the
# environment it was handed, then either emulates a successful install into the
# package cache or fails the way opencode 1.18.29 fails on a git spec.
STUB_OPENCODE = '''#!/usr/bin/env python3
import json, os, sys

spec = sys.argv[2] if len(sys.argv) > 2 else ""
with open(os.environ["FERRY_TEST_RECORD"], "w") as f:
    json.dump({
        "argv": sys.argv[1:],
        "opencode_config": os.environ.get("OPENCODE_CONFIG"),
        "xdg_cache_home": os.environ.get("XDG_CACHE_HOME"),
        "xdg_config_home": os.environ.get("XDG_CONFIG_HOME"),
        "xdg_data_home": os.environ.get("XDG_DATA_HOME"),
        "xdg_state_home": os.environ.get("XDG_STATE_HOME"),
        "cwd": os.getcwd(),
    }, f)

if os.environ.get("FERRY_TEST_STUB_FAIL") == "1":
    print("Install failed")
    print('Could not install "%s"' % spec)
    print("git dep preparation failed")
    sys.exit(1)

root = os.path.join(
    os.path.normpath(os.path.join(os.environ["XDG_CACHE_HOME"], "opencode",
                                  "packages", spec)),
    "node_modules", "opencode-goal-plugin")
os.makedirs(os.path.join(root, "dist"), exist_ok=True)
with open(os.path.join(root, "package.json"), "w") as f:
    json.dump({"name": "opencode-goal-plugin",
               "version": os.environ.get("FERRY_TEST_STUB_VERSION", "0.10.1")}, f)
halves = ["goal-plugin.js", "goal-tui.js"]
# A cache that installed the SERVER half and not the TUI half: the shape the
# pre-install must report, and the one the TUI copy must refuse to make.
if os.environ.get("FERRY_TEST_STUB_NO_TUI") == "1":
    halves.remove("goal-tui.js")
for half in halves:
    open(os.path.join(root, "dist", half), "w").close()
print("Plugin package ready")
print("Installed %s" % spec)
'''


class GoalPluginStubCase(FerryOpencodeCase):
    """A fake `opencode` on PATH that populates the package cache like the real
    one: <cache>/opencode/packages/<spec>/node_modules/opencode-goal-plugin with
    a package.json and both dist halves."""

    PLUGIN = TestGoalPlugin.PLUGIN

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.dir, "stubbin")
        os.makedirs(self.bin, exist_ok=True)
        self.stub = os.path.join(self.bin, "opencode")
        with open(self.stub, "w") as f:
            f.write(STUB_OPENCODE)
        os.chmod(self.stub, 0o755)
        self.record = os.path.join(self.dir, "stub-record.json")

    def run_install(self, *extra, fail=False, version=None, no_tui=False, **kw):
        env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "FERRY_TEST_RECORD": self.record,
            # Deliberately SET, so "the pre-install unsets it" is a real claim
            # instead of a value that was never there. `opencode plugin`
            # patches whatever config this points at.
            "OPENCODE_CONFIG": os.path.join(self.dir, "must-not-be-patched.json"),
        }
        if fail:
            env["FERRY_TEST_STUB_FAIL"] = "1"
        if version:
            env["FERRY_TEST_STUB_VERSION"] = version
        if no_tui:
            env["FERRY_TEST_STUB_NO_TUI"] = "1"
        return self.run_ferry(*extra, install=True, env_extra=env, **kw)

    def recorded(self):
        with open(self.record) as f:
            return json.load(f)


class TestGoalPluginPreInstall(GoalPluginStubCase):
    """Writing a spec installs nothing. This is what makes the failure visible.

    A plugin that fails to install during a normal opencode start is published
    as a Session event and never logged (plugin/index.ts:198-201 -> :139-141,
    no-op reporters at :191-192), the entry is dropped (loader.ts:234) and npm
    plugins are never retried (loader.ts:178) - which is exactly how v1.29.4
    shipped a spec that had never once loaded. `opencode plugin <spec> --global`
    is the only production entry point that PRINTS the real error.
    """

    def test_it_runs_opencode_plugin_with_the_spec_and_global(self):
        self.run_install()
        argv = self.recorded()["argv"]
        self.assertEqual(argv[0], "plugin")
        self.assertIn(self.PLUGIN, argv)
        self.assertIn("--global", argv)

    def test_opencode_config_is_unset_for_the_child(self):
        """Otherwise `opencode plugin` patches the operator's live config with a
        second copy of the entry ferry has just written."""
        self.run_install()
        self.assertIsNone(self.recorded()["opencode_config"])

    def test_the_child_gets_a_throwaway_config_data_and_state_home(self):
        self.run_install()
        rec = self.recorded()
        for key in ("xdg_config_home", "xdg_data_home", "xdg_state_home"):
            self.assertIsNotNone(rec[key], key)
            self.assertNotEqual(rec[key], self.xdg_config, key)
            self.assertNotIn(rec[key], (self.dir, self.xdg_cache, self.xdg_data), key)

    def test_the_child_keeps_the_real_package_cache(self):
        """The sandbox is for the config patch, NOT for the package cache:
        populating the real cache is the entire point of installing early."""
        self.run_install()
        self.assertEqual(self.recorded()["xdg_cache_home"], self.xdg_cache)

    def test_a_successful_install_is_reported_with_the_cached_version(self):
        out = self.run_install()
        self.assertIn("installed opencode-goal-plugin 0.10.1", out)
        self.assertIn("ready for next opencode start", out)
        self.assertNotIn("WARNING", out)

    def test_a_wrong_version_in_the_cache_is_reported_not_hidden(self):
        """Control for the line above: it must read the manifest, not echo the
        ref it was asked for."""
        out = self.run_install(version="0.9.0")
        self.assertIn("WARNING", out)
        self.assertIn("0.9.0", out)

    def test_a_failing_install_warns_and_still_exits_zero(self):
        # run_ferry already asserts rc == 0; the config was written correctly
        # and an offline laptop must not turn a good bootstrap into a red one.
        out = self.run_install(fail=True)
        self.assertIn("WARNING", out)
        self.assertIn("git dep preparation failed", out)
        self.assertIn("opencode will retry on next start", out)

    def test_no_install_skips_it_entirely(self):
        self.run_ferry("--no-install", env_extra={
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "FERRY_TEST_RECORD": self.record,
        })
        self.assertFalse(os.path.exists(self.record))

    def test_a_local_fork_does_not_trigger_an_upstream_install(self):
        fork = "/Users/someone/code/opencode-goal-plugin/dist/server.js"
        with open(self.cfg, "w") as f:
            json.dump({"plugin": [fork]}, f)
        self.run_install()
        self.assertFalse(os.path.exists(self.record))


class TestGoalPluginTuiCopy(GoalPluginStubCase):
    """tui.json must NOT name the spec: the TUI half cannot load from the cache.

    opencode installs a package at `<cache>/opencode/packages/<spec verbatim>/
    node_modules/<pkg>` (packages/core/src/npm.ts:43-47,79), so the canonical
    tarball spec's directory contains the component
    `opencode-goal-plugin@https:`. Bun's runtime plugin runner splits a module
    path at the FIRST colon into `namespace:path`, so a module under a colon
    path never reaches opentui's host-module shim (plugin/tui/runtime.ts:47),
    which is what rewrites `import ... from "solid-js"` into
    `opentui:runtime-module:solid-js`. Bun's native resolver then fails with
    `Cannot find package 'solid-js'` and the sidebar is silently dropped — while
    the SERVER half loads from that very spec, which is what hid it in v1.30.1.
    Reproduced with the same bundle copied byte-for-byte: a colon-free directory
    loads, `/tmp/x/https:/x/...` and `/tmp/x/a:b/...` do not.

    So opencode.json keeps the spec and tui.json gets a file:// URL for the
    colon-free copy ferry maintains under $XDG_DATA_HOME/ferry/.
    """

    REF = TestGoalPlugin.REF
    PKG = TestGoalPlugin.PKG
    MARKER = ".ferry-goal-plugin"

    def setUp(self):
        super().setUp()
        self.oc_dir = os.path.join(self.xdg_config, "opencode")
        self.global_cfg = os.path.join(self.oc_dir, "opencode.json")
        self.tui = os.path.join(self.oc_dir, "tui.json")
        self.copy = os.path.join(self.xdg_data, "ferry", "opencode-goal-plugin")
        # Spelled out rather than round-tripped through pathlib, so a bug shared
        # with the implementation's as_uri() call cannot pass. Safe because the
        # scratch path has no character percent-encoding would touch — asserted.
        self.assertNotIn(":", self.copy)
        self.assertNotIn("#", self.copy)
        self.assertEqual(self.copy, urllib.parse.quote(self.copy))
        self.uri = "file://" + self.copy

    def read_tui(self):
        with open(self.tui) as f:
            return json.load(f)

    def seed_copy(self, version=None, marker_spec=None, leftover=None):
        """A managed copy already on disk, as a previous run would have left it."""
        os.makedirs(os.path.join(self.copy, "dist"), exist_ok=True)
        with open(os.path.join(self.copy, "package.json"), "w") as f:
            json.dump({"name": self.PKG, "version": version or "0.10.1"}, f)
        open(os.path.join(self.copy, "dist", "goal-tui.js"), "w").close()
        open(os.path.join(self.copy, "dist", "goal-plugin.js"), "w").close()
        if leftover:
            open(os.path.join(self.copy, leftover), "w").close()
        with open(os.path.join(self.copy, self.MARKER), "w") as f:
            f.write("%s\n%s\n%s\n" % (marker_spec or self.PLUGIN,
                                      "v" + (version or "0.10.1"), self.PKG))

    def copy_version(self):
        with open(os.path.join(self.copy, "package.json")) as f:
            return json.load(f)["version"]

    def fingerprint(self):
        """Every file in the copy with its size and mtime, for a no-touch claim."""
        out = {}
        for root, _dirs, files in os.walk(self.copy):
            for name in files:
                p = os.path.join(root, name)
                st = os.stat(p)
                out[os.path.relpath(p, self.copy)] = (st.st_size, st.st_mtime_ns)
        return out

    # ── the install path ──────────────────────────────────────────────────
    def test_an_install_run_points_tui_json_at_a_copy_it_made(self):
        out = self.run_install(config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [self.uri])
        # The CONTROL that makes the line above mean something: opencode.json
        # keeps the tarball spec, because the SERVER half resolves out of the
        # cache next to the `zod` installed with it.
        with open(self.global_cfg) as f:
            self.assertEqual(json.load(f)["plugin"], [self.PLUGIN])
        self.assertTrue(os.path.isfile(os.path.join(self.copy, self.MARKER)))
        self.assertEqual(self.copy_version(), "0.10.1")
        self.assertTrue(os.path.exists(os.path.join(self.copy, "dist", "goal-tui.js")))
        self.assertIn("TUI plugin: %s (0.10.1) -> %s" % (self.copy, self.tui), out)

    def test_the_marker_records_the_spec_the_copy_came_from(self):
        """A copy with no provenance cannot be refreshed on a ref bump, and
        cannot be told apart from a directory somebody else put there."""
        self.run_install(config=self.global_cfg)
        with open(os.path.join(self.copy, self.MARKER)) as f:
            self.assertEqual(f.read().splitlines(),
                             [self.PLUGIN, self.REF, self.PKG])

    def test_a_second_install_run_is_idempotent(self):
        self.run_install(config=self.global_cfg)
        out = self.run_install(config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [self.uri])
        self.assertNotIn("WARNING", out)

    # ── --no-install ──────────────────────────────────────────────────────
    def test_no_install_on_a_fresh_machine_keeps_the_spec_and_says_pending(self):
        out = self.run_ferry("--no-install", config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [self.PLUGIN])
        self.assertIn("tui.json: spec, TUI copy pending install", out)
        self.assertNotIn(self.uri, out)
        self.assertFalse(os.path.exists(self.copy))

    def test_no_install_with_a_good_copy_uses_it_and_touches_nothing(self):
        self.seed_copy()
        before = self.fingerprint()
        out = self.run_ferry("--no-install", config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [self.uri])
        self.assertIn("tui.json: " + self.uri, out)
        self.assertNotIn("pending install", out)
        self.assertEqual(self.fingerprint(), before)

    # ── refusals ──────────────────────────────────────────────────────────
    def test_a_directory_without_the_marker_is_left_alone(self):
        """The managed path could be someone's checkout. Ferry deletes only what
        it can prove it wrote."""
        os.makedirs(self.copy, exist_ok=True)
        theirs = os.path.join(self.copy, "not-ours.txt")
        with open(theirs, "w") as f:
            f.write("mine")
        out = self.run_install(config=self.global_cfg)
        self.assertIn("not ferry's", out)
        self.assertIn("left alone", out)
        with open(theirs) as f:                      # control: still theirs
            self.assertEqual(f.read(), "mine")
        self.assertFalse(os.path.exists(os.path.join(self.copy, "package.json")))
        self.assertEqual(self.read_tui()["plugin"], [self.PLUGIN])

    def test_a_colon_in_the_data_home_falls_back_to_the_spec(self):
        """A managed path with a ':' in it reproduces the very Bun bug the copy
        exists to dodge, so there is no point making one."""
        bad = os.path.join(self.dir, "a:b")
        os.makedirs(bad, exist_ok=True)
        out = self.run_install(config=self.global_cfg, xdg_data=bad)
        self.assertIn("contains ':' or '#'", out)
        self.assertEqual(self.read_tui()["plugin"], [self.PLUGIN])
        self.assertFalse(os.path.exists(os.path.join(bad, "ferry")))
        # Control: the pre-install still ran, so this is a TUI-copy refusal and
        # not a run that fell over before it got there.
        self.assertIn("installed opencode-goal-plugin 0.10.1", out)

    def test_a_cache_without_the_tui_bundle_is_reported_not_copied(self):
        out = self.run_install(config=self.global_cfg, no_tui=True)
        self.assertIn("TUI plugin: not copied", out)
        self.assertIn("goal-tui.js", out)
        self.assertEqual(self.read_tui()["plugin"], [self.PLUGIN])
        self.assertFalse(os.path.exists(self.copy))

    # ── a ref bump ────────────────────────────────────────────────────────
    def test_a_stale_copy_is_replaced_wholesale_and_named(self):
        self.seed_copy(version="0.9.0", leftover="gone-in-0.10.1.js")
        out = self.run_install(config=self.global_cfg)
        self.assertIn("copy holds 0.9.0, expected 0.10.1", out)
        self.assertIn("refreshed after the install below", out)
        self.assertEqual(self.copy_version(), "0.10.1")
        self.assertEqual(self.read_tui()["plugin"], [self.uri])
        # Wholesale, not merged: a file the old version shipped is gone.
        self.assertFalse(os.path.exists(os.path.join(self.copy, "gone-in-0.10.1.js")))

    def test_no_install_with_a_stale_copy_says_how_to_fix_it(self):
        self.seed_copy(version="0.9.0")
        out = self.run_ferry("--no-install", config=self.global_cfg)
        self.assertIn("copy holds 0.9.0, expected 0.10.1", out)
        self.assertIn("rerun without --no-install", out)
        self.assertEqual(self.copy_version(), "0.9.0")   # nothing refreshed it

    def test_a_current_copy_prints_no_stale_note(self):
        """Control for the two above: the note must key on the VERSION, not on
        the copy merely existing."""
        self.seed_copy()
        out = self.run_ferry("--no-install", config=self.global_cfg)
        self.assertNotIn("copy holds", out)

    # ── the managed entry is ferry's, every other path is not ─────────────
    def test_a_managed_entry_in_opencode_json_is_migrated_to_the_spec(self):
        """The server half must never load from the copy: its dependencies were
        installed beside it in the cache, not beside the copy."""
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(self.global_cfg, "w") as f:
            json.dump({"plugin": [self.uri]}, f)
        self.run_ferry("--no-install", config=self.global_cfg)
        with open(self.global_cfg) as f:
            self.assertEqual(json.load(f)["plugin"], [self.PLUGIN])

    def test_the_managed_entry_and_the_spec_collapse_to_one_in_tui_json(self):
        self.seed_copy()
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(self.tui, "w") as f:
            json.dump({"plugin": [self.PLUGIN, self.uri]}, f)
        self.run_ferry("--no-install", config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [self.uri])

    def test_a_managed_tuple_entry_keeps_its_options(self):
        self.seed_copy()
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(self.tui, "w") as f:
            json.dump({"plugin": [[self.PLUGIN, {"enabled": True}]]}, f)
        self.run_ferry("--no-install", config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [[self.uri, {"enabled": True}]])

    def test_the_install_rewrite_keeps_tuple_options_too(self):
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(self.tui, "w") as f:
            json.dump({"plugin": [[self.PLUGIN, {"enabled": True}]]}, f)
        self.run_install(config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [[self.uri, {"enabled": True}]])

    def test_someone_elses_path_entries_are_untouched_in_both_files(self):
        """A `file://` URL and an absolute path that are NOT ferry's managed
        copy are forks, and forks are never rewritten."""
        theirs = ["file:///Users/someone/code/my-fork",
                  "/Users/someone/plugins/tidy.js"]
        os.makedirs(self.oc_dir, exist_ok=True)
        for path in (self.global_cfg, self.tui):
            with open(path, "w") as f:
                json.dump({"plugin": list(theirs)}, f)
        self.run_ferry("--no-install", config=self.global_cfg)
        with open(self.global_cfg) as f:
            self.assertEqual(json.load(f)["plugin"], theirs + [self.PLUGIN])
        self.assertEqual(self.read_tui()["plugin"], theirs + [self.PLUGIN])

    def test_a_local_fork_is_mirrored_into_tui_json_verbatim(self):
        """When a fork satisfies the server half it satisfies the TUI half too,
        and ferry has no copy of somebody's fork to point at."""
        fork = "/Users/someone/code/opencode-goal-plugin/dist/server.js"
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(self.global_cfg, "w") as f:
            json.dump({"plugin": [fork]}, f)
        out = self.run_ferry("--no-install", config=self.global_cfg)
        self.assertEqual(self.read_tui()["plugin"], [fork])
        self.assertIn("upstream not added", out)
        self.assertFalse(os.path.exists(self.copy))

    def test_the_canonical_cache_directory_survives_the_tui_rewrite(self):
        """tui.json moving off the spec is NOT a migration: opencode.json still
        points at that cache directory, and purging it would leave an offline
        laptop with no plugin at all."""
        self.run_install(config=self.global_cfg)
        canon = os.path.join(self.packages_root(), *TestGoalPluginCacheHygiene
                             .CANONICAL_REL.split(os.sep))
        installed = os.path.join(canon, "node_modules", self.PKG, "package.json")
        self.assertTrue(os.path.exists(installed), installed)
        out = self.run_ferry("--no-install", config=self.global_cfg)
        self.assertTrue(os.path.exists(installed), out)
        self.assertNotIn("Cache purged", out)


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

    `light`/`standard` are a DIFFERENT thing and must not be caught by this
    guard: they are custom subagents ferry declares deliberately, and a custom
    agent is legitimately absent from the built-in list. What distinguishes
    them from a phantom pin is that they carry `mode: "subagent"` and a
    description — the fields opencode reads for a user-declared agent — so this
    class checks the two populations separately.
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


class TestAgentKeysAreRealOrDeclared(FerryOpencodeCase):
    """Every key ferry writes under `agent` is either an opencode built-in or a
    properly DECLARED custom subagent. This is the generalisation of the scout
    bug: an unknown key lands in additionalProperties and is never read, so a
    typo'd or invented name costs nothing and does nothing.
    """

    def test_no_agent_key_is_an_undeclared_invention(self):
        self.run_ferry()
        agent = self.read()["agent"]
        for name, spec in agent.items():
            if name in BUILTIN_AGENTS:
                continue
            self.assertIn(name, CUSTOM_AGENTS,
                          f"{name} is neither an opencode built-in nor a "
                          f"custom agent ferry declares")
            # A custom agent opencode will actually surface: mode + description.
            self.assertEqual(spec.get("mode"), "subagent", name)
            self.assertTrue(spec.get("description"), name)

    def test_ferry_pins_exactly_the_builtins_it_means_to(self):
        # Six pinned to a lane, plus `general` disabled. If opencode renames a
        # built-in, this is the assertion that notices.
        self.run_ferry()
        agent = self.read()["agent"]
        pinned_builtins = {n for n in agent if n in BUILTIN_AGENTS}
        self.assertEqual(pinned_builtins, BUILTIN_AGENTS)
        for name in BUILTIN_AGENTS - set(DISABLED_AGENTS):
            self.assertTrue(agent[name]["model"].startswith("ferry/"), name)
        for name in DISABLED_AGENTS:
            self.assertNotIn("model", agent[name],
                             "a disabled agent must not also carry a lane pin")


class TestFleetHeaders(FerryOpencodeCase):
    """v1.26.0 — `ferry opencode` bakes X-Ferry-Client / X-Ferry-Fleet into
    provider.ferry.options.headers so the front door's fleet resolver
    (front/ferry_front.py, per docs/superpowers/specs/2026-09-04-fleets-design.md
    §4/§6) can identify the caller and honour a one-shot FERRY_FLEET override.
    """

    def host_home(self):
        # No ~/.config/ferry/client.json at all: CLIENT_MODE=0, so CLIENT_NAME
        # resolves to the literal "host" (lib/ferry-core.zsh).
        home = tempfile.mkdtemp(prefix="ferry-oc-hosthome-")
        self.addCleanup(shutil.rmtree, home, True)
        return home

    def client_home_named(self, name):
        home = tempfile.mkdtemp(prefix="ferry-oc-clienthome-")
        self.addCleanup(shutil.rmtree, home, True)
        fdir = os.path.join(home, ".config", "ferry")
        os.makedirs(fdir)
        prof = {"host": "127.0.0.1", "port": str(self.port), "name": name}
        with open(os.path.join(fdir, "client.json"), "w") as f:
            json.dump(prof, f)
        return home

    def test_headers_present_with_the_exact_fleet_placeholder(self):
        home = self.host_home()
        self.run_ferry(home=home)
        with open(self.cfg) as f:
            raw = f.read()
        # Byte-for-byte on the RAW file text: opencode substitutes "{env:VAR}"
        # itself at load time, so ferry must never resolve or mangle it.
        self.assertIn('"X-Ferry-Fleet": "{env:FERRY_FLEET}"', raw)
        headers = self.read()["provider"]["ferry"]["options"]["headers"]
        self.assertEqual(headers, {
            "X-Ferry-Client": "host",
            "X-Ferry-Fleet": "{env:FERRY_FLEET}",
        })

    def test_a_pre_existing_options_key_survives_while_stale_headers_are_replaced(self):
        home = self.host_home()
        with open(self.cfg, "w") as f:
            json.dump({
                "provider": {"ferry": {"options": {
                    "foo": "keep",
                    "headers": {"X-Ferry-Client": "stale", "X-Stale-Only": "gone"},
                }}},
            }, f)
        self.run_ferry(home=home)
        opts = self.read()["provider"]["ferry"]["options"]
        self.assertEqual(opts["foo"], "keep")
        self.assertEqual(opts["headers"], {
            "X-Ferry-Client": "host",
            "X-Ferry-Fleet": "{env:FERRY_FLEET}",
        })

    def test_no_client_profile_names_the_caller_host(self):
        home = self.host_home()
        self.run_ferry(home=home)
        headers = self.read()["provider"]["ferry"]["options"]["headers"]
        self.assertEqual(headers["X-Ferry-Client"], "host")

    def test_a_named_client_profile_names_the_client(self):
        home = self.client_home_named("laptop")
        self.run_ferry(home=home)
        headers = self.read()["provider"]["ferry"]["options"]["headers"]
        self.assertEqual(headers["X-Ferry-Client"], "laptop")


if __name__ == "__main__":
    if not os.path.exists(FERRY):
        sys.exit("built ./ferry not found — run ./build.zsh first")
    unittest.main(verbosity=2)
