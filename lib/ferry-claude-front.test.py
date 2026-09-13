#!/usr/bin/env python3
"""Claude-OAuth front hook: contract tests for install_claude_oauth_hook().

Run:  python3 lib/ferry-claude-front.test.py

The hook (front/ferry_front.py) wraps AnthropicModelInfo.validate_environment
so a Claude-subscription lane (model prefix `claude-oauth/` or placeholder
api_key `claude-oauth`) gets a live OAuth Bearer + the Claude Code-identical
header set instead of a real API key. Untagged anthropic lanes pass through.

These tests stub the provider/oauth modules via sys.modules so they run in any
interpreter, with or without litellm, and never touch the network or disk.

DEFERRED (documented skip): the BODY cloak (metadata.user_id / system billing
block via a transform_request wrapper) is NOT yet implemented in the hook — the
MVP cloaks headers only. test_body_cloak_via_transform_request pins that
follow-up and is skipped until it lands.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "front"))

# Stub the sibling modules BEFORE importing ferry_front so the hook resolves
# build_headers / ensure_valid_token without litellm, httpx, or a token file.
BEARER = "sk-ant-oat-test-token"
CLOAKED = {
    "Authorization": f"Bearer {BEARER}",
    "User-Agent": "claude-cli/2.1.88 (external, cli)",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
    "x-app": "cli",
}

_provider = types.ModuleType("ferry_claude_provider")
_provider.build_headers = lambda token, *, streaming, model=None, **kw: dict(CLOAKED)

_oauth = types.ModuleType("ferry_claude_oauth")


class _Tok:
    access_token = BEARER


_oauth.ensure_valid_token = lambda path: _Tok()

sys.modules.setdefault("ferry_claude_provider", _provider)
sys.modules.setdefault("ferry_claude_oauth", _oauth)

import ferry_front as FF

HAVE_HOOK = hasattr(FF, "install_claude_oauth_hook")


class _FakeInfo:
    """Stand-in for AnthropicModelInfo. validate_environment lives on the
    class so instance access binds `self` — exactly how litellm stores it and
    how the installed wrapper receives the instance as its first arg."""

    calls = []

    def validate_environment(self, headers, model, messages, optional_params,
                             litellm_params, api_key=None, api_base=None):
        type(self).calls.append(
            {"api_key": api_key, "headers": dict(headers),
             "litellm_params": litellm_params})
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}


@unittest.skipUnless(HAVE_HOOK, "install_claude_oauth_hook() not present")
class ClaudeOAuthHookTests(unittest.TestCase):
    def _fresh(self):
        _FakeInfo.calls = []
        info = _FakeInfo()
        FF.install_claude_oauth_hook(model_info_class=type(info))
        return info

    def test_tagged_by_api_key_gets_bearer_and_cloaked_headers(self):
        info = self._fresh()
        out = info.validate_environment({}, "claude-opus-4.5", [], {},
                                        {"model": "claude-oauth/claude-opus-4.5"},
                                        api_key="claude-oauth")
        self.assertEqual(out["Authorization"], f"Bearer {BEARER}")
        self.assertNotIn("x-api-key", out)
        self.assertEqual(out["x-app"], "cli")
        # Original must NOT run on the oauth path (no real key exists).
        self.assertEqual(_FakeInfo.calls, [])

    def test_tagged_by_model_prefix_alone(self):
        info = self._fresh()
        out = info.validate_environment({}, "m", [], {},
                                        {"model": "claude-oauth/claude-sonnet-4.6"})
        self.assertEqual(out["Authorization"], f"Bearer {BEARER}")
        self.assertEqual(_FakeInfo.calls, [])

    def test_untagged_passes_through_to_original(self):
        info = self._fresh()
        out = info.validate_environment({}, "claude-opus-4.5", [], {},
                                        {"model": "anthropic/claude-opus-4.5"},
                                        api_key="sk-ant-real-key")
        # Original ran, received the caller's api_key unchanged.
        self.assertEqual(len(_FakeInfo.calls), 1)
        self.assertEqual(_FakeInfo.calls[0]["api_key"], "sk-ant-real-key")
        self.assertEqual(out["x-api-key"], "sk-ant-real-key")

    def test_odd_litellm_params_is_untagged(self):
        info = self._fresh()
        out = info.validate_environment({}, "m", [], {}, None, api_key="k")
        self.assertEqual(len(_FakeInfo.calls), 1)
        self.assertEqual(out["x-api-key"], "k")

    def test_idempotent_install(self):
        info = self._fresh()
        first = type(info).validate_environment
        FF.install_claude_oauth_hook(model_info_class=type(info))
        self.assertIs(type(info).validate_environment, first)
        self.assertTrue(getattr(first, "_ferry_claude_oauth", False))

    def test_missing_litellm_symbol_is_nonfatal(self):
        # model_info_class=None with no litellm importable in this interpreter
        # must return without raising (fail-open), mirroring the other hooks.
        try:
            FF.install_claude_oauth_hook(model_info_class=None)
        except Exception as exc:  # pragma: no cover
            self.fail(f"install raised with model_info_class=None: {exc}")

    @unittest.skip("DEFERRED: body cloak via transform_request not yet in hook")
    def test_body_cloak_via_transform_request(self):
        """Follow-up: a tagged transform_request must inject metadata.user_id
        and the system billing block (front/ferry_claude_provider.apply_cloaking)
        while leaving untagged bodies byte-identical."""


if __name__ == "__main__":
    unittest.main(verbosity=2)
