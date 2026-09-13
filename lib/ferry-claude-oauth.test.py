#!/usr/bin/env python3
"""Stdlib unittest for the Claude subscription OAuth credential engine.

Run:  python3 lib/ferry-claude-oauth.test.py

front/ferry_claude_oauth.py implements the native Claude Pro/Max OAuth
lifecycle (PKCE authorize URL, one-shot localhost callback, code exchange,
rotating refresh, 0600 JSON persistence) that the subscription lane will
present to litellm. The protocol constants are not ours to choose — they come
from the verified reference client — so the tests pin them and the wire
shapes, not just the happy paths:

  * PKCE: 128-char unpadded-base64url verifier from 96 random bytes,
    43-char S256 challenge that actually hashes the verifier.
  * Authorize URL: every required param present, and the SCOPE string with
    literal colons and '+' joins — %3A or %2B spellings are rejected by
    Anthropic's endpoint, so an "improved" urlencode here breaks login.
  * TokenData: JSON round-trip fidelity, is_expired skew semantics.
  * Exchange/refresh: exact JSON bodies via a mocked httpx (json only, NO
    Authorization header), refresh retries transient failures with linear
    backoff but refuses to retry a revoked/reused (permanent) refresh token.
  * Persistence: 0600 mode, and ensure_valid_token re-saving the ROTATED
    refresh token — the failure that silently bricks a stored credential.

No network access, no browser: the callback server is not exercised here.
"""
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

from ferry_claude_oauth import (  # noqa: E402
    AUTH_URL,
    CALLBACK_PATH,
    CALLBACK_PORT,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPES,
    TOKEN_URL,
    OAuthError,
    PermanentRefreshError,
    TokenData,
    TokenExchangeError,
    build_authorize_url,
    ensure_valid_token,
    exchange_code,
    generate_pkce_pair,
    load_token,
    refresh_tokens,
    save_token,
)
import ferry_claude_oauth  # noqa: E402

B64URL = re.compile(r"^[A-Za-z0-9_-]+$")

TOKEN_RESPONSE = {
    "access_token": "sk-ant-oat01-ACCESS",
    "refresh_token": "sk-ant-ort01-REFRESH",
    "expires_in": 3600,
    "account": {"email_address": "user@example.com", "uuid": "acct-uuid-1"},
}


class FakeResponse:
    """Minimal httpx.Response stand-in: status, json(), text."""

    def __init__(self, status_code=200, doc=None, text=""):
        self.status_code = status_code
        self._doc = doc if doc is not None else {}
        self.text = text or json.dumps(self._doc)

    def json(self):
        if self._doc is None:
            raise ValueError("no json")
        return self._doc


class FakeHTTPError(Exception):
    """Stands in for httpx.HTTPError while the module's httpx is mocked."""


def mock_httpx():
    """Patch the module's httpx with a MagicMock whose .post is inspectable
    and whose HTTPError is a REAL exception class (the module resolves the
    retry tuple at call time, so this flows into except clauses)."""
    patcher = mock.patch.object(ferry_claude_oauth, "httpx")
    fake = patcher.start()
    fake.HTTPError = FakeHTTPError
    fake.post.return_value = FakeResponse(200, dict(TOKEN_RESPONSE))
    return patcher, fake


class PkcETests(unittest.TestCase):
    def test_verifier_shape(self):
        for _ in range(5):
            verifier, challenge = generate_pkce_pair()
            self.assertEqual(len(verifier), 128)  # 96 bytes -> 128 b64url chars
            self.assertEqual(verifier.count("="), 0)
            self.assertTrue(B64URL.match(verifier))

    def test_challenge_is_s256_of_verifier(self):
        for _ in range(3):
            verifier, challenge = generate_pkce_pair()
            self.assertEqual(len(challenge), 43)  # SHA-256 -> 43 b64url chars
            self.assertTrue(B64URL.match(challenge))
            expected = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            self.assertEqual(challenge, expected)

    def test_verifiers_are_random(self):
        seen = {generate_pkce_pair()[0] for _ in range(4)}
        self.assertEqual(len(seen), 4)


class AuthorizeUrlTests(unittest.TestCase):
    def test_params_and_values(self):
        url, state, verifier = build_authorize_url()
        self.assertTrue(url.startswith(AUTH_URL + "?"))
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(qs["code"], ["true"])
        self.assertEqual(qs["client_id"], [CLIENT_ID])
        self.assertEqual(qs["response_type"], ["code"])
        self.assertEqual(qs["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(qs["code_challenge_method"], ["S256"])
        self.assertEqual(qs["state"], [state])
        challenge = qs["code_challenge"][0]
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(challenge, expected)

    def test_scope_literal_colons_and_plus_joins(self):
        url, _, _ = build_authorize_url()
        # The endpoint matches the scope string verbatim: the colons in each
        # scope and the '+' joins must appear literally, never %3A / %2B.
        self.assertIn("scope=" + "+".join(SCOPES), url)
        self.assertEqual("+".join(SCOPES), "org:create_api_key+user:profile+user:inference")
        self.assertNotIn("%3A", url)
        self.assertNotIn("%2B", url)
        # parse_qs decodes '+' to a space without splitting on it; the
        # colons surviving intact is what %3A-encoding would have destroyed.
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(qs["scope"], [" ".join(SCOPES)])

    def test_state_differs_per_call(self):
        states = {build_authorize_url()[1] for _ in range(3)}
        self.assertEqual(len(states), 3)


class TokenDataTests(unittest.TestCase):
    def test_json_round_trip(self):
        tok = TokenData("a", "r", "e@x.com", "uuid-9", expires_at=1234.5)
        clone = TokenData.from_json(tok.to_json())
        self.assertEqual(clone, tok)
        doc = json.loads(tok.to_json())
        self.assertEqual(
            doc,
            {
                "access_token": "a",
                "refresh_token": "r",
                "email": "e@x.com",
                "account_uuid": "uuid-9",
                "expires_at": 1234.5,
            },
        )
        self.assertEqual(TokenData.from_json(doc), tok)

    def test_from_token_response_fields(self):
        before = time.time()
        tok = TokenData.from_token_response(TOKEN_RESPONSE)
        self.assertEqual(tok.access_token, "sk-ant-oat01-ACCESS")
        self.assertEqual(tok.refresh_token, "sk-ant-ort01-REFRESH")
        self.assertEqual(tok.email, "user@example.com")
        self.assertEqual(tok.account_uuid, "acct-uuid-1")
        self.assertAlmostEqual(tok.expires_at, before + 3600, delta=5)

    def test_is_expired_skew(self):
        now = time.time()
        fresh = TokenData("a", "r", expires_at=now + 3600)
        self.assertFalse(fresh.is_expired())          # 60s default skew
        self.assertFalse(fresh.is_expired(skew=0))
        inside_skew = TokenData("a", "r", expires_at=now + 30)
        self.assertTrue(inside_skew.is_expired())     # 30s left < 60s skew
        self.assertFalse(inside_skew.is_expired(skew=10))
        gone = TokenData("a", "r", expires_at=now - 1)
        self.assertTrue(gone.is_expired(skew=0))


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.patcher, self.httpx = mock_httpx()
        self.addCleanup(self.patcher.stop)

    def test_request_body_and_no_auth_header(self):
        exchange_code("AUTHCODE", "VERIFIER", state="ST8")
        self.assertEqual(self.httpx.post.call_count, 1)
        args, kwargs = self.httpx.post.call_args
        self.assertEqual(args[0], TOKEN_URL)
        self.assertEqual(
            kwargs.get("json"),
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": "AUTHCODE",
                "redirect_uri": REDIRECT_URI,
                "code_verifier": "VERIFIER",
                "state": "ST8",
            },
        )
        # The token endpoint must see NO Authorization header.
        self.assertNotIn("headers", kwargs)

    def test_state_omitted_when_not_given(self):
        exchange_code("AUTHCODE", "VERIFIER")
        body = self.httpx.post.call_args.kwargs["json"]
        self.assertNotIn("state", body)

    def test_returns_token_data(self):
        tok = exchange_code("AUTHCODE", "VERIFIER")
        self.assertEqual(tok.access_token, "sk-ant-oat01-ACCESS")
        self.assertEqual(tok.email, "user@example.com")
        self.assertTrue(tok.expires_at > time.time())

    def test_non_200_raises(self):
        self.httpx.post.return_value = FakeResponse(400, {"error": "invalid_grant"})
        with self.assertRaises(TokenExchangeError):
            exchange_code("AUTHCODE", "VERIFIER")


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.patcher, self.httpx = mock_httpx()
        self.addCleanup(self.patcher.stop)

    def test_request_body_and_no_auth_header(self):
        refresh_tokens("RT1")
        args, kwargs = self.httpx.post.call_args
        self.assertEqual(args[0], TOKEN_URL)
        self.assertEqual(
            kwargs.get("json"),
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": "RT1",
            },
        )
        self.assertNotIn("headers", kwargs)

    def test_rotation_returned_in_token_data(self):
        tok = refresh_tokens("RT1")
        self.assertEqual(tok.refresh_token, "sk-ant-ort01-REFRESH")

    def _with_sleep(self):
        sleeper = mock.patch.object(ferry_claude_oauth.time, "sleep")
        slept = sleeper.start()
        self.addCleanup(sleeper.stop)
        return slept

    def test_retries_transient_then_succeeds(self):
        slept = self._with_sleep()
        self.httpx.post.side_effect = [FakeHTTPError("reset"), OSError("boom"),
                                       FakeResponse(200, dict(TOKEN_RESPONSE))]
        tok = refresh_tokens("RT1")
        self.assertEqual(self.httpx.post.call_count, 3)
        self.assertEqual(tok.access_token, "sk-ant-oat01-ACCESS")
        self.assertEqual(slept.call_args_list, [mock.call(1), mock.call(2)])  # linear backoff

    def test_gives_up_after_three_attempts(self):
        self._with_sleep()
        self.httpx.post.return_value = FakeResponse(503, {"error": "unavailable"})
        with self.assertRaises(TokenExchangeError):
            refresh_tokens("RT1")
        self.assertEqual(self.httpx.post.call_count, 3)

    def test_permanent_failure_is_not_retried(self):
        # A revoked/reused refresh token maps to 400/401 and can never
        # succeed on retry — retrying it risks an account lock.
        for status in (400, 401):
            with self.subTest(status=status):
                self.httpx.post.reset_mock()
                self.httpx.post.return_value = FakeResponse(
                    status, {"error": "invalid_grant, refresh token was revoked"}
                )
                with self.assertRaises(PermanentRefreshError):
                    refresh_tokens("RT1")
                self.assertEqual(self.httpx.post.call_count, 1)

    def test_permanence_detected_from_error_text_too(self):
        self.httpx.post.return_value = FakeResponse(
            429, {"error": {"type": "invalid_grant", "message": "revoked token"}}
        )
        with self.assertRaises(PermanentRefreshError):
            refresh_tokens("RT1")
        self.assertEqual(self.httpx.post.call_count, 1)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="ferry-oauth-token-")
        os.close(fd)
        os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_save_load_round_trip_and_mode(self):
        tok = TokenData("a", "r", "e@x.com", "u1", expires_at=42.0)
        save_token(self.path, tok)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        self.assertEqual(load_token(self.path), tok)

    def test_save_tightens_preexisting_mode(self):
        with open(self.path, "w") as fh:
            fh.write("stale")
        os.chmod(self.path, 0o644)
        save_token(self.path, TokenData("a", "r", expires_at=42.0))
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    def test_ensure_valid_fresh_token_no_refresh(self):
        patcher, httpx = mock_httpx()
        self.addCleanup(patcher.stop)
        tok = TokenData("a", "r", expires_at=time.time() + 3600)
        save_token(self.path, tok)
        self.assertEqual(ensure_valid_token(self.path), tok)
        httpx.post.assert_not_called()

    def test_ensure_valid_refreshes_and_persists_rotation(self):
        # Anthropic rotates the refresh token on every refresh; if the new
        # one is not persisted the NEXT refresh presents an already-consumed
        # token and the credential dies. ensure_valid_token must re-save.
        patcher, httpx = mock_httpx()
        self.addCleanup(patcher.stop)
        rotated = dict(TOKEN_RESPONSE, refresh_token="sk-ant-ort01-ROTATED")
        httpx.post.return_value = FakeResponse(200, rotated)
        expired = TokenData("old-access", "old-refresh", expires_at=time.time() - 10)
        save_token(self.path, expired)
        fresh = ensure_valid_token(self.path)
        self.assertEqual(fresh.refresh_token, "sk-ant-ort01-ROTATED")
        self.assertEqual(httpx.post.call_args.kwargs["json"]["refresh_token"], "old-refresh")
        on_disk = load_token(self.path)
        self.assertEqual(on_disk, fresh)
        self.assertEqual(on_disk.refresh_token, "sk-ant-ort01-ROTATED")

    def test_ensure_valid_expired_without_refresh_token_raises(self):
        save_token(self.path, TokenData("a", "", expires_at=time.time() - 10))
        with self.assertRaises(OAuthError):
            ensure_valid_token(self.path)


class CallbackServerStaticsTests(unittest.TestCase):
    """Pin the constants the redirect depends on; no browser, no network."""

    def test_callback_constants(self):
        self.assertEqual(CALLBACK_PORT, 54545)
        self.assertEqual(CALLBACK_PATH, "/callback")
        self.assertEqual(REDIRECT_URI, "http://localhost:54545/callback")


if __name__ == "__main__":
    unittest.main(verbosity=2)
