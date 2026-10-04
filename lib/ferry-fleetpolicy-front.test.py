#!/usr/bin/env python3
"""Tests for the front door's domestic-fleet policy: startup refusal and the
per-deployment litellm hook (front/ferry_front.py).

Run:  python3 lib/ferry-fleetpolicy-front.test.py

The hook and startup tests need no litellm; the real-Router proof does. litellm
is not importable under the system python, so this file re-execs itself under
ferry's litellm venv when that exists. No port is bound; no provider is called
(`mock_response`).
"""
import os
import sys

LITELLM_PYTHON = os.path.expanduser("~/.local/share/uv/tools/litellm/bin/python")
_REEXEC_FLAG = "FERRY_FLEETPOLICY_TEST_REEXEC"

import importlib.util  # noqa: E402

if importlib.util.find_spec("litellm") is None:
    if os.path.exists(LITELLM_PYTHON) and not os.environ.get(_REEXEC_FLAG):
        env = dict(os.environ)
        env[_REEXEC_FLAG] = "1"
        os.execve(LITELLM_PYTHON, [LITELLM_PYTHON, os.path.abspath(__file__)]
                  + sys.argv[1:], env)

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import asyncio  # noqa: E402
import io  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

import ferry_front as ff  # noqa: E402

HAVE_LITELLM = importlib.util.find_spec("litellm") is not None
TEMPLATE = os.path.join(REPO, "litellm-route-example.yaml")

BAD_YAML = """
model_list:
  - model_name: domestic.pro
    litellm_params:
      model: openai/kimi-for-coding
      api_base: https://api.kimi.com/coding
      api_key: x
"""


def write(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


def kw(model, group=None, marker=False, api_base=None, **extra):
    md = {}
    if group is not None:
        md["model_group"] = group
    if marker:
        md["ferry_domestic_only"] = True
    out = {"model": model, "metadata": md}
    if api_base:
        out["api_base"] = api_base
    out.update(extra)
    return out


class FakeBase:
    pass


def hook():
    cbs = []
    assert ff.install_fleet_policy_hook(callbacks=cbs, logger_base=FakeBase)
    return cbs, cbs[0]


class TestStartup(unittest.TestCase):
    def test_refuses_violating_config(self):
        path = write(BAD_YAML)
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            ff.enforce_startup_fleet_policy(path, stderr=err)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("domestic.pro", err.getvalue())
        self.assertIn("kimi", err.getvalue())
        self.assertIn("refusing to load", err.getvalue())

    def test_accepts_repo_template(self):
        err = io.StringIO()
        ff.enforce_startup_fleet_policy(TEMPLATE, stderr=err)
        self.assertEqual(err.getvalue(), "")

    def test_build_app_refuses_before_importing_litellm(self):
        path = write(BAD_YAML)
        old = os.environ.get("CONFIG_FILE_PATH")
        os.environ["CONFIG_FILE_PATH"] = path
        try:
            with self.assertRaises(SystemExit) as cm:
                ff.build_app(litellm_app=object())
            self.assertEqual(cm.exception.code, 1)
        finally:
            if old is None:
                os.environ.pop("CONFIG_FILE_PATH", None)
            else:
                os.environ["CONFIG_FILE_PATH"] = old

    def test_unreadable_config_is_not_this_checks_job(self):
        err = io.StringIO()
        ff.enforce_startup_fleet_policy("/nonexistent/litellm.yaml", stderr=err)
        self.assertIn("not checked", err.getvalue())


class TestHookMessages(unittest.TestCase):
    def test_install_is_idempotent(self):
        cbs, _ = hook()
        self.assertTrue(ff.install_fleet_policy_hook(callbacks=cbs, logger_base=FakeBase))
        self.assertEqual(len(cbs), 1)

    def test_blocks_domestic_group_on_kimi(self):
        self.assertTrue(ff.fleet_policy_violation(
            kw("openai/kimi-for-coding", "domestic.pro", api_base="https://api.kimi.com/coding")))

    def test_blocks_domestic_group_on_zai(self):
        self.assertTrue(ff.fleet_policy_violation(
            kw("openai/glm-5.3", "domestic.pro", api_base="https://api.z.ai/api/coding/paas/v4")))

    def test_blocks_marker_under_non_fleet_name(self):
        self.assertTrue(ff.fleet_policy_violation(
            kw("openai/kimi-for-coding", "flash", marker=True,
               api_base="https://api.kimi.com/coding")))

    def test_marker_in_litellm_metadata_also_counts(self):
        k = {"model": "openai/glm-5", "litellm_metadata": {"ferry_domestic_only": True}}
        self.assertTrue(ff.fleet_policy_violation(k))

    def test_allows_international_group(self):
        self.assertIsNone(ff.fleet_policy_violation(
            kw("openai/kimi-for-coding", "international.pro",
               api_base="https://api.kimi.com/coding")))

    def test_allows_us_deployments_in_domestic(self):
        for model, base in (("chatgpt/gpt-5.5", None),
                            ("openrouter/openai/gpt-5.5", "https://openrouter.ai/api/v1"),
                            ("gemini/gemini-3-pro", None)):
            with self.subTest(model=model):
                self.assertIsNone(ff.fleet_policy_violation(
                    kw(model, "domestic.pro", api_base=base)))

    def test_restricted_fails_closed_on_unreadable_params(self):
        self.assertTrue(ff.fleet_policy_violation(
            {"model": None, "metadata": {"model_group": "domestic.pro"}}))

    def test_unrestricted_fails_open_on_garbage(self):
        self.assertIsNone(ff.fleet_policy_violation({"metadata": "nope"}))
        self.assertIsNone(ff.fleet_policy_violation({}))

    def test_deployment_violation_shares_the_denylist(self):
        import ferry_fleet_guard as g
        self.assertIn("kimi", g.deployment_violation({"model": "openai/kimi-k3"}))
        self.assertIn("api_base", g.deployment_violation(
            {"model": "openai/x", "api_base": "https://open.bigmodel.cn/api"}))
        self.assertIsNone(g.deployment_violation({"model": "chatgpt/gpt-5.5"}))
        self.assertIsNone(g.deployment_violation(None))


@unittest.skipUnless(HAVE_LITELLM, "litellm not importable and venv python missing")
class TestRealRouter(unittest.TestCase):
    def setUp(self):
        import litellm
        self.litellm = litellm
        self._saved = list(litellm.callbacks)
        litellm.callbacks = [c for c in litellm.callbacks
                             if not getattr(c, "_ferry_fleet_policy", False)]
        self.assertTrue(ff.install_fleet_policy_hook())

    def tearDown(self):
        self.litellm.callbacks = self._saved

    def router(self):
        params = {"model": "openai/kimi-for-coding", "api_base": "https://api.kimi.com/coding",
                  "api_key": "k", "mock_response": "hi"}
        us = {"model": "openai/gpt-5.5", "api_base": "https://api.openai.com/v1",
              "api_key": "k", "mock_response": "hi"}
        return self.litellm.Router(model_list=[
            {"model_name": "domestic.x", "litellm_params": dict(params)},
            {"model_name": "international.x", "litellm_params": dict(params)},
            {"model_name": "flash", "litellm_params": dict(params)},
            {"model_name": "domestic.us", "litellm_params": dict(us)},
        ])

    def call(self, router, model, **kwargs):
        return asyncio.run(router.acompletion(
            model=model, messages=[{"role": "user", "content": "x"}], **kwargs))

    def test_domestic_group_on_kimi_is_blocked(self):
        with self.assertRaises(self.litellm.PermissionDeniedError) as cm:
            self.call(self.router(), "domestic.x")
        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("fleet policy", str(cm.exception))

    def test_marker_on_non_fleet_name_is_blocked(self):
        with self.assertRaises(self.litellm.PermissionDeniedError):
            self.call(self.router(), "flash", metadata={"ferry_domestic_only": True})

    def test_international_group_succeeds(self):
        resp = self.call(self.router(), "international.x")
        self.assertEqual(resp.choices[0].message.content, "hi")

    def test_unmarked_non_fleet_name_succeeds(self):
        resp = self.call(self.router(), "flash")
        self.assertEqual(resp.choices[0].message.content, "hi")

    def test_domestic_group_on_us_deployment_succeeds(self):
        resp = self.call(self.router(), "domestic.us")
        self.assertEqual(resp.choices[0].message.content, "hi")

    def test_mixed_group_never_returns_the_chinese_deployment(self):
        # litellm retries a 403 when the group has other deployments, so a
        # blocked pick lands on the US one; the Chinese answer is never served.
        r = self.litellm.Router(model_list=[
            {"model_name": "domestic.mix", "litellm_params": {
                "model": "openai/kimi-for-coding", "api_base": "https://api.kimi.com/coding",
                "api_key": "k", "mock_response": "KIMI"}},
            {"model_name": "domestic.mix", "litellm_params": {
                "model": "openai/gpt-5.5", "api_base": "https://api.openai.com/v1",
                "api_key": "k", "mock_response": "US"}},
        ], num_retries=5)
        got = set()
        for _ in range(12):
            got.add(self.call(r, "domestic.mix").choices[0].message.content)
        self.assertEqual(got, {"US"})


if __name__ == "__main__":
    unittest.main()
