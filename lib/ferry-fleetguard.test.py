#!/usr/bin/env python3
"""Tests for the domestic-fleet policy guard (front/ferry_fleet_guard.py).

Run:  python3 lib/ferry-fleetguard.test.py

The `domestic` fleet is US-only. On 2026-09-14 the live domestic lanes were
re-pointed at Kimi K3 and z.ai GLM and nothing refused it for three weeks, so
ferry now refuses to load a route config in which `domestic` can reach a
Chinese model or provider, directly or through a fallback or model_group_alias.
"""
import copy
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(REPO, "front", "ferry_fleet_guard.py")
FERRY = os.path.join(REPO, "ferry")
TEMPLATE = os.path.join(REPO, "litellm-route-example.yaml")

_spec = importlib.util.spec_from_file_location("ferry_fleet_guard", GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def dep(name, model, **lp):
    return {"model_name": name, "litellm_params": dict(model=model, **lp)}


def cfg(deployments, **router):
    c = {"model_list": deployments}
    if router:
        c["router_settings"] = router
    return c


US = dep("domestic.heavy", "chatgpt/responses/gpt-6-astra")


class TestDenylist(unittest.TestCase):
    def test_template_is_clean(self):
        with open(TEMPLATE) as fh:
            self.assertEqual(guard.check(yaml.safe_load(fh)), [])

    def test_kimi_via_api_base_on_anthropic_k3_is_refused(self):
        c = cfg([dep("domestic.heavy", "anthropic/k3",
                     api_base="https://api.kimi.com/coding")])
        out = guard.check(c)
        self.assertEqual(len(out), 1, out)
        self.assertIn("domestic.heavy", out[0])
        self.assertIn("kimi", out[0])
        self.assertIn("https://api.kimi.com/coding", out[0])

    def test_openrouter_moonshot_is_refused(self):
        out = guard.check(cfg([dep("domestic.heavy", "openrouter/moonshotai/kimi-k3")]))
        self.assertTrue(out)
        self.assertIn("moonshot", out[0])

    def test_zai_glm_is_refused(self):
        out = guard.check(cfg([dep("domestic.medium", "zai/glm-5.3",
                                   api_base="https://api.z.ai/api/coding/paas/v4")]))
        self.assertEqual(len(out), 2, out)  # model and api_base both match
        self.assertTrue(any("zai" in m for m in out))
        self.assertTrue(any("z.ai" in m for m in out))

    def test_base_model_and_custom_provider_are_checked(self):
        c = cfg([{"model_name": "domestic.heavy",
                  "litellm_params": {"model": "openai/x", "custom_llm_provider": "deepseek"},
                  "model_info": {"base_model": "qwen3-max"}}])
        out = guard.check(c)
        self.assertEqual(len(out), 2, out)

    def test_international_lane_with_kimi_is_allowed(self):
        c = cfg([US, dep("international.heavy", "openrouter/moonshotai/kimi-k3")])
        self.assertEqual(guard.check(c), [])

    def test_must_not_match_list_stays_clean(self):
        for model in ("openai/gpt-5", "chatgpt/responses/gpt-6-astra",
                      "openrouter/~google/gemini-flash-latest",
                      "openrouter/openai/gpt-5.6-terra",
                      "anthropic/claude-opus-4-6", "meta/muse-spark"):
            self.assertEqual(guard.check(cfg([dep("domestic.heavy", model)])), [], model)
        self.assertEqual(
            guard.check(cfg([dep("domestic.heavy", "openai/x",
                                 api_base="https://api.openai.com/v1")])), [])

    def test_left_boundary_does_not_match_inside_a_word(self):
        # "glm" inside "englmx" has an alphanumeric on its left: no match.
        self.assertEqual(guard.check(cfg([dep("domestic.heavy", "x/englmx")])), [])
        self.assertTrue(guard.check(cfg([dep("domestic.heavy", "x/GLM-5")])))


class TestReachability(unittest.TestCase):
    BAD = dep("international.heavy-glm", "openrouter/z-ai/glm-5.3")

    def test_fallback_into_an_international_kimi_lane_is_refused_with_path(self):
        c = cfg([US, dep("international.heavy-kimi", "openrouter/moonshotai/kimi-k3")],
                fallbacks=[{"domestic.heavy": ["international.heavy-kimi"]}])
        out = guard.check(c)
        self.assertEqual(len(out), 1, out)
        self.assertIn("domestic.heavy -> fallback -> international.heavy-kimi", out[0])

    def test_fallback_chain_is_transitive(self):
        c = cfg([US, dep("international.a", "openai/gpt-5"), self.BAD],
                fallbacks=[{"domestic.heavy": ["international.a"]},
                           {"international.a": ["international.heavy-glm"]}])
        out = guard.check(c)
        self.assertEqual(len(out), 1, out)
        self.assertIn("domestic.heavy -> fallback -> international.a -> fallback -> "
                      "international.heavy-glm", out[0])

    def test_every_fallback_key_in_both_sections_counts(self):
        for section in ("router_settings", "litellm_settings"):
            for key in ("fallbacks", "context_window_fallbacks",
                        "content_policy_fallbacks"):
                c = {"model_list": [US, self.BAD],
                     section: {key: [{"domestic.heavy": ["international.heavy-glm"]}]}}
                self.assertTrue(guard.check(c), (section, key))

    def test_model_group_alias_is_followed(self):
        for alias in ("international.heavy-glm", {"model": "international.heavy-glm"}):
            c = cfg([US, self.BAD], model_group_alias={"domestic.heavy": alias})
            out = guard.check(c)
            self.assertEqual(len(out), 1, out)
            self.assertIn("domestic.heavy -> alias -> international.heavy-glm", out[0])

    def test_default_fallbacks_are_reachable_from_every_group(self):
        c = {"model_list": [US, self.BAD],
             "litellm_settings": {"default_fallbacks": ["international.heavy-glm"]}}
        out = guard.check(c)
        self.assertEqual(len(out), 1, out)
        self.assertIn("default_fallbacks", out[0])

    def test_default_fallbacks_without_a_restricted_fleet_are_ignored(self):
        c = {"model_list": [dep("international.heavy", "openai/gpt-5"), self.BAD],
             "litellm_settings": {"default_fallbacks": ["international.heavy-glm"]}}
        self.assertEqual(guard.check(c), [])

    def test_unreached_bad_lane_and_unrelated_fallbacks_are_allowed(self):
        c = cfg([US, self.BAD],
                fallbacks=[{"international.heavy": ["international.heavy-glm"]}])
        self.assertEqual(guard.check(c), [])

    def test_cycles_terminate(self):
        c = cfg([US], fallbacks=[{"domestic.heavy": ["domestic.heavy"]}])
        self.assertEqual(guard.check(c), [])


class TestCli(unittest.TestCase):
    def _run(self, text):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "litellm.yaml")
        with open(path, "w") as fh:
            fh.write(text)
        return path, subprocess.run([sys.executable, GUARD, path],
                                    capture_output=True, text=True, timeout=60)

    def test_exit_0_silent_when_clean(self):
        proc = subprocess.run([sys.executable, GUARD, TEMPLATE],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_exit_1_with_message_on_violation(self):
        path, proc = self._run(textwrap.dedent("""\
            model_list:
              - model_name: domestic.heavy
                litellm_params: {model: zai/glm-5.3}
            """))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(
            lines[0],
            "Error: the domestic fleet must use US models only; refusing to load %s:" % path)
        self.assertIn("domestic.heavy", lines[1])
        self.assertTrue(lines[-1].startswith("Fix the lane in %s, then re-run." % path))

    def test_exit_2_on_invalid_yaml_and_missing_file(self):
        _, proc = self._run("model_list: [unclosed\n")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Error", proc.stdout)
        proc = subprocess.run([sys.executable, GUARD, "/nonexistent/x.yaml"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)


class TestReloadRefuses(unittest.TestCase):
    """Run the REAL built `ferry reload` against a temp HOME and stub binaries.

    pgrep/pkill/lsof/curl/litellm are stubs that append their name to a call
    log, so the live proxy on :8090 is never touched. The refusal must happen
    BEFORE _ferry_stop_litellm (its first act is `pgrep`), leaving the running
    proxy alone; the clean-config control proves the stubs would have seen it.
    """

    def _stub(self, path, body):
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def _reload(self, config_text):
        root = tempfile.mkdtemp()
        home = os.path.join(root, "home")
        bin_ = os.path.join(root, "bin")
        os.makedirs(os.path.join(home, ".config", "ferry"))
        os.makedirs(bin_)
        calls = os.path.join(root, "calls.log")
        with open(os.path.join(home, ".config", "ferry", "litellm.yaml"), "w") as fh:
            fh.write(config_text)
        log = 'echo "$(basename "$0") $*" >> "%s"\n' % calls
        for name, rc in (("pkill", 0), ("lsof", 1), ("curl", 0), ("litellm", 0)):
            self._stub(os.path.join(bin_, name), log + "exit %d\n" % rc)
        # pgrep is _ferry_stop_litellm's first call. Log it, then end the whole
        # run: the control only needs to see that the stop was reached, not sit
        # through the launch and readiness wait that follow.
        self._stub(os.path.join(bin_, "pgrep"), log + "kill -TERM $PPID\nexit 1\n")
        # litellm's venv python (found next to the litellm stub): real
        # interpreter for the guard, but the front launch fails so ferry takes
        # its plain-litellm fallback (the stub).
        self._stub(os.path.join(bin_, "python"),
                   'case "$1" in *ferry_front.py) exit 1;; esac\nexec "%s" "$@"\n'
                   % sys.executable)
        # A temp HOME hides a user-site PyYAML; hand the guard's interpreter the
        # directory the test's own PyYAML was imported from.
        env = {"HOME": home, "TMPDIR": root,
               "PYTHONPATH": os.path.dirname(os.path.dirname(yaml.__file__)),
               "PATH": bin_ + ":/usr/bin:/bin:/usr/sbin:/sbin:"
                       + os.path.dirname(sys.executable)}
        proc = subprocess.run(["zsh", FERRY, "reload"], env=env, cwd=root,
                              capture_output=True, text=True, timeout=120)
        try:
            with open(calls) as fh:
                made = fh.read().split("\n")
        except FileNotFoundError:
            made = []
        return proc, [c.split(" ")[0] for c in made if c]

    BAD = textwrap.dedent("""\
        model_list:
          - model_name: domestic.heavy
            litellm_params: {model: anthropic/k3, api_base: "https://api.kimi.com/coding"}
        """)
    GOOD = textwrap.dedent("""\
        model_list:
          - model_name: domestic.heavy
            litellm_params: {model: chatgpt/responses/gpt-6-astra}
        """)

    def test_reload_with_a_violating_config_refuses_before_stopping_anything(self):
        proc, calls = self._reload(self.BAD)
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("the domestic fleet must use US models only", out)
        self.assertIn("kimi", out)
        self.assertEqual(calls, [], "refusal must precede every stop/launch call: %s" % calls)
        self.assertNotIn("Reloading the front door", out)

    def test_control_reload_with_a_clean_config_does_reach_the_stop(self):
        proc, calls = self._reload(self.GOOD)
        out = proc.stdout + proc.stderr
        self.assertNotIn("must use US models only", out)
        self.assertIn("Reloading the front door", out)
        self.assertIn("pgrep", calls, "stubs would have seen the stop call: %s" % out)


if __name__ == "__main__":
    unittest.main()
