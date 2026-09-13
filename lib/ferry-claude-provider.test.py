#!/usr/bin/env python3
"""Stdlib unittest for front/ferry_claude_provider.py (Claude subscription path).

Run:  python3 lib/ferry-claude-provider.test.py

Covers PART 1 (pure cloaking: headers, beta flags, URLs, billing fingerprint,
system blocks, metadata) and the PART 2 litellm seam (get_request_options)
with front/ferry_claude_oauth.py replaced by a stub module, so the sibling
token engine need not exist and nothing touches the network.
"""
import copy
import hashlib
import json
import os
import sys
import types
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import front.ferry_claude_provider as fcp  # noqa: E402

SALT = "59cf53e54c78"

EXPECTED_HEADER_KEYS = {
    "Authorization", "User-Agent", "X-Claude-Code-Session-Id",
    "X-Stainless-Lang", "X-Stainless-Package-Version", "X-Stainless-Runtime",
    "X-Stainless-Runtime-Version", "X-Stainless-Arch", "X-Stainless-Os",
    "X-Stainless-Timeout", "X-Stainless-Retry-Count", "Accept",
    "anthropic-dangerous-direct-browser-access", "anthropic-version",
    "x-app", "x-client-request-id", "anthropic-beta",
}

NON_HAIKU_BASE = ("claude-code-20250219,oauth-2025-04-20,"
                  "interleaved-thinking-2025-05-14,redact-thinking-2026-02-12,"
                  "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
                  "advanced-tool-use-2025-11-20,effort-2025-11-24")
HAIKU_BASE = ("oauth-2025-04-20,interleaved-thinking-2025-05-14,"
              "redact-thinking-2026-02-12,context-management-2025-06-27,"
              "prompt-caching-scope-2026-01-05,claude-code-20250219")


class TokenData:
    def __init__(self, access_token="stub-token-123"):
        self.access_token = access_token

    def is_expired(self):
        return False


def install_oauth_stub(record):
    module = types.ModuleType("ferry_claude_oauth")

    def ensure_valid_token(path):
        record.append(path)
        return TokenData()

    module.ensure_valid_token = ensure_valid_token
    module.TokenData = TokenData
    sys.modules["ferry_claude_oauth"] = module
    sys.modules["front.ferry_claude_oauth"] = module
    return module


class HeaderTests(unittest.TestCase):
    def test_completeness_and_casing(self):
        headers = fcp.build_headers("tok", streaming=True)
        self.assertEqual(set(headers), EXPECTED_HEADER_KEYS)
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["User-Agent"], "claude-cli/2.1.88 (external, cli)")
        self.assertEqual(headers["X-Stainless-Lang"], "js")
        self.assertEqual(headers["X-Stainless-Package-Version"], "0.74.0")
        self.assertEqual(headers["X-Stainless-Runtime"], "node")
        self.assertEqual(headers["X-Stainless-Runtime-Version"], "v22.13.0")
        self.assertIn(headers["X-Stainless-Arch"], {"arm64", "x64", "x86", "unknown"})
        self.assertIn(headers["X-Stainless-Os"], {"MacOS", "Linux", "Windows", "Unknown"})
        self.assertEqual(headers["X-Stainless-Retry-Count"], "0")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual(headers["anthropic-dangerous-direct-browser-access"], "true")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["x-app"], "cli")
        uuid.UUID(headers["x-client-request-id"])
        uuid.UUID(headers["X-Claude-Code-Session-Id"])
        self.assertEqual(headers["anthropic-beta"], NON_HAIKU_BASE)

    def test_streaming_accept_and_overrides(self):
        headers = fcp.build_headers(
            "tok", streaming=False, cli_version="9.9.9", entrypoint="vscode",
            timeout_s=0.5)
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["User-Agent"], "claude-cli/9.9.9 (external, vscode)")
        self.assertEqual(headers["X-Stainless-Timeout"], "1")
        self.assertEqual(
            fcp.build_headers("t", streaming=True, timeout_s=1)["X-Stainless-Timeout"], "1")
        self.assertEqual(
            fcp.build_headers("t", streaming=True, timeout_s=600.2)["X-Stainless-Timeout"], "601")

    def test_beta_header_tracks_model_family(self):
        self.assertEqual(
            fcp.build_headers("t", streaming=True, model="claude-3-5-haiku-latest")["anthropic-beta"],
            HAIKU_BASE)
        self.assertEqual(
            fcp.build_headers("t", streaming=True)["anthropic-beta"], NON_HAIKU_BASE)

    def test_session_uuid_reused_request_id_fresh(self):
        first = fcp.build_headers("t", streaming=True)
        second = fcp.build_headers("t", streaming=True)
        self.assertEqual(first["X-Claude-Code-Session-Id"],
                         second["X-Claude-Code-Session-Id"])
        self.assertEqual(first["X-Claude-Code-Session-Id"], fcp.get_session_id())
        self.assertNotEqual(first["x-client-request-id"], second["x-client-request-id"])
        fcp.reset_session_id()
        self.assertNotEqual(first["X-Claude-Code-Session-Id"], fcp.get_session_id())


class BetaTests(unittest.TestCase):
    def test_families_and_structured(self):
        self.assertEqual(fcp.build_anthropic_beta("claude-opus-4"), NON_HAIKU_BASE)
        self.assertEqual(fcp.build_anthropic_beta("claude-sonnet-4-5"), NON_HAIKU_BASE)
        self.assertEqual(fcp.build_anthropic_beta("claude-3-5-haiku"), HAIKU_BASE)
        self.assertTrue(fcp.build_anthropic_beta("claude-3-5-haiku").endswith("claude-code-20250219"))
        self.assertNotIn("advanced-tool-use-2025-11-20", fcp.build_anthropic_beta("claude-haiku-4"))
        self.assertNotIn("effort-2025-11-24", fcp.build_anthropic_beta("claude-haiku-4"))
        self.assertEqual(fcp.build_anthropic_beta("claude-opus-4", structured=True),
                         NON_HAIKU_BASE + ",structured-outputs-2025-12-15")
        self.assertEqual(fcp.build_anthropic_beta("claude-3-5-haiku", structured=True),
                         HAIKU_BASE + ",structured-outputs-2025-12-15")

    def test_client_beta_merge_force_prepends_oauth(self):
        merged = fcp.build_anthropic_beta(
            "claude-opus-4", client_beta="foo-2020-01-01, oauth-2025-04-20,bar-2021-02-02")
        self.assertEqual(merged.split(",")[0], "oauth-2025-04-20")
        self.assertEqual(merged.count("oauth-2025-04-20"), 1)
        self.assertIn("foo-2020-01-01", merged)
        self.assertIn("bar-2021-02-02", merged)
        self.assertIn("claude-code-20250219", merged)
        # client_beta with no NEW entries still force-prepends oauth (moved, not duplicated)
        self.assertEqual(
            fcp.build_anthropic_beta("claude-opus-4", client_beta=NON_HAIKU_BASE),
            "oauth-2025-04-20," + NON_HAIKU_BASE.replace("oauth-2025-04-20,", ""))
        haiku_merged = fcp.build_anthropic_beta("claude-3-5-haiku", client_beta="zz-1")
        self.assertEqual(haiku_merged.split(",")[0], "oauth-2025-04-20")
        self.assertTrue(haiku_merged.endswith("claude-code-20250219,zz-1"))


class UrlTests(unittest.TestCase):
    def test_upstream_urls(self):
        self.assertEqual(fcp.build_upstream_url(),
                         "https://api.anthropic.com/v1/messages?beta=true")
        self.assertEqual(fcp.build_upstream_url(count_tokens=True),
                         "https://api.anthropic.com/v1/messages/count_tokens?beta=true")


def expected_fp(text, version):
    chars = "".join(text[i] if 0 <= i < len(text) else "0" for i in (4, 7, 20))
    return hashlib.sha256((SALT + chars + version).encode()).hexdigest()[:3]


class CloakingTests(unittest.TestCase):
    def body(self, system=None, user_text=""):
        return {"model": "claude-opus-4", "system": system,
                "messages": [{"role": "user", "content": user_text}]}

    def test_fingerprint_determinism_and_formula(self):
        text = "abcdefghijklmnopqrstuvwxy"  # msg[4]=e msg[7]=h msg[20]=u
        for version in ("2.1.88", "3.0.0"):
            body = self.body(user_text=text)
            out = fcp.apply_cloaking(body, cli_version=version)
            fp = expected_fp(text, version)
            self.assertIn(f"cc_version={version}.{fp};", out["system"][0]["text"])
        # same input -> identical billing block on two fresh runs
        a = fcp.apply_cloaking(self.body(user_text=text))["system"][0]["text"]
        b = fcp.apply_cloaking(self.body(user_text=text))["system"][0]["text"]
        self.assertEqual(a, b)
        # short message: every missing index contributes "0"
        short = fcp.apply_cloaking(self.body(user_text="hi"))["system"][0]["text"]
        self.assertIn(f"cc_version=2.1.88.{expected_fp('hi', '2.1.88')};", short)
        self.assertEqual(expected_fp("hi", "2.1.88"), expected_fp("", "2.1.88"))
        # differing text (and thus fingerprint chars) -> differing fp
        self.assertNotEqual(expected_fp(text, "2.1.88"), expected_fp("hi", "2.1.88"))
        # first USER message is the source, not the first message
        body = {"messages": [{"role": "assistant", "content": "AAAAAAAAAAAAAAAAAAAAAAAA"},
                             {"role": "user", "content": text}]}
        out = fcp.apply_cloaking(body)
        self.assertIn(expected_fp(text, "2.1.88"), out["system"][0]["text"])
        self.assertIn("cc_entrypoint=cli;", out["system"][0]["text"])

    def test_system_block_ordering(self):
        original = self.body(system="be helpful", user_text="hello")
        out = fcp.apply_cloaking(copy.deepcopy(original))
        system = out["system"]
        self.assertEqual(len(system), 3)
        self.assertTrue(system[0]["text"].startswith("x-anthropic-billing-header: "))
        self.assertEqual(system[1], {"type": "text",
                                     "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                                     "cache_control": {"type": "ephemeral"}})
        self.assertEqual(system[2], {"type": "text", "text": "be helpful"})
        # pre-existing block list is preserved verbatim after the two injected blocks
        user_blocks = [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
        out = fcp.apply_cloaking(self.body(system=user_blocks, user_text="hello"))
        self.assertEqual(out["system"][2:], user_blocks)
        # no system -> just the two injected blocks
        out = fcp.apply_cloaking({"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(len(out["system"]), 2)
        self.assertEqual(out["system"][1]["text"],
                         "You are Claude Code, Anthropic's official CLI for Claude.")

    def test_metadata_user_id_shape(self):
        out = fcp.apply_cloaking(self.body(user_text="hello"), session_id="sess",
                                 account_uuid="acct", device_id="dev")
        self.assertEqual(json.loads(out["metadata"]["user_id"]),
                         {"device_id": "dev", "account_uuid": "acct", "session_id": "sess"})
        self.assertIsInstance(out["metadata"]["user_id"], str)
        generated = json.loads(fcp.apply_cloaking(self.body(user_text="hello"))["metadata"]["user_id"])
        self.assertEqual(set(generated), {"device_id", "account_uuid", "session_id"})
        for value in generated.values():
            uuid.UUID(value)
        self.assertEqual(generated["session_id"], fcp.get_session_id())

    def test_mutates_and_returns_input(self):
        body = self.body(system="s", user_text="hello")
        self.assertIs(fcp.apply_cloaking(body), body)
        self.assertEqual(body["model"], "claude-opus-4")

    def test_block_list_user_content(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "abcd"}, {"type": "text", "text": "efghijklmnopqrstuvwx"}]}]}
        out = fcp.apply_cloaking(body)
        expected = "abcd" + "efghijklmnopqrstuvwx"
        self.assertIn(f".{expected_fp(expected, '2.1.88')};", out["system"][0]["text"])


class SeamTests(unittest.TestCase):
    def setUp(self):
        self.record = []
        self.stub = install_oauth_stub(self.record)
        self._env = os.environ.get("FERRY_CLAUDE_TOKEN_PATH")
        os.environ.pop("FERRY_CLAUDE_TOKEN_PATH", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["FERRY_CLAUDE_TOKEN_PATH"] = self._env
        sys.modules.pop("ferry_claude_oauth", None)
        sys.modules.pop("front.ferry_claude_oauth", None)

    def test_get_request_options(self):
        body = {"model": "claude-opus-4", "messages": [{"role": "user", "content": "hi"}]}
        url, headers, cloaked = fcp.get_request_options(
            "claude-opus-4", copy.deepcopy(body), "/tmp/tok.json")
        self.assertEqual(url, "https://api.anthropic.com/v1/messages?beta=true")
        self.assertEqual(headers["Authorization"], "Bearer stub-token-123")
        self.assertEqual(set(headers), EXPECTED_HEADER_KEYS)
        self.assertEqual(len(cloaked["system"]), 2)
        self.assertIn("cc_version=2.1.88.", cloaked["system"][0]["text"])
        self.assertIn("metadata", cloaked)
        self.assertEqual(self.record, ["/tmp/tok.json"])
        # module wrapper and config method agree; kwargs flow through
        url2, headers2, body2 = fcp.ClaudeOAuthAnthropicConfig().get_request_options(
            "claude-3-5-haiku", {"messages": []}, "/tmp/tok.json",
            streaming=True, structured=True, count_tokens=True)
        self.assertEqual(url2, "https://api.anthropic.com/v1/messages/count_tokens?beta=true")
        self.assertEqual(headers2["Accept"], "text/event-stream")
        self.assertEqual(headers2["anthropic-beta"],
                         HAIKU_BASE + ",structured-outputs-2025-12-15")

    def test_token_path_resolution(self):
        config = fcp.ClaudeOAuthAnthropicConfig()
        body = {"messages": [{"role": "user", "content": "hi"}]}
        fcp.get_request_options("m", copy.deepcopy(body), "/explicit.json")
        self.assertEqual(self.record[-1], "/explicit.json")
        os.environ["FERRY_CLAUDE_TOKEN_PATH"] = "/from-env.json"
        self.assertEqual(config.resolve_token_path(), "/from-env.json")
        fcp.get_request_options("m", copy.deepcopy(body))
        self.assertEqual(self.record[-1], "/from-env.json")
        self.assertEqual(fcp.ClaudeOAuthAnthropicConfig("/ctor.json").resolve_token_path(),
                         "/ctor.json")
        del os.environ["FERRY_CLAUDE_TOKEN_PATH"]
        self.assertEqual(config.resolve_token_path(), fcp.DEFAULT_TOKEN_PATH)


if __name__ == "__main__":
    unittest.main()
