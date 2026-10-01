#!/usr/bin/env python3
"""Stdlib unittest for the Codex CLI wiring (`ferry codex`).

Run:  python3 lib/ferry-codex.test.py

`ferry codex` is the OpenAI Codex twin of `ferry claude`: one marker-delimited
block in ~/.zshrc defining `codex-ferry()` (heavy), `codex-ferry-flash()`
(flash) and `codex-ferry-local()` (local-orch), each of which launches `codex`
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
        for fn in ("codex-ferry() {", "codex-ferry-flash() {", "codex-ferry-local() {",
                   "_codex_ferry_run() {"):
            self.assertEqual(text.count(fn), 1, fn)
        self.assertIn("_codex_ferry_run heavy", text)
        self.assertIn("_codex_ferry_run flash", text)
        self.assertIn("_codex_ferry_run local-orch", text)
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
        self.assertEqual(self.count("codex-ferry-local() {"), 1)
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
                                        "codex-ferry-flash": "flash",
                                        "codex-ferry-local": "local-orch"})
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
        for fn, lane in (("codex-ferry", "heavy"), ("codex-ferry-flash", "flash"),
                         ("codex-ferry-local", "local-orch")):
            argv, _ = self.run_fn(fn)
            joined = "\n".join(argv)
            self.assertIn("model_provider=ferry", joined, fn)
            self.assertIn(f'model="{lane}"', joined, fn)
            self.assertIn(f'model_providers.ferry.base_url="http://{INSTALL_HOST}:{INSTALL_PORT}/v1"',
                          joined, fn)
            self.assertIn('model_providers.ferry.wire_api="responses"', joined, fn)
            self.assertIn('model_providers.ferry.env_key="FERRY_CODEX_KEY"', joined, fn)

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
