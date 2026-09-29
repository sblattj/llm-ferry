#!/usr/bin/env python3
"""Stdlib unittest for `ferry cline` — the Cline (VS Code) takeover.

Run:  python3 lib/ferry-cline.test.py

`ferry cline` pre-seeds Cline's file-backed provider config (~/.cline) so a
fresh machine gets the ferry endpoint on first launch — seat-01 established
that the target Macs have no VS Code yet, so pre-seed IS the normal flow, and
missing VS Code/Cline is a note, not an error. The config surface it owns is
three plain files (Cline v4.1.21 dropped SecretStorage):

  data/globalState.json           act/plan provider, baseUrl, model ids, headers
  data/secrets.json               {"openAiApiKey": ...} — mode 0600
  data/settings/providers.json    the SDK-shared mirror ("openai-compatible")

plus a ~/.config/ferry/cline.json record of what was wired, snapshotted .bak
like claude.json. The two behaviors most worth pinning, both from the DESIGN
fact base (swarm/cline-support-20260929/DESIGN.md section 1):

  * the provider-id duality — globalState says "openai", providers.json says
    "openai-compatible", and a fresh Cline defaults actModeApiProvider to
    "openrouter" when the key is absent, so writing ONLY providers.json is
    silently ignored;
  * the API key must be non-empty even when ferry ignores auth — Cline only
    sends Authorization when a key exists, so keyless setups bake "local".

Runs the REAL `cmd_cline` against a throwaway $HOME and a stub /v1/models
catalogue — through the built `ferry` monolith when it answers
`ferry cline --help`, otherwise by sourcing lib/ferry-core.zsh +
lib/ferry-cline.zsh in a zsh subprocess (the pattern of ferry-claude.test.py;
ferry-integrate.test.py contributes the stub-server half).
"""
import datetime
import glob
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
CLINE_MODULE = os.path.join(REPO, "lib", "ferry-cline.zsh")

# A minimal catalogue: the DESIGN fallback target (heavy) is served, a
# selectable second lane (medium) exists, and `flash` is there so a
# served-lane test has a lane that is NOT the fallback. Nothing else — the
# lane-check tests rely on super-flash being absent.
CATALOGUE = ["heavy", "medium", "flash"]

MARKETPLACE_ID = "saoudrizwan.claude-dev"


def _monolith_supports_cline():
    """True when the built `ferry` monolith answers `ferry cline --help`.

    The monolith is GENERATED (build.zsh concatenates lib/ferry-*.zsh), so it
    lags the module until someone rebuilds. When it is stale or absent, the
    functional tests fall back to sourcing the modules directly — the contract
    under test is the module, not the packaging step. ("Unknown command" can
    arrive with returncode 0, so the string check is what decides.)
    """
    if not os.path.exists(FERRY):
        return False
    try:
        p = subprocess.run(
            ["zsh", FERRY, "cline", "--help"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "Unknown command" not in (p.stdout + p.stderr)


MONOLITH = _monolith_supports_cline()


class _ModelsHandler(BaseHTTPRequestHandler):
    """The one endpoint the lane check depends on: GET /v1/models."""

    def do_GET(self):  # noqa: N802 - stdlib naming
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


class ClineHarness(unittest.TestCase):
    """Shared fixture: stub catalogue host, throwaway $HOME, one runner."""

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
        self.home = tempfile.mkdtemp(prefix="ferry-cline-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.data = os.path.join(self.home, "clinedata")

    # --- helpers ------------------------------------------------------------
    def env(self, extra=None):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["TMPDIR"] = self.home
        # Hermetic in both directions: nothing the operator's shell carries
        # may steer the resolution (XDG roots would relocate ~/.config, the
        # two master-key globals would fake the key-precedence tests, a stray
        # FERRY_FLEET would fake the fleet-header ones), and nothing this run
        # writes may land outside the throwaway HOME.
        for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
                  "OPENCODE_CONFIG", "OPENCODE_TUI_CONFIG",
                  "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY",
                  "FERRY_FLEET", "CLINE_DATA_DIR"):
            e.pop(k, None)
        if extra:
            e.update(extra)
        return e

    def run_cline(self, *extra, data=None, pass_data_dir=True, port=None,
                  env_extra=None):
        """Drive the REAL `cmd_cline` — via the built monolith when it is in
        sync, otherwise by sourcing the lib/ modules in a zsh subprocess."""
        args = ["--host", "127.0.0.1",
                "--port", str(port if port is not None else self.port)]
        if pass_data_dir:
            args += ["--data-dir", data or self.data]
        args += list(extra)
        if MONOLITH:
            cmd = ["zsh", FERRY, "cline", *args]
        else:
            if not os.path.exists(CLINE_MODULE):
                self.fail(
                    "neither the built ferry monolith nor lib/ferry-cline.zsh "
                    "exists — the cline command has not landed yet"
                )
            script = (
                f"source {REPO}/lib/ferry-core.zsh\n"
                f"source {REPO}/lib/ferry-cline.zsh\n"
                'cmd_cline "$@"\n'
            )
            cmd = ["zsh", "-c", script, "ferry-cline", *args]
        p = subprocess.run(
            cmd, capture_output=True, text=True,
            env=self.env(env_extra), cwd=REPO, timeout=180,
        )
        self.assertEqual(p.returncode, 0,
                         f"ferry cline failed:\n{p.stdout}\n{p.stderr}")
        return p

    def gpath(self, *parts):
        """A file under the Cline data dir's own data/ subtree."""
        return os.path.join(self.data, "data", *parts)

    def read_json(self, path):
        with open(path) as f:
            return json.load(f)

    def cline_json_path(self):
        return os.path.join(self.home, ".config", "ferry", "cline.json")

    def write_client_profile(self, master_key=None, api_key=None, name=None):
        fdir = os.path.join(self.home, ".config", "ferry")
        os.makedirs(fdir, exist_ok=True)
        prof = {"host": "127.0.0.1", "port": str(self.port)}
        if master_key is not None:
            prof["master_key"] = master_key
        if api_key is not None:
            prof["api_key"] = api_key
        if name is not None:
            prof["name"] = name
        with open(os.path.join(fdir, "client.json"), "w") as f:
            json.dump(prof, f)

    def ferry_baks(self, path):
        return sorted(glob.glob(path + ".*.ferry.bak"))


class FreshInstallTest(ClineHarness):
    """What one `ferry cline` run writes into an empty data dir."""

    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def test_globalstate_pins_act_and_plan_to_the_openai_provider(self):
        """The #1 trap from the DESIGN fact base, pinned end to end: extension
        state names the provider "openai" (never "openai-compatible"), and
        BOTH mode selectors must say it — a fresh Cline defaults
        actModeApiProvider to "openrouter", so a half-written globalState is
        silently ignored."""
        self.run_cline()
        gs = self.read_json(self.gpath("globalState.json"))
        self.assertEqual(gs["actModeApiProvider"], "openai")
        self.assertEqual(gs["planModeApiProvider"], "openai")
        self.assertEqual(gs["openAiBaseUrl"], self.base_url())
        self.assertEqual(gs["actModeOpenAiModelId"], "heavy")
        self.assertEqual(gs["planModeOpenAiModelId"], "heavy")
        self.assertIs(gs["welcomeViewCompleted"], True,
                      "welcomeViewCompleted must be true, or Cline shows its "
                      "onboarding over the pre-seed")
        headers = gs["openAiHeaders"]
        self.assertTrue(headers.get("X-Ferry-Client"),
                        "the X-Ferry-Client identity header is missing or empty")
        self.assertNotIn("X-Ferry-Fleet", headers,
                         "fleet header baked without FERRY_FLEET in the env")

    def test_secrets_json_carries_a_non_empty_key_at_mode_600(self):
        """Cline only sends Authorization when a key exists, so even a keyless
        LAN setup must bake a non-empty placeholder — and the file is a
        credential, mode 0600 on create."""
        self.run_cline()
        secrets = self.read_json(self.gpath("secrets.json"))
        self.assertEqual(secrets["openAiApiKey"], "local")
        mode = stat.S_IMODE(os.stat(self.gpath("secrets.json")).st_mode)
        self.assertEqual(mode, 0o600, f"secrets.json created {oct(mode)}, not 0600")

    def test_providers_json_mirrors_the_sdk_shape(self):
        self.run_cline()
        prov = self.read_json(self.gpath("settings", "providers.json"))
        self.assertEqual(prov["version"], 1)
        self.assertEqual(prov["lastUsedProvider"], "openai-compatible")
        self.assertEqual(prov["modes"], {})
        entry = prov["providers"]["openai-compatible"]
        self.assertEqual(entry["tokenSource"], "manual")
        s = entry["settings"]
        self.assertEqual(s["provider"], "openai-compatible")
        self.assertEqual(s["baseUrl"], self.base_url())
        self.assertEqual(s["apiKey"], "local")
        self.assertEqual(s["model"], "heavy")
        self.assertIn("X-Ferry-Client", s["headers"])
        # updatedAt is a real ISO8601 UTC timestamp, not a frozen string.
        raw = entry["updatedAt"]
        try:
            when = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            self.fail(f"updatedAt {raw!r} is not ISO8601")
        self.assertIsNotNone(when.tzinfo, "updatedAt carries no timezone")

    def test_cline_json_records_the_endpoint(self):
        self.run_cline()
        cfg = self.read_json(self.cline_json_path())
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(str(cfg["port"]), str(self.port))
        self.assertEqual(cfg["model"], "heavy")
        self.assertEqual(cfg["data_dir"], self.data)
        # Keyless: the mirror must not claim an auth setup that does not exist.
        self.assertNotIn("master_key", cfg)

    def test_the_summary_names_the_marketplace_extension(self):
        """The one-time hint is the operator's only path to an install — it
        must name the exact extension id, and the run still exits 0."""
        p = self.run_cline()
        self.assertIn(MARKETPLACE_ID, p.stdout + p.stderr)


class MergeTest(ClineHarness):
    """A machine that already runs Cline: merge, never clobber."""

    OLD_GLOBAL = {
        "actModeApiProvider": "openrouter",
        "planModeApiProvider": "anthropic",
        "openAiBaseUrl": "http://oldhost:1/v1",
        "actModeOpenAiModelId": "old-lane",
        "planModeOpenAiModelId": "old-lane",
        "openAiHeaders": {"X-Ferry-Client": "oldname"},
        "welcomeViewCompleted": False,
        "language": "fr",
        "myCustomSetting": {"keep": True},
    }
    OLD_SECRETS = {"openAiApiKey": "old-key", "anthropicApiKey": "keepme"}
    OLD_PROVIDERS = {
        "version": 2,
        "lastUsedProvider": "anthropic",
        "providers": {"anthropic": {"settings": {"provider": "anthropic"}}},
    }

    def setUp(self):
        super().setUp()
        os.makedirs(self.gpath("settings"), exist_ok=True)
        with open(self.gpath("globalState.json"), "w") as f:
            json.dump(self.OLD_GLOBAL, f)
        with open(self.gpath("secrets.json"), "w") as f:
            json.dump(self.OLD_SECRETS, f)
        os.chmod(self.gpath("secrets.json"), 0o644)
        with open(self.gpath("settings", "providers.json"), "w") as f:
            json.dump(self.OLD_PROVIDERS, f)
        # A previous wiring's record, so the .bak spelling gets exercised too.
        os.makedirs(os.path.dirname(self.cline_json_path()), exist_ok=True)
        with open(self.cline_json_path(), "w") as f:
            json.dump({"host": "oldhost", "port": "1"}, f)

    def test_unknown_keys_survive_and_old_values_are_replaced(self):
        self.run_cline()
        gs = self.read_json(self.gpath("globalState.json"))
        self.assertEqual(gs["language"], "fr",
                         "the merge clobbered the user's language setting")
        self.assertEqual(gs["myCustomSetting"], {"keep": True})
        # ...while every ferry-owned key moved to the new wiring.
        self.assertEqual(gs["actModeApiProvider"], "openai")
        self.assertEqual(gs["openAiBaseUrl"], f"http://127.0.0.1:{self.port}/v1")
        self.assertEqual(gs["actModeOpenAiModelId"], "heavy")

        secrets = self.read_json(self.gpath("secrets.json"))
        self.assertEqual(secrets["openAiApiKey"], "local")
        self.assertEqual(secrets["anthropicApiKey"], "keepme",
                         "a provider secret ferry does not own was dropped")
        mode = stat.S_IMODE(os.stat(self.gpath("secrets.json")).st_mode)
        self.assertEqual(mode, 0o600,
                         f"a 0644 secrets.json was left at {oct(mode)}")

        prov = self.read_json(self.gpath("settings", "providers.json"))
        self.assertEqual(prov["version"], 1)
        self.assertEqual(prov["lastUsedProvider"], "openai-compatible")
        self.assertIn("anthropic", prov["providers"],
                      "the merge dropped another provider's entry")
        self.assertIn("openai-compatible", prov["providers"])

    def test_each_pre_existing_file_is_snapshotted(self):
        """Reversible by design: the three Cline files get a
        <file>.<UTC>.ferry.bak and cline.json a claude.json-style .bak."""
        self.run_cline()

        for path, old in ((self.gpath("globalState.json"), self.OLD_GLOBAL),
                          (self.gpath("secrets.json"), self.OLD_SECRETS),
                          (self.gpath("settings", "providers.json"),
                           self.OLD_PROVIDERS)):
            snaps = self.ferry_baks(path)
            self.assertTrue(snaps, f"no .ferry.bak snapshot beside {path}")
            with open(snaps[0]) as f:
                self.assertEqual(json.load(f), old,
                                  f"the snapshot does not hold the PREVIOUS {path}")

        record_snaps = sorted(glob.glob(self.cline_json_path() + ".*.bak"))
        self.assertTrue(record_snaps, "the pre-existing cline.json was not snapshotted")
        with open(record_snaps[0]) as f:
            self.assertEqual(json.load(f).get("host"), "oldhost")


class LaneCheckTest(ClineHarness):
    """The catalogue probe: warn and adapt, never hard-fail."""

    def test_an_unserved_lane_falls_back_to_heavy_with_a_warning(self):
        p = self.run_cline("--model", "super-flash")   # not in CATALOGUE
        gs = self.read_json(self.gpath("globalState.json"))
        self.assertEqual(gs["actModeOpenAiModelId"], "heavy")
        self.assertEqual(gs["planModeOpenAiModelId"], "heavy")
        prov = self.read_json(self.gpath("settings", "providers.json"))
        self.assertEqual(prov["providers"]["openai-compatible"]["settings"]["model"],
                         "heavy")
        out = p.stdout + p.stderr
        self.assertIn("super-flash", out, "the warning must name the old lane")
        self.assertIn("heavy", out, "the warning must name the new lane")

    def test_a_served_lane_is_kept(self):
        self.run_cline("--model", "flash")
        gs = self.read_json(self.gpath("globalState.json"))
        self.assertEqual(gs["actModeOpenAiModelId"], "flash")
        self.assertEqual(gs["planModeOpenAiModelId"], "flash")

    def test_offline_host_still_writes_everything_with_a_warning(self):
        """No server at all: the lane cannot be verified, the CHOSEN model is
        kept (not silently swapped), every file is still written, exit 0."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        p = self.run_cline("--model", "flash", port=dead)
        combined = (p.stdout + p.stderr).lower()
        self.assertIn("could not", combined,
                      "an unreachable catalogue must produce a warning, not silence")
        gs = self.read_json(self.gpath("globalState.json"))
        self.assertEqual(gs["actModeOpenAiModelId"], "flash",
                         "offline must keep the chosen lane, not swap it")
        self.assertTrue(os.path.exists(self.gpath("secrets.json")))
        self.assertTrue(os.path.exists(self.gpath("settings", "providers.json")))
        self.assertTrue(os.path.exists(self.cline_json_path()))


class KeyTest(ClineHarness):
    """Key precedence: --key > client.json master_key > CLIENT_MASTER_KEY >
    "local" — and the identity header follows client.json's name."""

    PROFILE_KEY = "sk-cline-profile"
    FLAG_KEY = "sk-cline-flag"

    def test_a_profile_master_key_reaches_the_files(self):
        self.write_client_profile(master_key=self.PROFILE_KEY)
        self.run_cline()
        self.assertEqual(self.read_json(self.gpath("secrets.json"))["openAiApiKey"],
                         self.PROFILE_KEY)
        self.assertEqual(self.read_json(self.cline_json_path())["master_key"],
                         self.PROFILE_KEY)
        prov = self.read_json(self.gpath("settings", "providers.json"))
        self.assertEqual(
            prov["providers"]["openai-compatible"]["settings"]["apiKey"],
            self.PROFILE_KEY)

    def test_the_key_flag_beats_the_profile_master_key(self):
        self.write_client_profile(master_key=self.PROFILE_KEY)
        self.run_cline("--key", self.FLAG_KEY)
        self.assertEqual(self.read_json(self.gpath("secrets.json"))["openAiApiKey"],
                         self.FLAG_KEY)
        self.assertEqual(self.read_json(self.cline_json_path())["master_key"],
                         self.FLAG_KEY)
        self.assertNotIn(self.PROFILE_KEY,
                         json.dumps(self.read_json(self.gpath("secrets.json"))))

    def test_keyless_defaults_to_local_and_records_no_master_key(self):
        self.run_cline()
        self.assertEqual(self.read_json(self.gpath("secrets.json"))["openAiApiKey"],
                         "local")
        self.assertNotIn("master_key", self.read_json(self.cline_json_path()))

    def test_a_named_profile_bakes_its_name_into_the_header(self):
        self.write_client_profile(name="laptop")
        self.run_cline()
        gs = self.read_json(self.gpath("globalState.json"))
        self.assertEqual(gs["openAiHeaders"].get("X-Ferry-Client"), "laptop")
        prov = self.read_json(self.gpath("settings", "providers.json"))
        self.assertEqual(
            prov["providers"]["openai-compatible"]["settings"]["headers"]
            .get("X-Ferry-Client"), "laptop")


class DeviceKeyTest(ClineHarness):
    """v1.39.0 per-device keys: the profile's api_key (an enrolled device key)
    outranks master_key, is recorded as api_key — never mislabeled the master —
    and the master it replaces is redacted from every cline snapshot, the same
    discipline claude.json's backups follow."""

    MASTER = "sk-cline-master-secret"
    DEVICE = "fk-cline-laptop-abcdefghijklmnopqrstuvwxyz234567"

    def test_the_profile_api_key_outranks_the_master_key(self):
        self.write_client_profile(master_key=self.MASTER, api_key=self.DEVICE)
        self.run_cline()
        self.assertEqual(self.read_json(self.gpath("secrets.json"))["openAiApiKey"],
                         self.DEVICE)
        rec = self.read_json(self.cline_json_path())
        self.assertEqual(rec["api_key"], self.DEVICE)
        self.assertNotIn("master_key", rec)

    def test_a_device_key_replacing_a_master_redacts_the_snapshots(self):
        # Run 1 wires cline with the master (files + future snapshot source).
        self.run_cline("--key", self.MASTER)
        self.assertEqual(self.read_json(self.gpath("secrets.json"))["openAiApiKey"],
                         self.MASTER)
        # Run 2 replaces it with a device key; run 1's files are snapshotted
        # BEFORE the rewrite, so the redaction pass is what keeps the master
        # out of them.
        p = self.run_cline("--key", self.DEVICE)
        self.assertIn("redacted", (p.stdout + p.stderr).lower())
        holders, redacted = [], []
        for root, _dirs, files in os.walk(self.home):
            for name in files:
                full = os.path.join(root, name)
                with open(full, "rb") as fh:
                    data = fh.read()
                rel = os.path.relpath(full, self.home)
                if self.MASTER.encode() in data:
                    holders.append(rel)
                if b"<redacted: replaced by ferry device key>" in data:
                    redacted.append(rel)
        self.assertEqual(holders, [],
                         "the master key survived the device-key swap in these files")
        self.assertTrue(any(r.endswith(".ferry.bak") for r in redacted), redacted)
        self.assertTrue(any(r.endswith(".bak") and "cline.json" in r
                            for r in redacted), redacted)
        # Live files carry the device key only.
        self.assertEqual(self.read_json(self.gpath("secrets.json"))["openAiApiKey"],
                         self.DEVICE)

    def test_no_redaction_when_there_is_no_master_to_replace(self):
        # Keyless run 1 ("local"), device-key run 2: nothing to redact, and the
        # run must not crash or invent a secret.
        self.run_cline()
        p = self.run_cline("--key", self.DEVICE)
        self.assertNotIn("redacted", (p.stdout + p.stderr).lower())
        self.assertEqual(self.read_json(self.gpath("secrets.json"))["openAiApiKey"],
                         self.DEVICE)


class FleetHeaderTest(ClineHarness):
    """X-Ferry-Fleet is a one-shot override: baked only when FERRY_FLEET is in
    the run's environment (fleets-design.md section 6, same rule as the
    claude wrappers' conditional)."""

    def test_fleet_header_is_baked_when_ferry_fleet_is_set(self):
        self.run_cline(env_extra={"FERRY_FLEET": "international"})
        gs = self.read_json(self.gpath("globalState.json"))
        self.assertEqual(gs["openAiHeaders"].get("X-Ferry-Fleet"), "international")
        prov = self.read_json(self.gpath("settings", "providers.json"))
        self.assertEqual(
            prov["providers"]["openai-compatible"]["settings"]["headers"]
            .get("X-Ferry-Fleet"), "international")

    def test_no_fleet_header_without_the_env(self):
        self.run_cline()   # harness scrubs FERRY_FLEET
        gs = self.read_json(self.gpath("globalState.json"))
        self.assertNotIn("X-Ferry-Fleet", gs["openAiHeaders"])
        prov = self.read_json(self.gpath("settings", "providers.json"))
        self.assertNotIn("X-Ferry-Fleet",
                         prov["providers"]["openai-compatible"]["settings"]["headers"])


class DataDirTest(ClineHarness):
    """--data-dir relocates the whole write; CLINE_DATA_DIR is the default
    default and the flag wins over it."""

    def test_cline_data_dir_env_locates_the_default_target(self):
        envdir = os.path.join(self.home, "envdata")
        self.run_cline(pass_data_dir=False, env_extra={"CLINE_DATA_DIR": envdir})
        self.assertTrue(
            os.path.exists(os.path.join(envdir, "data", "globalState.json")),
            "CLINE_DATA_DIR was not honoured as the default data dir")
        self.assertFalse(os.path.exists(os.path.join(self.data, "data")))

    def test_the_data_dir_flag_wins_over_the_env(self):
        flagdir = os.path.join(self.home, "flagdata")
        envdir = os.path.join(self.home, "envdata")
        self.run_cline(data=flagdir, env_extra={"CLINE_DATA_DIR": envdir})
        self.assertTrue(
            os.path.exists(os.path.join(flagdir, "data", "globalState.json")),
            "--data-dir lost to CLINE_DATA_DIR")
        self.assertFalse(os.path.exists(os.path.join(envdir, "data")))


class ScriptContractTest(unittest.TestCase):
    """Cheap static checks pinning the cross-seat wiring contracts — the
    style of ferry-claude.test.py's ScriptContractTest. A failure before the
    sibling seats land is the contract enforced loudly, not a bug here."""

    def read(self, rel):
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            self.fail(f"{rel} does not exist — the cline wiring has not landed yet")
        with open(path) as f:
            return f.read()

    def test_main_dispatches_the_cline_command(self):
        # Regex, not a literal: the dispatch table aligns its arms with
        # padding, so the spacing around `cline)` is not the contract.
        self.assertRegex(
            self.read("lib/ferry-main.zsh"),
            r'(?m)^\s*cline\)\s+cmd_cline\s+\"\$@\"\s*;;',
            "ferry-main.zsh has no cline dispatch arm")

    def test_usage_gains_a_cline_row(self):
        text = self.read("lib/ferry-usage.zsh")
        self.assertRegex(text, r"(?m)^\s*cline\s+",
                         "no cline row in the usage table")
        self.assertIn("Cline (VS Code)", text,
                      "the usage row must say what cline is")

    def test_build_assembles_the_cline_module_before_main(self):
        text = self.read("build.zsh")
        m = re.search(r"MODULES=\(([^)]*)\)", text)
        self.assertIsNotNone(m, "build.zsh MODULES array not found")
        mods = m.group(1).split()
        # build.zsh cats lib/ferry-$m.zsh, so the array holds STEMS ('cline'),
        # not filenames — accept either spelling; the contract is the ORDER.
        cline = next((i for i, n in enumerate(mods)
                      if n in ("cline", "ferry-cline.zsh")), None)
        main = next((i for i, n in enumerate(mods)
                     if n in ("main", "ferry-main.zsh")), None)
        self.assertIsNotNone(cline, "build.zsh MODULES is missing the cline module")
        self.assertIsNotNone(main, "build.zsh MODULES is missing ferry-main.zsh")
        self.assertLess(cline, main,
                        "the cline module must assemble before ferry-main.zsh "
                        "(dispatch last)")

    def test_the_module_carries_the_cline_markers(self):
        text = self.read("lib/ferry-cline.zsh")
        for marker in (".ferry.bak", "actModeApiProvider",
                       "openai-compatible", "openAiHeaders"):
            self.assertIn(marker, text, f"lib/ferry-cline.zsh lost {marker!r}")
        # The VS Code-open warning: files must be written while the extension
        # is not running, and the operator has to be told when it is.
        self.assertIn("pgrep", text)
        self.assertIn("Visual Studio Code", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
