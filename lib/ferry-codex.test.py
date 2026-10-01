#!/usr/bin/env python3
"""Stdlib unittest for the Codex CLI wiring (`ferry codex`).

Run:  python3 lib/ferry-codex.test.py

`ferry codex` is the OpenAI Codex twin of `ferry claude`: one marker-delimited
block in ~/.zshrc defining `codex-ferry()` (heavy), `codex-ferry-flash()`
(flash), each of which launches `codex`
with `-c` overrides that select a `ferry` model provider (Responses API) and
pass the key via the FERRY_CODEX_KEY env var. The default action also writes
~/.config/ferry/codex.json (0600). The user's ~/.codex is NEVER touched.

Runs the REAL `cmd_codex` against a throwaway $HOME — through the built
`ferry` monolith when it answers `ferry codex --help`, otherwise by sourcing
lib/ferry-core.zsh + lib/ferry-codex.zsh in a zsh subprocess. One end-to-end
test runs the REAL `codex` binary through an installed wrapper against a local
mock Responses server (skipped when codex is not installed).
"""
import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
CODEX_MODULE = os.path.join(REPO, "lib", "ferry-codex.zsh")

CANON_START = "# >>> ferry codex profiles >>>"
CANON_END = "# <<< ferry codex profiles <<<"

INSTALL_HOST = "testhost"
INSTALL_PORT = "8090"


def _monolith_supports_codex():
    if not os.path.exists(FERRY):
        return False
    try:
        p = subprocess.run(["zsh", FERRY, "codex", "--help"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "Unknown command" not in (p.stdout + p.stderr)


MONOLITH = _monolith_supports_codex()


class CodexHarness(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-codex-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.rc = os.path.join(self.home, ".zshrc")
        self.cfg_dir = os.path.join(self.home, ".config", "ferry")
        self.codex_json = os.path.join(self.cfg_dir, "codex.json")

    def env(self, path_prefix=None, **extra):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["TMPDIR"] = self.home
        e["PATH"] = ((path_prefix + os.pathsep) if path_prefix else "") + e.get("PATH", "")
        for k in ("FERRY_CODEX_KEY", "FERRY_FLEET", "CODEX_HOME"):
            e.pop(k, None)
        e.update(extra)
        return e

    def write_client_json(self, **fields):
        os.makedirs(self.cfg_dir, exist_ok=True)
        with open(os.path.join(self.cfg_dir, "client.json"), "w") as f:
            json.dump(fields, f)

    def run_install(self, *extra, host=INSTALL_HOST, port=INSTALL_PORT, with_host=True):
        args = (("--host", host, "--port", port) if with_host else ()) + extra
        if MONOLITH:
            cmd = [FERRY, "codex", *args]
        else:
            if not os.path.exists(CODEX_MODULE):
                self.fail("neither the built ferry monolith nor lib/ferry-codex.zsh exists")
            script = (f"source {REPO}/lib/ferry-core.zsh\n"
                      f"source {REPO}/lib/ferry-codex.zsh\n"
                      'cmd_codex "$@"\n')
            cmd = ["zsh", "-c", script, "ferry-codex", *args]
        return subprocess.run(cmd, capture_output=True, text=True,
                              env=self.env(), cwd=self.home)

    def rc_text(self):
        if not os.path.exists(self.rc):
            return ""
        with open(self.rc) as f:
            return f.read()

    def count(self, needle):
        return self.rc_text().count(needle)

    def codex_cfg(self):
        with open(self.codex_json) as f:
            return json.load(f)

    def make_stub(self, name="codex"):
        """A stub `codex` that prints its argv (one per line) and the env key."""
        bindir = os.path.join(self.home, "bin")
        os.makedirs(bindir, exist_ok=True)
        stub = os.path.join(bindir, name)
        with open(stub, "w") as f:
            f.write('#!/bin/sh\nfor a in "$@"; do echo "ARG:$a"; done\n'
                    'echo "ENVKEY:${FERRY_CODEX_KEY:-unset}"\n')
        os.chmod(stub, 0o755)
        return bindir


class WrapperInstallTest(CodexHarness):
    def test_fresh_install_writes_one_block_with_all_wrappers(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.rc_text()
        self.assertEqual(text.count(CANON_START), 1)
        self.assertEqual(text.count(CANON_END), 1)
        for fn in ("codex-ferry() {", "codex-ferry-flash() {",
                   "_codex_ferry_run() {"):
            self.assertEqual(text.count(fn), 1, fn)
        self.assertIn("_codex_ferry_run heavy", text)
        self.assertIn("_codex_ferry_run flash", text)
        self.assertNotIn("codex-ferry-local", text)
        self.assertNotIn("local-orch", text)
        # Shared -c list lives in ONE helper, not triplicated.
        self.assertEqual(text.count(f"http://{INSTALL_HOST}:{INSTALL_PORT}/v1"), 1)
        self.assertEqual(text.count('wire_api="responses"'), 1)
        self.assertNotIn("CODEX_HOME", text.replace("CODEX_HOME`", "").split(CANON_START)[1]
                         .split("# Host, port")[1],
                         "wrappers must not set CODEX_HOME")
        self.assertIsNone(re.search(r"^\s*codex\(\)", text, re.M),
                          "a bare codex() would shadow the user's own codex")

    def test_is_idempotent_and_result_parses(self):
        for _ in range(3):
            self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.count(CANON_START), 1)
        self.assertEqual(self.count(CANON_END), 1)
        self.assertEqual(self.count("codex-ferry-flash() {"), 1)
        r = subprocess.run(["zsh", "-n", self.rc], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"generated ~/.zshrc does not parse: {r.stderr}")

    def test_rerun_rebakes_host_and_preserves_other_content(self):
        with open(self.rc, "w") as f:
            f.write("export KEEP=me\n")
        self.run_install(host="oldhost")
        self.run_install(host="newhost")
        text = self.rc_text()
        self.assertIn("export KEEP=me", text)
        self.assertIn("http://newhost:8090/v1", text)
        self.assertNotIn("oldhost", text)

    def test_host_port_and_keyless_default_are_baked(self):
        self.run_install(host="gpu-box.local", port="9191")
        text = self.rc_text()
        self.assertIn("http://gpu-box.local:9191/v1", text)
        self.assertIn("FERRY_CODEX_KEY=local codex", text.replace("'local'", "local"))

    def test_wrappers_flag_writes_only_the_zshrc_block(self):
        r = self.run_install("--wrappers")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.count(CANON_START), 1)
        self.assertFalse(os.path.exists(self.codex_json),
                         "--wrappers must not write codex.json")

    def test_key_flag_and_precedence(self):
        self.write_client_json(host="h", port=8090, master_key="sk-master-1",
                               api_key="fk-device-1")
        self.assertEqual(self.run_install(with_host=False).returncode, 0)
        self.assertIn("FERRY_CODEX_KEY='fk-device-1' codex", self.rc_text())
        self.assertNotIn("sk-master-1", self.rc_text())
        cfg = self.codex_cfg()
        self.assertEqual(cfg["api_key"], "fk-device-1")
        self.assertEqual(cfg["key_source"], "api_key")
        self.assertNotIn("master_key", cfg)

    def test_master_key_used_when_no_device_key(self):
        self.write_client_json(host="h", port=8090, master_key="sk-master-1")
        self.assertEqual(self.run_install(with_host=False).returncode, 0)
        self.assertIn("FERRY_CODEX_KEY='sk-master-1' codex", self.rc_text())
        cfg = self.codex_cfg()
        self.assertEqual(cfg["master_key"], "sk-master-1")
        self.assertEqual(cfg["key_source"], "master_key")

    def test_explicit_key_flag_beats_client_json(self):
        self.write_client_json(host="h", port=8090, api_key="fk-device-1")
        self.assertEqual(self.run_install("--key", "fk-flagged", with_host=False).returncode, 0)
        self.assertIn("FERRY_CODEX_KEY='fk-flagged' codex", self.rc_text())
        self.assertNotIn("fk-device-1", self.rc_text())

    def test_client_json_supplies_host_and_port(self):
        self.write_client_json(host="lanbox.local", port=8123)
        self.assertEqual(self.run_install(with_host=False).returncode, 0)
        self.assertIn("http://lanbox.local:8123/v1", self.rc_text())

    def test_unknown_option_fails(self):
        r = self.run_install("--bogus")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Unknown option", r.stdout + r.stderr)


class CodexJsonTest(CodexHarness):
    def test_contents_for_a_keyless_setup(self):
        self.assertEqual(self.run_install().returncode, 0)
        cfg = self.codex_cfg()
        self.assertEqual(cfg["host"], INSTALL_HOST)
        self.assertEqual(cfg["port"], INSTALL_PORT)
        self.assertEqual(cfg["base_url"], f"http://{INSTALL_HOST}:{INSTALL_PORT}/v1")
        self.assertEqual(cfg["wire_api"], "responses")
        self.assertEqual(cfg["lanes"], {"codex-ferry": "heavy",
                                        "codex-ferry-flash": "flash"})
        for k in ("api_key", "master_key", "key_source"):
            self.assertNotIn(k, cfg, "'local' placeholder must not be mirrored")

    def test_key_is_recorded_and_file_is_0600(self):
        self.assertEqual(self.run_install("--key", "fk-abc").returncode, 0)
        cfg = self.codex_cfg()
        self.assertEqual(cfg["api_key"], "fk-abc")
        mode = stat.S_IMODE(os.stat(self.codex_json).st_mode)
        self.assertEqual(mode, 0o600)
        # Re-run over an existing, looser-mode file tightens it back.
        os.chmod(self.codex_json, 0o644)
        self.assertEqual(self.run_install("--key", "fk-abc").returncode, 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.codex_json).st_mode), 0o600)


class UserCodexHomeUntouchedTest(CodexHarness):
    def test_user_config_toml_is_byte_identical(self):
        d = os.path.join(self.home, ".codex")
        os.makedirs(d)
        cfgp = os.path.join(d, "config.toml")
        original = b'model = "gpt-x"\n[projects."/tmp/p"]\ntrust_level = "trusted"\n'
        with open(cfgp, "wb") as f:
            f.write(original)
        before = sorted(os.listdir(d))
        for extra in ((), ("--wrappers",)):
            self.assertEqual(self.run_install(*extra).returncode, 0)
        with open(cfgp, "rb") as f:
            self.assertEqual(f.read(), original)
        self.assertEqual(sorted(os.listdir(d)), before, "~/.codex gained files")


class WrapperBehaviorTest(CodexHarness):
    def run_fn(self, fn, *args, key="fk-secret-key", fleet=None):
        self.assertEqual(self.run_install("--key", key).returncode, 0)
        bindir = self.make_stub()
        extra = {"FERRY_FLEET": fleet} if fleet else {}
        r = subprocess.run(
            ["zsh", "-c", f"source {self.rc}; {fn} " + " ".join(args)],
            capture_output=True, text=True, env=self.env(path_prefix=bindir, **extra))
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = [l[4:] for l in r.stdout.splitlines() if l.startswith("ARG:")]
        envkey = [l[7:] for l in r.stdout.splitlines() if l.startswith("ENVKEY:")][0]
        return argv, envkey

    def test_each_wrapper_selects_its_lane_with_the_ferry_provider(self):
        for fn, lane in (("codex-ferry", "heavy"), ("codex-ferry-flash", "flash")):
            argv, _ = self.run_fn(fn)
            joined = "\n".join(argv)
            self.assertIn("model_provider=ferry", joined, fn)
            self.assertIn(f'model="{lane}"', joined, fn)
            self.assertIn(f'model_providers.ferry.base_url="http://{INSTALL_HOST}:{INSTALL_PORT}/v1"',
                          joined, fn)
            self.assertIn('model_providers.ferry.wire_api="responses"', joined, fn)
            self.assertIn('model_providers.ferry.env_key="FERRY_CODEX_KEY"', joined, fn)

    def test_wrappers_pass_the_ferry_routing_developer_instructions(self):
        """Codex's AGENTS.md names subagent models ferry does not serve
        (gpt-5.6-luna/-terra, gpt-6-astra...); the wrapper must tell it the
        lanes that exist, via the developer_instructions override."""
        for fn in ("codex-ferry", "codex-ferry-flash"):
            argv, _ = self.run_fn(fn)
            di = [a for a in argv if a.startswith("developer_instructions=")]
            self.assertEqual(len(di), 1, fn)
            for must in ("FERRY ROUTING", "tier names heavy, medium, light and super-light",
                         "pass the tier name itself as the spawn_agent model",
                         "never an OpenAI model id"):
                self.assertIn(must, di[0], f"{fn}: {must}")
            self.assertNotIn("gpt-", di[0], fn)
            # one argv element: the double-quoted TOML string survived the shell
            self.assertTrue(di[0].endswith('model id."'), di[0][-30:])

    def test_wrappers_pass_the_model_catalog_override(self):
        want = f"model_catalog_json=\"{self.home}/.config/ferry/codex-catalog.json\""
        for fn in ("codex-ferry", "codex-ferry-flash"):
            argv, _ = self.run_fn(fn)
            self.assertIn(want, argv, fn)

    def test_wrappers_run_at_high_reasoning_effort(self):
        for fn in ("codex-ferry", "codex-ferry-flash"):
            argv, _ = self.run_fn(fn)
            self.assertIn('model_reasoning_effort="high"', argv, fn)
            self.assertEqual(argv.index('model_reasoning_effort="high"') + 2,  # -c between
                             [i for i, a in enumerate(argv) if a.startswith("model_catalog_json=")][0], fn)

    def test_key_travels_by_env_never_argv(self):
        argv, envkey = self.run_fn("codex-ferry", "exec", "hello", key="fk-secret-key")
        self.assertEqual(envkey, "fk-secret-key")
        self.assertFalse(any("fk-secret-key" in a for a in argv),
                         f"key leaked into argv: {argv}")

    def test_user_args_come_last_so_they_can_override(self):
        argv, _ = self.run_fn("codex-ferry", "-m", "other-lane", "exec")
        self.assertEqual(argv[-3:], ["-m", "other-lane", "exec"])
        self.assertLess(argv.index('model="heavy"'), argv.index("-m"))

    def test_identity_headers_and_fleet(self):
        argv, _ = self.run_fn("codex-ferry")
        hdr = [a for a in argv if a.startswith("model_providers.ferry.http_headers=")][0]
        self.assertIn('"X-Ferry-Client"=', hdr)
        self.assertNotIn("X-Ferry-Fleet", hdr)
        argv, _ = self.run_fn("codex-ferry", fleet="f1")
        hdr = [a for a in argv if a.startswith("model_providers.ferry.http_headers=")][0]
        self.assertIn('"X-Ferry-Fleet"="f1"', hdr)

    def test_control_unsourced_shell_has_no_wrapper(self):
        self.assertEqual(self.run_install().returncode, 0)
        r = subprocess.run(["zsh", "-c", "whence -w codex-ferry"],
                           capture_output=True, text=True, env=self.env())
        self.assertIn("none", r.stdout)


class CatalogTest(CodexHarness):
    """~/.config/ferry/codex-catalog.json: Codex validates spawn_agent's model
    client-side against its catalog, so the tier names must be in one."""

    def catalog_path(self):
        return os.path.join(self.cfg_dir, "codex-catalog.json")

    def write_cache(self, models, codex_home=None):
        d = codex_home or os.path.join(self.home, ".codex")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "models_cache.json"), "w") as f:
            json.dump({"models": models}, f)

    @staticmethod
    def entry(slug, visibility="list", ctx=1000):
        return {"slug": slug, "display_name": slug, "visibility": visibility,
                "priority": 9, "context_window": ctx, "marker": f"from-{slug}",
                "supported_reasoning_levels": [{"effort": "low", "description": "x"}],
                "default_reasoning_level": "low", "shell_type": "shell_command",
                "supported_in_api": True, "support_verbosity": False,
                "truncation_policy": {"mode": "tokens", "limit": 1000},
                "experimental_supported_tools": [], "base_instructions": "x"}

    def slugs(self):
        with open(self.catalog_path()) as f:
            return [m["slug"] for m in json.load(f)["models"]]

    def models(self):
        with open(self.catalog_path()) as f:
            return {m["slug"]: m for m in json.load(f)["models"]}

    def test_four_tiers_in_order_from_the_newest_listed_models(self):
        self.write_cache([
            self.entry("gpt-6.9-sol"), self.entry("gpt-6.10-sol"),
            self.entry("gpt-6.1-sol"), self.entry("gpt-7-sol", "hide"),
            self.entry("gpt-5-astra"), self.entry("gpt-6-astra"),
            self.entry("gpt-6-luna"), self.entry("gpt-5.6-luna"),
            self.entry("gpt-reserve"), self.entry("codex-auto-review", "hide")])
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.slugs(), ["heavy", "medium", "light", "super-light"])
        m = self.models()
        self.assertEqual(m["heavy"]["marker"], "from-gpt-6-astra")
        self.assertEqual(m["medium"]["marker"], "from-gpt-6.10-sol")  # 6.10 > 6.9; hidden 7 ignored
        self.assertEqual(m["light"]["marker"], "from-gpt-6-luna")
        self.assertEqual(m["super-light"]["marker"], "from-gpt-6-luna")
        for i, slug in enumerate(("heavy", "medium", "light", "super-light"), 1):
            self.assertEqual(m[slug]["display_name"], slug)
            self.assertEqual(m[slug]["priority"], i)
            self.assertEqual(m[slug]["visibility"], "list")
            self.assertEqual(m[slug]["context_window"], 1000)  # real metadata copied
            self.assertEqual(m[slug]["default_reasoning_level"], "high")  # not the copied "low"
            self.assertEqual(m[slug]["multi_agent_reasoning_effort"], "high")

    def test_tiers_drop_openai_code_mode_tools(self):
        # gpt-6-astra's real entry sets code_mode_only; GLM behind heavy then
        # narrated the tool call as text instead of making it.
        e = self.entry("gpt-6-astra")
        e.update({"tool_mode": "code_mode_only", "use_responses_lite": True,
                  "apply_patch_tool_type": "freeform", "multi_agent_version": "v2"})
        self.write_cache([e])
        self.assertEqual(self.run_install().returncode, 0)
        heavy = self.models()["heavy"]
        for key in ("tool_mode", "use_responses_lite", "apply_patch_tool_type"):
            self.assertNotIn(key, heavy)
        self.assertEqual(heavy["multi_agent_version"], "v2")  # spawn_agent keeps working

    def test_cache_is_only_read_and_catalog_has_no_key(self):
        self.write_cache([self.entry("gpt-6-sol")])
        cache = os.path.join(self.home, ".codex", "models_cache.json")
        before = open(cache, "rb").read()
        self.assertEqual(self.run_install("--key", "fk-secret-key").returncode, 0)
        self.assertEqual(open(cache, "rb").read(), before)
        self.assertNotIn("fk-secret-key", open(self.catalog_path()).read())

    def test_codex_home_cache_is_honoured(self):
        alt = os.path.join(self.home, "alt-codex")
        self.write_cache([self.entry("gpt-6-sol")], codex_home=alt)
        if MONOLITH:
            cmd = [FERRY, "codex", "--host", INSTALL_HOST]
        else:
            cmd = ["zsh", "-c", f"source {REPO}/lib/ferry-core.zsh\n"
                   f"source {REPO}/lib/ferry-codex.zsh\ncmd_codex \"$@\"\n",
                   "x", "--host", INSTALL_HOST]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=self.env(CODEX_HOME=alt), cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.models()["medium"]["marker"], "from-gpt-6-sol")

    def test_fallback_when_cache_is_missing(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.slugs(), ["heavy", "medium", "light", "super-light"])
        for e in self.models().values():
            self.assertEqual(e["default_reasoning_level"], "high")
            self.assertEqual(e["multi_agent_reasoning_effort"], "high")
            for f in ("supported_reasoning_levels", "shell_type", "visibility",
                      "supported_in_api", "priority", "support_verbosity",
                      "truncation_policy", "experimental_supported_tools",
                      "base_instructions"):
                self.assertIn(f, e)

    def test_fallback_for_a_missing_family_and_a_corrupt_cache(self):
        self.write_cache([self.entry("gpt-6-sol")])  # no astra, no luna
        self.assertEqual(self.run_install().returncode, 0)
        m = self.models()
        self.assertEqual(m["medium"]["marker"], "from-gpt-6-sol")
        self.assertNotIn("marker", m["heavy"])
        self.assertNotIn("marker", m["light"])
        d = os.path.join(self.home, ".codex")
        with open(os.path.join(d, "models_cache.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.slugs(), ["heavy", "medium", "light", "super-light"])

    def test_wrappers_only_run_also_writes_the_catalog(self):
        self.assertEqual(self.run_install("--wrappers").returncode, 0)
        self.assertTrue(os.path.exists(self.catalog_path()))

    @unittest.skipUnless(shutil.which("codex"), "codex binary not installed")
    def test_real_codex_lists_exactly_the_four_tiers(self):
        for cache in (True, False):  # copied entries, then the minimal fallback
            if os.path.exists(self.catalog_path()):
                os.remove(self.catalog_path())
            if cache:
                self.write_cache([self.entry("gpt-6-sol")])
            else:
                shutil.rmtree(os.path.join(self.home, ".codex"), ignore_errors=True)
            self.assertEqual(self.run_install().returncode, 0)
            r = subprocess.run(
                ["codex", "-c", f'model_catalog_json="{self.catalog_path()}"',
                 "debug", "models"],
                capture_output=True, text=True, env=self.env(), cwd=self.home,
                stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual([m["slug"] for m in json.loads(r.stdout)["models"]],
                             ["heavy", "medium", "light", "super-light"], f"cache={cache}")


# --- end-to-end against the REAL codex binary ---------------------------------

class _Mock(http.server.BaseHTTPRequestHandler):
    records = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        type(self).records.append({"method": "POST", "path": self.path,
                                   "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": body.decode("utf8", "replace")})
        events = [
            ("response.created", {"type": "response.created",
                                  "response": {"id": "resp_1", "object": "response",
                                               "status": "in_progress"}}),
            ("response.output_item.done", {"type": "response.output_item.done",
                                           "item": {"type": "message", "role": "assistant",
                                                    "id": "msg_1",
                                                    "content": [{"type": "output_text",
                                                                 "text": "hi"}]}}),
            ("response.completed", {"type": "response.completed",
                                    "response": {"id": "resp_1", "object": "response",
                                                 "status": "completed",
                                                 "usage": {"input_tokens": 1,
                                                           "input_tokens_details": None,
                                                           "output_tokens": 1,
                                                           "output_tokens_details": None,
                                                           "total_tokens": 2}}}),
        ]
        out = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        type(self).records.append({"method": "GET", "path": self.path,
                                   "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": ""})
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@unittest.skipUnless(shutil.which("codex"), "codex binary not installed - skipping e2e test")
class CodexEndToEndTest(CodexHarness):
    def test_real_codex_through_the_wrapper_hits_responses_with_key_and_headers(self):
        _Mock.records = []
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Mock)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)

        key = "fk-e2e-secret"
        self.assertEqual(
            self.run_install("--key", key, host="127.0.0.1", port=str(port)).returncode, 0)
        codex_home = os.path.join(self.home, "codex-home")
        os.makedirs(codex_home)
        cwd = os.path.join(self.home, "work")
        os.makedirs(cwd)
        env = self.env(CODEX_HOME=codex_home, FERRY_FLEET="e2e-fleet")
        try:
            r = subprocess.run(
                ["zsh", "-c",
                 f"source {self.rc}; codex-ferry exec --skip-git-repo-check 'say hi' </dev/null"],
                capture_output=True, text=True, env=env, cwd=cwd, timeout=90)
        except subprocess.TimeoutExpired:
            self.fail("codex exec did not finish within 90s")
        posts = [x for x in _Mock.records if x["method"] == "POST"]
        self.assertTrue(posts, f"mock saw no POST; rc={r.returncode}\n{r.stdout}\n{r.stderr}")
        first = posts[0]
        self.assertEqual(first["path"], "/v1/responses")
        self.assertEqual(first["headers"].get("authorization"), f"Bearer {key}")
        self.assertTrue(first["headers"].get("x-ferry-client"))
        self.assertEqual(first["headers"].get("x-ferry-fleet"), "e2e-fleet")
        self.assertEqual(json.loads(first["body"]).get("model"), "heavy")
        # Bounded: a completed response must not trigger the retry storm.
        self.assertLessEqual(len(posts), 2, f"codex retried: {len(posts)} POSTs")
        # The wrapper never wrote into the (test-scoped) real config dir's config.toml.
        self.assertFalse(os.path.exists(os.path.join(codex_home, "config.toml")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
