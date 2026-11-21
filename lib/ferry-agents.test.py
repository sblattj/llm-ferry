#!/usr/bin/env python3
"""Stdlib unittest for issue #3: every popular agentic coding CLI gets a
`ferry <client>` command, and the client scripts know about each one.

Run:  python3 lib/ferry-agents.test.py

The failure case from the issue: on v1.51.0 `ferry pi`, `ferry gemini`,
`ferry aider`, ... print "Unknown command" (with exit 0, so the string decides),
client-bootstrap.sh rejects `--no-<client>`, and nothing records a
`<client>_mode` for client-reset.sh / client-cleanup.sh to act on.

Per-client behavior (what each command writes, unwire, e2e against a stub)
lives in lib/ferry-<client>.test.py. This file pins the registry contract that
ties them together:

  * every To-do client in the issue answers `ferry <client> --help`;
  * `ferry agents` lists the registry, and every name in it is a command;
  * client-bootstrap.sh accepts `--no-<client>` for each and records
    `<client>_mode` (full / none) in client.json;
  * client-reset.sh and client-cleanup.sh drive the registry, not a hand list.
"""
import json
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

# The issue's "To do" list, by the command name each one ships under.
# Cline CLI is covered by the existing `ferry cline` (shared ~/.cline state),
# so it is pinned by ferry-cline.test.py, not here.
ISSUE_CLIENTS = [
    "pi", "gemini", "openhands", "interpreter", "goose", "aider",
    "codewhale", "reasonix", "omp", "crush", "qwen", "kilo", "kimi", "copilot",
]


def _rd(path):
    with open(path) as f:
        return f.read()


def ferry(*args, home=None, timeout=60):
    env = dict(os.environ)
    if home:
        env["HOME"] = home
    p = subprocess.run(["zsh", FERRY, *args], capture_output=True, text=True,
                       timeout=timeout, env=env)
    return p.returncode, p.stdout + p.stderr


class EveryClientHasACommand(unittest.TestCase):
    def test_help_answers_for_every_issue_client(self):
        with tempfile.TemporaryDirectory() as home:
            for c in ISSUE_CLIENTS:
                with self.subTest(client=c):
                    rc, out = ferry(c, "--help", home=home)
                    self.assertTrue("Unknown command" not in out,
                                    f"ferry {c} is not a command")
                    self.assertEqual(rc, 0, f"ferry {c} --help exit {rc}")
                    self.assertTrue(f"ferry {c}" in out, f"ferry {c} help text")

    def test_registry_lists_every_issue_client(self):
        with tempfile.TemporaryDirectory() as home:
            rc, out = ferry("agents", "--list", home=home)
            self.assertTrue(rc == 0 and "Unknown command" not in out,
                            "ferry agents is not a command")
            names = out.split()
            for c in ISSUE_CLIENTS:
                with self.subTest(client=c):
                    self.assertTrue(c in names, f"ferry agents --list lacks {c}")


class ClientScriptsDriveTheRegistry(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(REPO, name)) as f:
            return f.read()

    def test_bootstrap_has_a_no_flag_and_mode_for_every_client(self):
        src = self.read("client-bootstrap.sh")
        help_out = subprocess.run(
            ["zsh", os.path.join(REPO, "client-bootstrap.sh"), "--help"],
            capture_output=True, text=True, timeout=30).stdout
        for c in ISSUE_CLIENTS:
            with self.subTest(client=c):
                self.assertTrue(f"--no-{c}" in help_out,
                                f"client-bootstrap.sh --help has no --no-{c}")
        self.assertTrue("ferry agents" in src or "agents --list" in src,
                        "client-bootstrap.sh does not read the agents registry")

    def test_bootstrap_copy_of_the_registry_matches_the_cli(self):
        import re as _re
        src = self.read("client-bootstrap.sh")
        m = _re.search(r"^AGENT_CLIENTS=\(([^)]*)\)", src, _re.M)
        self.assertTrue(m, "client-bootstrap.sh has no AGENT_CLIENTS=(...)")
        mod = self.read("lib/ferry-agents.zsh")
        reg = _re.search(r"^FERRY_AGENT_CLIENTS=\(([^)]*)\)", mod, _re.M | _re.S)
        names = [e.split(":")[0] for e in reg.group(1).split()]
        self.assertEqual(m.group(1).split(), names)
        self.assertEqual(sorted(names), sorted(ISSUE_CLIENTS))

    def test_reset_and_cleanup_use_the_registry(self):
        for script in ("client-reset.sh", "client-cleanup.sh"):
            with self.subTest(script=script):
                self.assertTrue("agents --list" in self.read(script),
                                f"{script} does not read the agents registry")


DEMO_MODULE = r"""
_demo_help() { echo "ferry demo - test fixture"; }
_demo_wire() {
  _agent_python "$HOME/.demo/settings.json" <<'PYEOF'
import sys
wrapper(os.path.join(HOME, ".local", "bin", "demo-ferry"),
        {"DEMO_BASE_URL": BASE, "DEMO_API_KEY": '"$FERRY_KEY"'}, "demo",
        ["--model", LANE])
json_member_set(sys.argv[1], ["providers", "ferry"],
                {"baseUrl": BASE, "headers": HEADERS, "model": LANE})
save_record()
PYEOF
}
cmd_demo() { _agent_main demo "$@"; }
"""


def run_demo(home, *args):
    script = (f"source {REPO}/lib/ferry-core.zsh\n"
              f"source {REPO}/lib/ferry-agents.zsh\n"
              f"{DEMO_MODULE}\n"
              'FERRY_AGENT_CLIENTS+=(demo:demo)\ncmd_demo "$@"\n')
    env = dict(os.environ, HOME=home, PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin")
    env.pop("FERRY_FLEET", None)
    p = subprocess.run(["zsh", "-c", script, "ferry-demo", *args],
                       capture_output=True, text=True, timeout=60, env=env)
    return p.returncode, p.stdout + p.stderr


class HelperContract(unittest.TestCase):
    """The shared _agent_main / FERRY_AGENT_PY contract every client uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, ".demo"))
        self.settings = os.path.join(self.home, ".demo", "settings.json")
        with open(self.settings, "w") as f:
            json.dump({"theme": "dark", "providers": {"mine": {"x": 1}}}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def wire(self, *extra):
        # Port 9 (discard) is never a ferry: the lane check must not be fatal.
        rc, out = run_demo(self.home, "--host", "127.0.0.1", "--port", "9",
                           "--key", "sk-demo-secret", *extra)
        self.assertEqual(rc, 0, out)
        return out

    def test_wire_writes_wrapper_member_record_and_0600_key(self):
        out = self.wire()
        self.assertNotIn("sk-demo-secret", out, "key leaked to stdout")
        keyfile = os.path.join(self.home, ".config", "ferry", "agents.key")
        self.assertEqual(_rd(keyfile), "sk-demo-secret")
        self.assertEqual(os.stat(keyfile).st_mode & 0o777, 0o600)
        wrap = os.path.join(self.home, ".local", "bin", "demo-ferry")
        body = _rd(wrap)
        self.assertIn("ferry-managed: demo", body)
        self.assertIn("DEMO_BASE_URL=http://127.0.0.1:9/v1", body)
        self.assertNotIn("sk-demo-secret", body, "key baked into wrapper")
        self.assertTrue(os.access(wrap, os.X_OK))
        d = json.loads(_rd(self.settings))
        self.assertEqual(d["theme"], "dark")
        self.assertEqual(d["providers"]["mine"], {"x": 1})
        self.assertEqual(d["providers"]["ferry"]["baseUrl"], "http://127.0.0.1:9/v1")
        self.assertEqual(d["providers"]["ferry"]["headers"]["X-Ferry-Client"], "host")
        rec = json.loads(_rd(os.path.join(self.home, ".config", "ferry", "demo.json")))
        self.assertEqual([c["path"] for c in rec["created"]], [wrap])
        self.assertEqual(rec["edited"][0]["keys"], ["providers", "ferry"])

    def test_unreachable_ferry_keeps_the_whole_default_lane_name(self):
        # Port 9 answers nothing, so the lane check prints one line; a bare
        # (f) split of a one-line string indexed its CHARACTERS ("h").
        self.wire()
        d = json.loads(_rd(self.settings))
        self.assertEqual(d["providers"]["ferry"]["model"], "heavy")
        body = _rd(os.path.join(self.home, ".local", "bin", "demo-ferry"))
        self.assertIn("--model heavy", body)

    def test_rewire_is_idempotent_and_unwire_restores_user_config(self):
        self.wire(); self.wire()
        rc, out = run_demo(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        d = json.loads(_rd(self.settings))
        self.assertEqual(d, {"theme": "dark", "providers": {"mine": {"x": 1}}})
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "demo-ferry")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "ferry", "demo.json")))

    def test_unwire_keeps_a_wrapper_the_user_changed(self):
        self.wire()
        wrap = os.path.join(self.home, ".local", "bin", "demo-ferry")
        with open(wrap, "a") as f:
            f.write("# mine now\n")
        rc, out = run_demo(self.home, "--unwire")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(wrap))
        self.assertIn("changed since ferry wrote it", out)

    def test_dry_run_unwire_changes_nothing(self):
        self.wire()
        before = _rd(self.settings)
        rc, out = run_demo(self.home, "--unwire", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("[dry-run]", out)
        self.assertEqual(_rd(self.settings), before)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config", "ferry", "demo.json")))

    def test_refuses_to_replace_a_user_file_at_the_wrapper_path(self):
        wrap = os.path.join(self.home, ".local", "bin", "demo-ferry")
        os.makedirs(os.path.dirname(wrap))
        with open(wrap, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = run_demo(self.home, "--host", "127.0.0.1", "--port", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not ferry-managed", out)
        self.assertEqual(_rd(wrap), "#!/bin/sh\necho mine\n")

    def test_client_json_mode_follows_wire_and_unwire(self):
        cj = os.path.join(self.home, ".config", "ferry", "client.json")
        os.makedirs(os.path.dirname(cj))
        with open(cj, "w") as f:
            json.dump({"host": "127.0.0.1", "port": "9", "name": "lap"}, f)
        rc, out = run_demo(self.home)
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(_rd(cj))["demo_mode"], "full")
        rc, out = run_demo(self.home, "--unwire")
        self.assertEqual(json.loads(_rd(cj))["demo_mode"], "none")


if __name__ == "__main__":
    unittest.main()
