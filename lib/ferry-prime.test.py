#!/usr/bin/env python3
"""Stdlib unittest for `ferry prime` — the Prime Agent takeover.

Run:  python3 lib/ferry-prime.test.py

`ferry prime` adds a `ferry` custom provider to Prime Agent's models.json
(PrimeIntellect-ai/prime-agent: <agent dir>/models.json, default
~/.prime/agent, relocated by PRIME_AGENT_CODING_AGENT_DIR) and records
~/.config/ferry/prime.json. Facts pinned here come from the prime-agent
source (crates/pa-core/src/models/custom.rs, crates/pa-ai/src/providers/
openai_completions/stream.rs):

  * models.json is {"providers": {"<id>": {...}}}, JSONC (comments and
    trailing commas tolerated);
  * baseUrl is the .../v1 root — the client appends /chat/completions;
  * a custom provider needs baseUrl + apiKey + api; models need only `id`.

No prime-agent binary exists on this machine (and the task forbids
installing it), so there is NO end-to-end check: these tests prove the file
the command writes, not that prime-agent accepts it.

Runs the REAL `cmd_prime` against a throwaway $HOME and a stub /v1/models
catalogue, through the built `ferry` monolith when it answers
`ferry prime --help`, otherwise by sourcing lib/ferry-core.zsh +
lib/ferry-prime.zsh in a zsh subprocess (the pattern of ferry-cline.test.py).
"""
import glob
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
PRIME_MODULE = os.path.join(REPO, "lib", "ferry-prime.zsh")

CATALOGUE = ["heavy", "medium", "flash"]


def _monolith_supports_prime():
    """True when the built `ferry` monolith knows `prime` ("Unknown command"
    can arrive with returncode 0, so the string check decides)."""
    if not os.path.exists(FERRY):
        return False
    try:
        p = subprocess.run(["zsh", FERRY, "prime", "--help"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "Unknown command" not in (p.stdout + p.stderr)


MONOLITH = _monolith_supports_prime()


class _ModelsHandler(BaseHTTPRequestHandler):
    seen_auth = []

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path != "/v1/models":
            self.send_error(404)
            return
        type(self).seen_auth.append(self.headers.get("Authorization"))
        body = json.dumps({"data": [{"id": m} for m in CATALOGUE]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class PrimeHarness(unittest.TestCase):
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
        self.home = tempfile.mkdtemp(prefix="ferry-prime-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.agent = os.path.join(self.home, ".prime", "agent")
        self.models = os.path.join(self.agent, "models.json")

    def env(self, extra=None):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["TMPDIR"] = self.home
        for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
                  "CLIENT_MASTER_KEY", "FERRY_MASTER_KEY", "FERRY_FLEET",
                  "PRIME_AGENT_CODING_AGENT_DIR"):
            e.pop(k, None)
        if extra:
            e.update(extra)
        return e

    def run_prime(self, *extra, port=None, env_extra=None, expect_rc=0,
                  host_args=True):
        args = []
        if host_args:
            args += ["--host", "127.0.0.1",
                     "--port", str(port if port is not None else self.port)]
        args += list(extra)
        if MONOLITH:
            cmd = ["zsh", FERRY, "prime", *args]
        else:
            if not os.path.exists(PRIME_MODULE):
                self.fail("neither the built monolith nor lib/ferry-prime.zsh exists")
            script = (f"source {REPO}/lib/ferry-core.zsh\n"
                      f"source {REPO}/lib/ferry-prime.zsh\n"
                      'cmd_prime "$@"\n')
            cmd = ["zsh", "-c", script, "ferry-prime", *args]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           env=self.env(env_extra), cwd=REPO, timeout=180)
        self.assertEqual(p.returncode, expect_rc,
                         f"ferry prime rc != {expect_rc}:\n{p.stdout}\n{p.stderr}")
        return p

    def read_json(self, path):
        with open(path) as f:
            return json.load(f)

    def read_text(self, path):
        with open(path) as f:
            return f.read()

    def write_models(self, text, path=None):
        path = path or self.models
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def record_path(self):
        return os.path.join(self.home, ".config", "ferry", "prime.json")

    def write_client_profile(self, **kw):
        fdir = os.path.join(self.home, ".config", "ferry")
        os.makedirs(fdir, exist_ok=True)
        prof = {"host": "127.0.0.1", "port": str(self.port)}
        prof.update(kw)
        with open(os.path.join(fdir, "client.json"), "w") as f:
            json.dump(prof, f)

    def baks(self, path):
        return sorted(glob.glob(path + ".*.ferry.bak"))

    def ferry_provider(self, path=None):
        return self.read_json(path or self.models)["providers"]["ferry"]


class FreshInstallTest(PrimeHarness):
    def setUp(self):
        super().setUp()
        self.run_prime()

    def test_models_json_is_wrapped_in_providers_with_the_ferry_provider(self):
        doc = self.read_json(self.models)
        self.assertEqual(list(doc), ["providers"])
        self.assertEqual(list(doc["providers"]), ["ferry"])

    def test_the_provider_has_the_validated_custom_provider_shape(self):
        p = self.ferry_provider()
        # custom.rs validate_config: baseUrl + apiKey + api are all required.
        self.assertEqual(p["baseUrl"], f"http://127.0.0.1:{self.port}/v1")
        self.assertEqual(p["apiKey"], "local")
        self.assertEqual(p["api"], "openai-completions")
        # registry.rs: authHeader true => Authorization: Bearer <apiKey>.
        self.assertIs(p["authHeader"], True)
        self.assertIn("X-Ferry-Client", p["headers"])
        self.assertNotIn("X-Ferry-Fleet", p["headers"])
        self.assertEqual(p["compat"]["maxTokensField"], "max_tokens")
        self.assertIs(p["compat"]["supportsStore"], False)
        self.assertIs(p["compat"]["supportsDeveloperRole"], False)

    def test_the_base_url_does_not_double_the_chat_completions_path(self):
        base = self.ferry_provider()["baseUrl"]
        self.assertTrue(base.endswith("/v1"), base)
        self.assertNotIn("chat/completions", base)

    def test_models_are_the_served_lanes_with_ids_and_names(self):
        models = self.ferry_provider()["models"]
        self.assertEqual([m["id"] for m in models], ["heavy", "medium", "flash"])
        for m in models:
            self.assertTrue(m["name"])

    def test_file_mode_is_0600(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.models).st_mode), 0o600)

    def test_the_record_is_written(self):
        rec = self.read_json(self.record_path())
        self.assertEqual(rec["host"], "127.0.0.1")
        self.assertEqual(rec["port"], str(self.port))
        self.assertEqual(rec["model"], "heavy")
        self.assertEqual(rec["agent_dir"], self.agent)
        self.assertEqual(rec["models_path"], self.models)
        self.assertEqual(rec["provider"], "ferry")
        self.assertNotIn("master_key", rec)        # 'local' is never recorded
        self.assertNotIn("api_key", rec)

    def test_no_snapshot_for_a_file_that_did_not_exist(self):
        self.assertEqual(self.baks(self.models), [])


class LaneListTest(PrimeHarness):
    def test_unreachable_host_lists_the_fixed_lane_set(self):
        # port 1: connection refused fast; wiring must still succeed.
        p = self.run_prime(port=1)
        ids = [m["id"] for m in self.ferry_provider()["models"]]
        self.assertEqual(ids, ["heavy", "flash", "super-flash",
                               "local-orch", "local-sub"])
        self.assertIn("WARNING", p.stderr)

    def test_the_chosen_lane_is_listed_first(self):
        self.run_prime("--model", "flash")
        ids = [m["id"] for m in self.ferry_provider()["models"]]
        self.assertEqual(ids, ["flash", "heavy", "medium"])
        self.assertEqual(self.read_json(self.record_path())["model"], "flash")

    def test_an_unserved_lane_falls_back_to_heavy(self):
        p = self.run_prime("--model", "nope")
        self.assertIn("does not serve lane 'nope'", p.stderr)
        self.assertEqual(self.read_json(self.record_path())["model"], "heavy")

    def test_the_catalogue_probe_carries_the_bearer(self):
        _ModelsHandler.seen_auth.clear()
        self.run_prime("--key", "sekret")
        self.assertIn("Bearer sekret", _ModelsHandler.seen_auth)


class MergeTest(PrimeHarness):
    OTHER = {"ollama": {"baseUrl": "http://localhost:11434", "apiKey": "none",
                        "api": "openai-completions",
                        "models": [{"id": "llama3", "name": "Llama 3"}]}}

    def test_an_existing_provider_survives_unchanged(self):
        self.write_models(json.dumps({"providers": self.OTHER,
                                      "somethingElse": {"keep": [1, 2]}},
                                     indent=2))
        self.run_prime()
        doc = self.read_json(self.models)
        self.assertEqual(doc["providers"]["ollama"], self.OTHER["ollama"])
        self.assertEqual(doc["somethingElse"], {"keep": [1, 2]})
        self.assertIn("ferry", doc["providers"])

    def test_the_pre_existing_file_is_snapshotted_byte_for_byte(self):
        original = json.dumps({"providers": self.OTHER}, indent=2) + "\n"
        self.write_models(original)
        self.run_prime()
        baks = self.baks(self.models)
        self.assertEqual(len(baks), 1)
        self.assertEqual(self.read_text(baks[0]), original)

    def test_a_file_without_providers_gains_it(self):
        self.write_models('{\n  "other": 1\n}\n')
        self.run_prime()
        doc = self.read_json(self.models)
        self.assertEqual(doc["other"], 1)
        self.assertIn("ferry", doc["providers"])

    def test_an_empty_providers_object_gains_the_provider(self):
        self.write_models('{"providers": {}}')
        self.run_prime()
        self.assertEqual(list(self.read_json(self.models)["providers"]), ["ferry"])

    def test_an_empty_file_is_treated_as_fresh(self):
        self.write_models("")
        self.run_prime()
        self.assertEqual(list(self.read_json(self.models)["providers"]), ["ferry"])

    def test_a_stale_ferry_provider_is_replaced_not_duplicated(self):
        self.write_models(json.dumps({"providers": {
            "ferry": {"baseUrl": "http://old:1/v1", "apiKey": "x",
                      "api": "openai-completions", "models": [{"id": "old"}]},
            **self.OTHER}}, indent=2))
        self.run_prime()
        doc = self.read_json(self.models)
        self.assertEqual(doc["providers"]["ferry"]["baseUrl"],
                         f"http://127.0.0.1:{self.port}/v1")
        self.assertEqual(doc["providers"]["ollama"], self.OTHER["ollama"])
        self.assertEqual(self.read_text(self.models).count('"ferry": {'), 1)

    def test_a_loose_existing_mode_is_tightened(self):
        self.write_models('{"providers": {}}')
        os.chmod(self.models, 0o644)
        self.run_prime()
        self.assertEqual(stat.S_IMODE(os.stat(self.models).st_mode), 0o600)


class IdempotenceTest(PrimeHarness):
    def test_a_second_run_changes_no_bytes_and_makes_no_new_snapshot(self):
        self.write_models(json.dumps({"providers": MergeTest.OTHER}, indent=2))
        self.run_prime()
        first = self.read_text(self.models)
        baks = self.baks(self.models)
        self.assertEqual(len(baks), 1)
        p = self.run_prime()
        self.assertEqual(self.read_text(self.models), first)
        self.assertEqual(self.baks(self.models), baks)
        self.assertIn("unchanged", p.stdout)

    def test_a_fresh_install_rerun_is_stable_and_creates_no_bak(self):
        self.run_prime()
        first = self.read_text(self.models)
        self.run_prime()
        self.assertEqual(self.read_text(self.models), first)
        self.assertEqual(self.baks(self.models), [])

    def test_a_changed_endpoint_snapshots_the_previous_file_once(self):
        self.run_prime()
        self.run_prime("--key", "k2")
        self.assertEqual(len(self.baks(self.models)), 1)
        self.assertEqual(self.ferry_provider()["apiKey"], "k2")

    def test_keep_prunes_old_snapshots(self):
        self.run_prime()
        for i in range(4):
            self.run_prime("--key", f"k{i}", "--keep", "2")
        self.assertEqual(len(self.baks(self.models)), 2)

    def test_the_record_gets_a_plain_bak_on_rerun(self):
        self.run_prime()
        self.run_prime()
        baks = glob.glob(self.record_path() + ".*.bak")
        self.assertEqual(len(baks), 1)


class JsoncTest(PrimeHarness):
    """The documented rule: comments and trailing commas are PRESERVED —
    the edit is text-surgical, never a JSON round trip — and a file that
    cannot be edited safely is left untouched with a hand-edit snippet."""

    COMMENTED = (
        '// my models\n'
        '{\n'
        '  /* providers block */\n'
        '  "providers": {\n'
        '    // local llama\n'
        '    "ollama": {\n'
        '      "baseUrl": "http://localhost:11434", // port\n'
        '      "apiKey": "none",\n'
        '      "api": "openai-completions",\n'
        '      "models": [{"id": "llama3"},],\n'
        '    }, // end ollama\n'
        '  },\n'
        '}\n'
    )

    def test_comments_and_trailing_commas_survive_the_insert(self):
        self.write_models(self.COMMENTED)
        self.run_prime()
        text = self.read_text(self.models)
        for frag in ("// my models", "/* providers block */", "// local llama",
                     "// port", "// end ollama", '"models": [{"id": "llama3"},],'):
            self.assertIn(frag, text)
        # The original text is a verbatim prefix-and-suffix of the result:
        # only the ferry member was added.
        head = self.COMMENTED[:self.COMMENTED.index("    }, // end ollama")]
        self.assertTrue(text.startswith(head))
        # And the result still parses (strip comments/commas) with both
        # providers present.
        stripped = re.sub(r"(?m)(^|\s)//[^\n]*", " ", text)  # not the // in http://
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        doc = json.loads(stripped)
        self.assertEqual(sorted(doc["providers"]), ["ferry", "ollama"])
        self.assertEqual(doc["providers"]["ferry"]["api"], "openai-completions")

    def test_the_jsonc_rerun_is_idempotent(self):
        self.write_models(self.COMMENTED)
        self.run_prime()
        first = self.read_text(self.models)
        self.run_prime()
        self.assertEqual(self.read_text(self.models), first)
        self.assertEqual(len(self.baks(self.models)), 1)

    def test_a_comment_after_the_last_member_stays_a_comment(self):
        self.write_models('{\n  "providers": {\n    "a": {"baseUrl": "u", "apiKey": "k", "api": "x"} // last\n  }\n}\n')
        self.run_prime()
        text = self.read_text(self.models)
        self.assertIn('"api": "x"}, // last', text)
        self.assertIn('"ferry"', text)

    def test_a_string_that_looks_like_a_comment_is_not_mistaken_for_one(self):
        self.write_models('{"providers": {"a": {"baseUrl": "http://h/v1", '
                          '"apiKey": "k//x", "api": "x"}}}')
        self.run_prime()
        doc = self.read_json(self.models)
        self.assertEqual(doc["providers"]["a"]["apiKey"], "k//x")
        self.assertIn("ferry", doc["providers"])

    def test_an_unparseable_file_is_not_touched_and_the_snippet_is_printed(self):
        garbage = '{"providers": {"a": '
        self.write_models(garbage)
        p = self.run_prime(expect_rc=1)
        self.assertEqual(self.read_text(self.models), garbage)
        self.assertIn("was NOT modified", p.stderr)
        self.assertIn('"ferry"', p.stderr)
        self.assertIn('"baseUrl"', p.stderr)
        self.assertEqual(self.baks(self.models), [])
        self.assertFalse(os.path.exists(self.record_path()))

    def test_a_non_object_providers_value_is_not_touched(self):
        txt = '{"providers": []}'
        self.write_models(txt)
        p = self.run_prime(expect_rc=1)
        self.assertEqual(self.read_text(self.models), txt)
        self.assertIn("NOT modified", p.stderr)

    def test_a_non_object_top_level_is_not_touched(self):
        self.write_models("[1, 2]")
        self.run_prime(expect_rc=1)
        self.assertEqual(self.read_text(self.models), "[1, 2]")


class KeyTest(PrimeHarness):
    def test_a_profile_master_key_reaches_the_file_and_the_record(self):
        self.write_client_profile(master_key="MASTER")
        self.run_prime(host_args=False)
        self.assertEqual(self.ferry_provider()["apiKey"], "MASTER")
        self.assertEqual(self.read_json(self.record_path())["master_key"], "MASTER")

    def test_the_device_api_key_outranks_the_master_key(self):
        self.write_client_profile(master_key="MASTER", api_key="fk-dev1")
        self.run_prime(host_args=False)
        self.assertEqual(self.ferry_provider()["apiKey"], "fk-dev1")
        rec = self.read_json(self.record_path())
        self.assertEqual(rec["api_key"], "fk-dev1")
        self.assertNotIn("master_key", rec)

    def test_the_key_flag_beats_the_profile(self):
        self.write_client_profile(api_key="fk-dev1")
        self.run_prime("--key", "explicit", host_args=False)
        self.assertEqual(self.ferry_provider()["apiKey"], "explicit")

    def test_a_device_key_replacing_a_master_redacts_the_snapshots(self):
        self.write_client_profile(master_key="MASTER-SECRET")
        self.run_prime(host_args=False)
        self.write_client_profile(api_key="fk-dev2", master_key="MASTER-SECRET")
        self.run_prime(host_args=False)
        self.assertEqual(self.ferry_provider()["apiKey"], "fk-dev2")
        snaps = self.baks(self.models) + glob.glob(self.record_path() + ".*.bak")
        self.assertTrue(snaps)
        for s in snaps:
            self.assertNotIn("MASTER-SECRET", self.read_text(s), s)
        self.assertIn("redacted", " ".join(self.read_text(s) for s in snaps))

    def test_a_named_profile_bakes_its_name_into_the_header(self):
        self.write_client_profile(name="laptop-7")
        self.run_prime(host_args=False)
        self.assertEqual(self.ferry_provider()["headers"]["X-Ferry-Client"],
                         "laptop-7")

    def test_the_fleet_header_is_baked_when_ferry_fleet_is_set(self):
        self.run_prime(env_extra={"FERRY_FLEET": "alpha"})
        self.assertEqual(self.ferry_provider()["headers"]["X-Ferry-Fleet"], "alpha")

    def test_the_key_never_reaches_stdout(self):
        p = self.run_prime("--key", "TOP-SECRET-KEY")
        self.assertNotIn("TOP-SECRET-KEY", p.stdout + p.stderr)


class AgentDirTest(PrimeHarness):
    def test_the_env_override_locates_models_json(self):
        alt = os.path.join(self.home, "alt-agent")
        self.run_prime(env_extra={"PRIME_AGENT_CODING_AGENT_DIR": alt})
        self.assertTrue(os.path.exists(os.path.join(alt, "models.json")))
        self.assertFalse(os.path.exists(self.models))
        self.assertEqual(self.read_json(self.record_path())["agent_dir"], alt)

    def test_the_flag_wins_over_the_env(self):
        env_dir = os.path.join(self.home, "env-agent")
        flag_dir = os.path.join(self.home, "flag-agent")
        self.run_prime("--agent-dir", flag_dir,
                       env_extra={"PRIME_AGENT_CODING_AGENT_DIR": env_dir})
        self.assertTrue(os.path.exists(os.path.join(flag_dir, "models.json")))
        self.assertFalse(os.path.exists(env_dir))


def _path_without_prime_agent():
    # The inherited PATH minus every dir holding a prime-agent, so the test
    # sees "not installed" even on a machine where it really is installed.
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not os.access(os.path.join(d, "prime-agent"), os.X_OK)]
    return os.pathsep.join(keep)


class NoPrimeAgentTest(PrimeHarness):
    def test_a_missing_binary_is_a_note_not_an_error(self):
        p = self.run_prime(env_extra={"PATH": _path_without_prime_agent()})
        self.assertIn("isn't on this machine yet", p.stdout)
        self.assertIn("install.sh", p.stdout)
        self.assertTrue(os.path.exists(self.models))

    def test_control_an_installed_binary_prints_no_note(self):
        # CONTROL: a prime-agent on PATH must suppress the note, or the test
        # above passes no matter what the machine has installed.
        bindir = os.path.join(self.home, "fakebin")
        os.makedirs(bindir, exist_ok=True)
        fake = os.path.join(bindir, "prime-agent")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        path = bindir + os.pathsep + _path_without_prime_agent()
        p = self.run_prime(env_extra={"PATH": path})
        self.assertNotIn("isn't on this machine yet", p.stdout)
        self.assertTrue(os.path.exists(self.models))


class ScriptContractTest(unittest.TestCase):
    def read(self, rel):
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            self.fail(f"{rel} does not exist")
        with open(path) as f:
            return f.read()

    def test_main_dispatches_the_prime_command(self):
        self.assertRegex(self.read("lib/ferry-main.zsh"),
                         r'(?m)^\s*prime\)\s+cmd_prime\s+"\$@"\s*;;')

    def test_usage_gains_a_prime_row(self):
        text = self.read("lib/ferry-usage.zsh")
        self.assertRegex(text, r"(?m)^\s*prime\s+")
        self.assertIn("Prime Agent", text)

    def test_build_assembles_the_prime_module_before_main(self):
        m = re.search(r"MODULES=\(([^)]*)\)", self.read("build.zsh"))
        self.assertIsNotNone(m)
        mods = m.group(1).split()
        self.assertIn("prime", mods)
        self.assertLess(mods.index("prime"), mods.index("main"))

    def test_the_built_monolith_is_in_sync(self):
        p = subprocess.run(["zsh", os.path.join(REPO, "build.zsh"), "--check"],
                           capture_output=True, text=True, cwd=REPO, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_the_module_carries_the_prime_markers(self):
        text = self.read("lib/ferry-prime.zsh")
        for marker in (".ferry.bak", "openai-completions", "PRIME_AGENT_CODING_AGENT_DIR",
                       "authHeader", "prime.json", "providers"):
            self.assertIn(marker, text, f"lib/ferry-prime.zsh lost {marker!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
