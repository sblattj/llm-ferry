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
    end: a client carried `compaction -> ferry/gemini-3.7-flash` for months
    after that lane was renamed, resolving only through a `hidden: true`
    back-compat alias that /v1/models does not advertise.
  * EVERY other key is left alone. Nuking a user's mcp/lsp/theme block is the
    regression this file exists to catch.
  * `provider.ferry.models` declares exactly the LANE PAIR, never the served
    catalogue. The host advertises the fallback deployments too (flash-gem,
    orch-deepseek, ...); those are reached by the router on overflow, not by a
    client picking one out of a menu, and a real model id must never reach a
    client config.
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
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

# What the live host advertises. Only `orch` and `flash` may reach a config.
CATALOGUE = ["orch", "orch-zai-glm53", "orch-deepseek", "orch-gpt56-sol",
             "flash", "flash-or-glm", "flash-gem", "flash-or",
             "local-orch", "local-sub"]

BUILTIN_AGENTS = {"build", "plan", "general", "explore",
                  "title", "summary", "compaction"}

SNAP_RE = re.compile(r"^[^/]+\.\d{8}T\d{6}Z(-\d+)?\.jsonc$")


class _ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
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

    def run_ferry(self, *extra, config=None):
        cfg = config or self.cfg
        cmd = ["zsh", FERRY, "opencode", "--host", "127.0.0.1",
               "--port", str(self.port), "--config", cfg, *extra]
        # env -u OPENCODE_CONFIG equivalent: the command honours it as the
        # default target, and an inherited one would silently redirect the write.
        env = {k: v for k, v in os.environ.items() if k != "OPENCODE_CONFIG"}
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
        "small_model": "ferry/gemini-3.7-flash",
        "mcp": {"context7": {"type": "local", "command": ["npx", "ctx7"]}},
        "lsp": {"typescript": {"disabled": False}},
        "agent": {
            "build": {"model": "ferry/orch"},
            "compaction": {"model": "ferry/gemini-3.7-flash"},
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

    def test_permission_becomes_the_bare_allow_enum(self):
        # opencode's schema: PermissionConfig is anyOf[PermissionActionConfig,
        # {read,edit,bash,...}], and PermissionActionConfig is the bare enum.
        self.run_ferry()
        self.assertEqual(self.read()["permission"], "allow")

    def test_model_and_small_model_are_lane_names(self):
        self.run_ferry()
        cfg = self.read()
        self.assertEqual(cfg["model"], "ferry/orch")
        self.assertEqual(cfg["small_model"], "ferry/flash")

    def test_agent_section_is_replaced_wholesale(self):
        self.run_ferry()
        agent = self.read()["agent"]
        self.assertEqual(set(agent), BUILTIN_AGENTS)
        self.assertNotIn("my-custom-agent", agent, "agent must be replaced, not merged")
        self.assertNotIn("scout", agent, "scout is not an opencode agent")

    def test_stale_retired_lane_pin_is_gone(self):
        # The whole point: `compaction -> ferry/gemini-3.7-flash` resolved only
        # through a hidden back-compat alias. After a takeover, no key anywhere
        # in the file may name anything but the two lanes.
        self.run_ferry()
        blob = json.dumps(self.read())
        self.assertNotIn("gemini-3.7-flash", blob)

    def test_driver_and_worker_split(self):
        self.run_ferry()
        agent = self.read()["agent"]
        for a in ("build", "plan"):
            self.assertEqual(agent[a]["model"], "ferry/orch")
        for a in ("general", "explore", "title", "summary", "compaction"):
            self.assertEqual(agent[a]["model"], "ferry/flash")

    def test_no_default_leaves_the_takeover_keys_alone(self):
        self.run_ferry("--no-default")
        cfg = self.read()
        self.assertEqual(cfg["permission"], self.USER_CONFIG["permission"])
        self.assertEqual(cfg["model"], self.USER_CONFIG["model"])
        self.assertIn("my-custom-agent", cfg["agent"])
        self.assertIn("ferry", cfg["provider"])   # provider is still wired


class TestLaneNamesOnly(FerryOpencodeCase):
    """The served catalogue validates the pair; it never populates the config."""

    def test_only_the_lane_pair_is_declared(self):
        self.run_ferry()
        models = self.read()["provider"]["ferry"]["models"]
        self.assertEqual(set(models), {"orch", "flash"})

    def test_fallback_deployments_never_reach_the_config(self):
        self.run_ferry()
        blob = json.dumps(self.read())
        for lane in ("flash-gem", "flash-or", "flash-or-glm",
                     "orch-deepseek", "orch-zai-glm53", "orch-gpt56-sol"):
            self.assertNotIn(lane, blob, f"{lane} is router-only, not client-selectable")

    def test_local_pair_carries_the_kv_limits(self):
        # The local lanes cap KV at 131072, so opencode's 32k output reservation
        # has to be pulled down to 8k or a ~100k prompt returns a clean 400.
        self.run_ferry("--local")
        cfg = self.read()
        models = cfg["provider"]["ferry"]["models"]
        self.assertEqual(set(models), {"local-orch", "local-sub"})
        for lane in models.values():
            self.assertEqual(lane["limit"], {"context": 131072, "output": 8192})
        self.assertEqual(cfg["model"], "ferry/local-orch")
        self.assertEqual(cfg["small_model"], "ferry/local-sub")

    def test_unserved_lane_warns_instead_of_silently_wiring(self):
        out = self.run_ferry("--model", "no-such-lane")
        self.assertIn("does not serve", out)
        self.assertIn("no-such-lane", out)


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
