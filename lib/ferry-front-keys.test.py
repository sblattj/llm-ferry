#!/usr/bin/env python3
"""Per-device client keys at the front door (front/ferry_front.py).

Run:  python3 lib/ferry-front-keys.test.py

Every test runs with FERRY_KEYS_FILE / FERRY_KEYS_DB in a temp dir and a fake
LITELLM_MASTER_KEY, drives LaneCatalogueFilter directly with a recording ASGI
app, and never imports litellm or binds a port.

Task 3 owns the authentication tests; Task 4 appends limit and accounting
tests to this same file.
"""
import asyncio
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

import ferry_front as FF  # noqa: E402
import ferry_keys as K  # noqa: E402

MASTER = "sk-test-master"

# Copied from lib/ferry-front.test.py FLEETS (valid at 43acdda) — a test file
# must not import another test file.
FLEETS = {"domestic": {"heavy": "chatgpt/responses/gpt-5.6-sol",
                       "medium": "chatgpt/responses/gpt-5.6-terra",
                       "flash": "openrouter/~google/gemini-flash-latest",
                       "super-flash": "openrouter/~google/gemini-flash-latest"},
          "international": {"heavy": "anthropic/k3",
                            "medium": "zai/glm-5.3",
                            "flash": "zai/glm-5.3-flash",
                            "super-flash": "zai/glm-5.3-flash"}}

USAGE_BODY = json.dumps({"id": "x", "choices": [],
                         "usage": {"prompt_tokens": 7, "completion_tokens": 5}}).encode()


class Upstream:
    """Stands in for litellm: records what it was handed, answers 200 JSON."""

    def __init__(self, payload=USAGE_BODY):
        self.payload = payload
        self.calls = 0
        self.scope = None
        self.send = None
        self.body = None

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scope = scope
        self.send = send
        if scope.get("type") == "websocket":
            await send({"type": "websocket.accept"})
            return
        buf = b""
        while True:
            msg = await receive()
            buf += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        self.body = buf
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": self.payload,
                    "more_body": False})

    def headers(self):
        return {bytes(k).lower(): bytes(v) for k, v in self.scope["headers"]}


def drive(mw, path, headers=(), body=b"{}", method="POST",
          client=("100.64.0.9", 50000), scope_type="http", query=b""):
    scope = {"type": scope_type, "path": path, "method": method, "client": client,
             "headers": [(k.encode(), v.encode()) for k, v in headers],
             "query_string": query}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return scope, sent, send


def reply(sent):
    """(status, {header: value}, parsed body) of the first response in `sent`."""
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {bytes(k).lower(): bytes(v) for k, v in start.get("headers", [])}
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    return start["status"], headers, doc


def bearer(token):
    return [("authorization", "Bearer " + token)]


class KeyFrontCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-front-keys-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.keys = os.path.join(self.dir, "keys.json")
        self.db = os.path.join(self.dir, "keys-usage.sqlite")
        env = {"FERRY_KEYS_FILE": self.keys, "FERRY_KEYS_DB": self.db,
               "LITELLM_MASTER_KEY": MASTER, "FERRY_EVENTS": "0",
               "FERRY_STRIP_HEADERS": "0"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        FF._KEY_CACHE = None
        FF._KEY_WARNED.clear()
        self.app = Upstream()

    def mw(self, state=None, fleets=None):
        return FF.LaneCatalogueFilter(self.app, frozenset(), fleets=fleets, state=state)

    def mint(self, name="laptop", **kwargs):
        return K.add(name, **kwargs)[1]

    def bump(self):
        st = os.stat(self.keys)
        os.utime(self.keys, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


class TestPassThrough(KeyFrontCase):
    def test_master_and_bare_requests_touch_no_key_state(self):
        # The key-store accessor raises: a master or bare request that reached
        # it would 401 (fail-closed) instead of passing through.
        boom = mock.Mock(side_effect=AssertionError("key store touched"))
        patcher = mock.patch.object(FF, "_key_cache", boom)
        patcher.start()
        self.addCleanup(patcher.stop)
        for headers, label in ((bearer(MASTER), "master"), ((), ""),
                               ([("x-api-key", MASTER)], "master")):
            with self.subTest(label=label, headers=headers):
                scope, sent, send = drive(self.mw(), "/v1/chat/completions", headers)
                self.assertEqual(reply(sent)[0], 200)
                seen = self.app.headers()
                for name in (b"authorization", b"x-api-key"):
                    want = dict(headers).get(name.decode())
                    self.assertEqual(seen.get(name), want.encode() if want else None)
                self.assertEqual(scope["ferry.key"], label)
                self.assertNotIn("ferry.key_entry", scope)
                self.assertIs(self.app.send, send)
        boom.assert_not_called()
        self.assertFalse(os.path.exists(self.keys))
        self.assertFalse(os.path.exists(self.db))

    def test_the_no_touch_probe_can_fail(self):
        # Control for the test above: the same patch DOES fire for an fk- key.
        boom = mock.Mock(side_effect=AssertionError("key store touched"))
        with mock.patch.object(FF, "_key_cache", boom):
            _, sent, _ = drive(self.mw(), "/v1/chat/completions",
                               bearer("fk-nobody-" + "a" * 32))
        self.assertEqual(reply(sent)[0], 401)
        self.assertEqual(boom.call_count, 1)

    def test_a_non_ferry_credential_is_litellms_business(self):
        scope, _, _ = drive(self.mw(), "/v1/chat/completions", bearer("sk-something-else"))
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer sk-something-else")
        self.assertEqual(scope["ferry.key"], "")


class TestDeviceKeys(KeyFrontCase):
    def test_device_key_is_rewritten_to_the_master(self):
        token = self.mint()
        scope, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(token))
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer " + MASTER.encode())
        self.assertEqual(scope["ferry.key"], "laptop")
        self.assertEqual(scope["ferry.key_entry"]["name"], "laptop")
        self.assertNotIn(token.encode(), b"".join(v for _, v in self.app.scope["headers"]))

    def test_device_key_is_stripped_when_no_master_is_configured(self):
        token = self.mint()
        with mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
            scope, _, _ = drive(self.mw(), "/v1/models", bearer(token), method="GET")
        self.assertNotIn(b"authorization", self.app.headers())
        self.assertEqual(scope["ferry.key"], "laptop")

    def test_x_api_key_carrying_a_device_key_is_rewritten(self):
        token = self.mint()
        drive(self.mw(), "/v1/messages", [("x-api-key", token),
                                          ("anthropic-version", "2023-06-01")])
        headers = self.app.headers()
        self.assertEqual(headers[b"x-api-key"], MASTER.encode())
        self.assertEqual(headers[b"anthropic-version"], b"2023-06-01")

    def test_refused_keys_are_401_and_never_reach_litellm(self):
        _, revoked = K.add("gone")
        K.revoke("gone")
        past = K.iso(datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(days=1))
        _, expired = K.add("old", expires=past)
        cases = {"unknown": "fk-nobody-" + "a" * 32, "revoked": revoked,
                 "expired": expired}
        for reason, token in cases.items():
            for path, method in (("/v1/chat/completions", "POST"),
                                 ("/v1/models", "GET")):
                with self.subTest(reason=reason, path=path):
                    _, sent, _ = drive(self.mw(), path, bearer(token), method=method)
                    status, _, doc = reply(sent)
                    self.assertEqual(status, 401)
                    self.assertEqual(doc["error"]["code"], "invalid_api_key")
                    self.assertEqual(doc["error"]["type"], "invalid_request_error")
                    self.assertIn(reason, doc["error"]["message"])
        self.assertEqual(self.app.calls, 0)

    def test_anthropic_path_gets_the_anthropic_error_shape(self):
        _, sent, _ = drive(self.mw(), "/v1/messages",
                           [("x-api-key", "fk-nobody-" + "a" * 32)])
        status, _, doc = reply(sent)
        self.assertEqual(status, 401)
        self.assertEqual(doc["type"], "error")
        self.assertEqual(doc["error"]["type"], "authentication_error")

    def test_revoke_takes_effect_on_the_next_request(self):
        token = self.mint()
        mw = self.mw()
        self.assertEqual(reply(drive(mw, "/v1/models", bearer(token), method="GET")[1])[0], 200)
        K.revoke("laptop")
        self.bump()
        self.assertEqual(reply(drive(mw, "/v1/models", bearer(token), method="GET")[1])[0], 401)

    def test_corrupt_store_refuses_device_keys_but_not_the_master(self):
        token = self.mint()
        with open(self.keys, "w") as fh:
            fh.write("{corrupt")
        self.bump()
        with mock.patch("sys.stderr") as err:
            _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(token))
            status, _, doc = reply(sent)
            self.assertEqual(status, 401)
            self.assertIn("unreadable", doc["error"]["message"])
            drive(self.mw(), "/v1/chat/completions", bearer(token))
        warned = "".join(str(c.args[0]) for c in err.write.call_args_list if c.args)
        self.assertEqual(warned.count("ferry-front: client keys:"), 1)
        _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(MASTER))
        self.assertEqual(reply(sent)[0], 200)

    def test_websocket_with_a_revoked_key_is_closed(self):
        _, token = K.add("ws")
        K.revoke("ws")
        _, sent, _ = drive(self.mw(), "/v1/realtime", bearer(token),
                           method="GET", scope_type="websocket")
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        self.assertEqual(self.app.calls, 0)

    def test_websocket_with_a_valid_key_is_closed_unrewritten(self):
        # Round 6: no websocket is metered, so even a valid key is refused.
        token = self.mint()
        scope, sent, _ = drive(self.mw(), "/v1/realtime", bearer(token), method="GET",
                               scope_type="websocket")
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        self.assertEqual(self.app.calls, 0)
        self.assertEqual(scope["headers"], [(b"authorization", ("Bearer " + token).encode())])


class TestAmbiguousCredentials(KeyFrontCase):
    """Fix round 1: a request must present exactly one credential, and when
    any credential header carries an fk- key, every credential header must
    be that same validated key."""

    def refused(self, headers, path="/v1/chat/completions"):
        _, sent, _ = drive(self.mw(), path, headers)
        status, _, doc = reply(sent)
        self.assertEqual(status, 401, headers)
        self.assertEqual(doc["error"]["code"], "invalid_api_key")
        return doc

    def test_duplicate_credential_headers_with_an_fk_word_are_refused(self):
        token = self.mint()
        cases = {
            "two fk bearers": bearer(token) + bearer(token),
            "fk then master": bearer(token) + bearer(MASTER),
            "master then fk": bearer(MASTER) + bearer(token),
            "two x-api-keys": [("x-api-key", token), ("x-api-key", token)],
            "foreign then fk x-api-key": [("x-api-key", "sk-x"), ("x-api-key", token)],
        }
        for label, headers in cases.items():
            with self.subTest(label=label):
                self.refused(headers)
        self.assertEqual(self.app.calls, 0)

    def test_duplicate_headers_without_an_fk_word_pass_through_untouched(self):
        # Global constraint: master and non-fk requests keep today's behaviour
        # exactly, so a duplicate without any fk- word is litellm's business.
        boom = mock.Mock(side_effect=AssertionError("key store touched"))
        cases = {
            "master bearer twice": (bearer(MASTER) + bearer(MASTER), "master"),
            "master x-api-key twice": ([("x-api-key", MASTER), ("x-api-key", MASTER)],
                                       "master"),
            "foreign bearer twice": (bearer("sk-a") + bearer("sk-b"), ""),
        }
        with mock.patch.object(FF, "_key_cache", boom):
            for label, (headers, key) in cases.items():
                with self.subTest(label=label):
                    scope, sent, send = drive(self.mw(), "/v1/chat/completions", headers)
                    self.assertEqual(reply(sent)[0], 200)
                    self.assertIs(self.app.send, send)
                    self.assertEqual(scope["ferry.key"], key)
                    self.assertEqual([(k.decode(), v.decode())
                                      for k, v in self.app.scope["headers"]], headers)
        boom.assert_not_called()

    def test_mixed_credentials_with_an_fk_key_are_refused(self):
        token = self.mint()
        _, other = K.add("other")
        cases = {
            "two different fk keys": bearer(token) + [("x-api-key", other)],
            "foreign bearer + fk x-api-key": bearer("sk-something-else")
                                             + [("x-api-key", token)],
            "master bearer + fk x-api-key": bearer(MASTER) + [("x-api-key", token)],
            "fk bearer + master x-api-key": bearer(token) + [("x-api-key", MASTER)],
            "fk under another scheme": [("authorization", "Token " + token)],
            "fk after a comma": [("authorization", "Bearer sk-x, " + token)],
        }
        for label, headers in cases.items():
            with self.subTest(label=label):
                self.refused(headers)
        self.assertEqual(self.app.calls, 0)

    def test_the_same_fk_key_in_both_headers_is_admitted_and_both_rewritten(self):
        token = self.mint()
        scope, sent, _ = drive(self.mw(), "/v1/chat/completions",
                               bearer(token) + [("x-api-key", token)])
        self.assertEqual(reply(sent)[0], 200)
        headers = self.app.headers()
        self.assertEqual(headers[b"authorization"], b"Bearer " + MASTER.encode())
        self.assertEqual(headers[b"x-api-key"], MASTER.encode())
        self.assertEqual(scope["ferry.key"], "laptop")

    def test_scheme_less_authorization_fk_is_a_presented_key(self):
        token = self.mint()
        scope, sent, _ = drive(self.mw(), "/v1/chat/completions",
                               [("authorization", token)])
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer " + MASTER.encode())
        self.assertEqual(scope["ferry.key"], "laptop")
        K.revoke("laptop")
        self.bump()
        doc = self.refused([("authorization", token)])
        self.assertIn("revoked", doc["error"]["message"])

    def test_websocket_with_ambiguous_credentials_is_closed(self):
        token = self.mint()
        _, sent, _ = drive(self.mw(), "/v1/realtime", bearer(token) + bearer(token),
                           method="GET", scope_type="websocket")
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        _, sent, _ = drive(self.mw(), "/v1/realtime",
                           bearer(MASTER) + [("x-api-key", token)],
                           method="GET", scope_type="websocket")
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        self.assertEqual(self.app.calls, 0)

    def test_a_foreign_key_that_merely_contains_fk_dash_is_not_a_device_key(self):
        scope, sent, _ = drive(self.mw(), "/v1/chat/completions",
                               bearer("sk-abcfk-xyz"))
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer sk-abcfk-xyz")
        self.assertEqual(scope["ferry.key"], "")


EXTRA_HEADERS = ("x-litellm-api-key", "api-key", "x-goog-api-key",
                 "ocp-apim-subscription-key")


class TestEveryLitellmCredentialSource(KeyFrontCase):
    """Fix round 2: every header litellm 1.99 reads a key from, plus ?key=."""

    def refused(self, headers, query=b"", path="/v1/chat/completions"):
        _, sent, _ = drive(self.mw(), path, headers, query=query)
        status, _, doc = reply(sent)
        self.assertEqual(status, 401, (headers, query))
        return doc

    def test_a_device_key_in_any_litellm_header_is_rewritten(self):
        token = self.mint()
        for name in EXTRA_HEADERS:
            with self.subTest(header=name):
                scope, sent, _ = drive(self.mw(), "/v1/chat/completions",
                                       [(name, token)])
                self.assertEqual(reply(sent)[0], 200)
                self.assertEqual(self.app.headers()[name.encode()], MASTER.encode())
                self.assertEqual(scope["ferry.key"], "laptop")

    def test_a_revoked_key_in_any_litellm_header_is_refused(self):
        _, token = K.add("gone")
        K.revoke("gone")
        for name in EXTRA_HEADERS:
            with self.subTest(header=name):
                self.assertIn("revoked", self.refused([(name, token)])["error"]["message"])
        self.assertEqual(self.app.calls, 0)

    def test_a_different_credential_in_any_header_beside_a_device_key_is_refused(self):
        token = self.mint()
        for name in EXTRA_HEADERS:
            for other in ("sk-other", MASTER):
                with self.subTest(header=name, other=other):
                    self.refused(bearer(token) + [(name, other)])
                    self.refused([(name, token), ("authorization", "Bearer " + other)])
        self.assertEqual(self.app.calls, 0)

    def test_duplicate_extra_header_with_a_device_key_is_refused(self):
        token = self.mint()
        for name in EXTRA_HEADERS:
            with self.subTest(header=name):
                self.refused([(name, token), (name, token)])

    def test_the_same_key_everywhere_is_admitted_and_every_copy_rewritten(self):
        token = self.mint()
        headers = bearer(token) + [(name, token) for name in EXTRA_HEADERS]
        drive(self.mw(), "/v1/chat/completions", headers)
        seen = self.app.headers()
        self.assertEqual(seen[b"authorization"], b"Bearer " + MASTER.encode())
        for name in EXTRA_HEADERS:
            self.assertEqual(seen[name.encode()], MASTER.encode())

    def test_master_in_x_litellm_api_key_is_untouched(self):
        boom = mock.Mock(side_effect=AssertionError("key store touched"))
        with mock.patch.object(FF, "_key_cache", boom):
            headers = [("x-litellm-api-key", MASTER), ("x-litellm-api-key", MASTER)]
            scope, sent, send = drive(self.mw(), "/v1/chat/completions", headers)
        self.assertEqual(reply(sent)[0], 200)
        self.assertIs(self.app.send, send)
        self.assertEqual(scope["ferry.key"], "master")
        self.assertEqual([(k.decode(), v.decode()) for k, v in self.app.scope["headers"]],
                         headers)
        boom.assert_not_called()

    def test_query_key_device_key_is_stripped_and_moved_to_a_header(self):
        # Fix round 3: uvicorn's access log prints the (outermost, shared)
        # scope's query string, so the master must NEVER be written there.
        # The master rides in x-litellm-api-key, which litellm 1.99's
        # get_api_key reads first on every route, generateContent included.
        token = self.mint()
        query = ("alt=sse&key=%s&x=%%2F1" % token).encode()
        scope, sent, _ = drive(self.mw(), "/v1/chat/completions", (), query=query)
        self.assertEqual(reply(sent)[0], 200)
        for label, seen in (("downstream", self.app.scope), ("original", scope)):
            with self.subTest(scope=label):
                self.assertEqual(seen["query_string"], b"alt=sse&x=%2F1")
                self.assertNotIn(MASTER.encode(), seen["query_string"])
                self.assertNotIn(token.encode(), seen["query_string"])
        self.assertEqual(self.app.headers()[b"x-litellm-api-key"], MASTER.encode())
        self.assertEqual(scope["ferry.key"], "laptop")
        # The same key in a header and in ?key= is one credential.
        scope, _, _ = drive(self.mw(), "/v1/chat/completions", bearer(token),
                            query=("key=" + token).encode())
        self.assertEqual(self.app.scope["query_string"], b"")
        self.assertEqual(scope["query_string"], b"")
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer " + MASTER.encode())
        self.assertEqual(self.app.headers()[b"x-litellm-api-key"], MASTER.encode())
        # x-litellm-api-key already present is rewritten, never duplicated.
        drive(self.mw(), "/v1/chat/completions", [("x-litellm-api-key", token)],
              query=("key=" + token).encode())
        names = [bytes(k).lower() for k, _ in self.app.scope["headers"]]
        self.assertEqual(names.count(b"x-litellm-api-key"), 1)

    def test_query_key_is_stripped_without_a_master(self):
        token = self.mint()
        with mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
            scope, sent, _ = drive(self.mw(), "/v1/chat/completions", (),
                                   query=("key=%s&alt=sse" % token).encode())
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(self.app.scope["query_string"], b"alt=sse")
        self.assertNotIn(b"x-litellm-api-key", self.app.headers())

    def test_a_refused_query_key_is_stripped_from_the_logged_scope(self):
        # A refused request never reaches litellm, but uvicorn still logs the
        # scope's query string: the plaintext device key must not be in it.
        _, gone = K.add("gone")
        K.revoke("gone")
        scope, sent, _ = drive(self.mw(), "/v1beta/models/f:generateContent", (),
                               query=("alt=sse&key=" + gone).encode())
        self.assertEqual(reply(sent)[0], 401)
        self.assertEqual(scope["query_string"], b"alt=sse")

    def test_query_key_that_disagrees_or_is_revoked_is_refused(self):
        token = self.mint()
        _, other = K.add("other")
        _, gone = K.add("gone")
        K.revoke("gone")
        self.refused(bearer(token), query=("key=" + other).encode())
        self.refused(bearer(MASTER), query=("key=" + token).encode())
        self.refused((), query=("key=%s&key=%s" % (token, other)).encode())
        self.refused((), query=("key=" + gone).encode())
        self.assertEqual(self.app.calls, 0)

    def test_duplicate_query_key_with_an_fk_word_is_refused(self):
        token = self.mint()
        self.refused((), query=("key=%s&key=%s" % (token, token)).encode())
        self.refused(bearer(token), query=("key=%s&key=%s" % (token, token)).encode())
        self.assertEqual(self.app.calls, 0)
        # Without an fk- word a duplicate ?key= is litellm's business, as today.
        _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(MASTER),
                           query=b"key=sk-a&key=sk-a")
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(self.app.scope["query_string"], b"key=sk-a&key=sk-a")

    def test_x_litellm_api_key_accepts_the_bearer_form(self):
        # litellm strips "Bearer " from x-litellm-api-key, so this is one key.
        token = self.mint()
        scope, sent, _ = drive(self.mw(), "/v1/chat/completions",
                               [("x-litellm-api-key", "Bearer " + token)])
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(scope["ferry.key"], "laptop")
        self.assertEqual(self.app.headers()[b"x-litellm-api-key"], MASTER.encode())
        _, sent, _ = drive(self.mw(), "/v1/chat/completions",
                           [("x-litellm-api-key", "Bearer " + token)] + bearer(token))
        self.assertEqual(reply(sent)[0], 200)
        scope, sent, _ = drive(self.mw(), "/v1/chat/completions",
                               [("x-litellm-api-key", "Bearer " + MASTER)])
        self.assertEqual(scope["ferry.key"], "master")

    def test_credential_headers_follow_litellm_precedence(self):
        self.assertEqual(FF.CREDENTIAL_HEADERS,
                         (b"x-litellm-api-key", b"authorization", b"api-key",
                          b"x-api-key", b"x-goog-api-key",
                          b"ocp-apim-subscription-key"))

    def test_a_non_fk_query_key_is_untouched(self):
        scope, sent, send = drive(self.mw(), "/v1/chat/completions", bearer(MASTER),
                                  query=b"key=sk-foo")
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(self.app.scope["query_string"], b"key=sk-foo")
        self.assertIs(self.app.send, send)
        self.assertEqual(scope["ferry.key"], "master")


class TestDeviceKeySpelling(KeyFrontCase):
    """Fix round 2: quotes and case do not hide a device key."""

    def test_quoted_and_upper_case_keys_are_presented(self):
        token = self.mint()
        for label, headers in {
                "quoted bearer": [("authorization", 'Bearer "%s"' % token)],
                "quoted x-api-key": [("x-api-key", "'%s'" % token)],
                "upper case": bearer(token.upper()),
                "quoted scheme-less": [("authorization", '"%s"' % token)]}.items():
            with self.subTest(label=label):
                scope, sent, _ = drive(self.mw(), "/v1/chat/completions", headers)
                self.assertEqual(reply(sent)[0], 200)
                self.assertEqual(scope["ferry.key"], "laptop")
                self.assertNotIn(token.lower().encode(),
                                 b"".join(v for _, v in self.app.scope["headers"]).lower())

    def test_quoted_or_upper_case_revoked_key_is_refused(self):
        _, token = K.add("gone")
        K.revoke("gone")
        for headers in (bearer('"%s"' % token), bearer(token.upper()),
                        [("x-api-key", token.upper())]):
            with self.subTest(headers=headers):
                _, sent, _ = drive(self.mw(), "/v1/chat/completions", headers)
                self.assertEqual(reply(sent)[0], 401)
        self.assertEqual(self.app.calls, 0)

    def test_upper_case_fk_beside_another_credential_is_ambiguous(self):
        token = self.mint()
        _, sent, _ = drive(self.mw(), "/v1/chat/completions",
                           bearer(MASTER) + [("x-api-key", token.upper())])
        self.assertEqual(reply(sent)[0], 401)


class TestEmptyCredentialHeaders(KeyFrontCase):
    def test_an_empty_credential_header_beside_a_device_key_is_ignored(self):
        token = self.mint()
        for name in ("x-api-key", "x-litellm-api-key", "api-key"):
            with self.subTest(header=name):
                scope, sent, _ = drive(self.mw(), "/v1/chat/completions",
                                       bearer(token) + [(name, "")])
                self.assertEqual(reply(sent)[0], 200)
                self.assertEqual(scope["ferry.key"], "laptop")
                self.assertNotIn(token.encode(),
                                 b"".join(v for _, v in self.app.scope["headers"]))


class TestDevicePathAllowlist(KeyFrontCase):
    """Round 4: a valid device key is admitted only on client routes; the
    credential swap must not hand it litellm's admin routes at master power."""

    def test_admin_and_unlisted_routes_are_403_and_never_reach_litellm(self):
        token = self.mint()
        for path in ("/key/generate", "/config/update", "/v1/audio/transcriptions",
                     "/model/new", "/user/new", "/health", "/v1/ferry/reorder",
                     "/v1/chat/completionsX", "/v1/modelsX", "/v1beta/models/x:embedContent",
                     "/v1/realtime/client_secrets"):
            with self.subTest(path=path):
                _, sent, _ = drive(self.mw(), path, bearer(token))
                status, _, doc = reply(sent)
                self.assertEqual(status, 403)
                self.assertEqual(doc["error"]["type"], "invalid_request_error")
                self.assertEqual(doc["error"]["code"], "route_not_allowed")
                self.assertIn("device keys may not call this route", doc["error"]["message"])
        self.assertEqual(self.app.calls, 0)

    def test_anthropic_family_403_shape(self):
        # GET /v1/messages is not a client route (only POST is).
        token = self.mint()
        _, sent, _ = drive(self.mw(), "/v1/messages", bearer(token), method="GET")
        status, _, doc = reply(sent)
        self.assertEqual(status, 403)
        self.assertEqual(doc, {"type": "error", "error": {
            "type": "permission_error", "message": "device keys may not call this route"}})
        self.assertEqual(self.app.calls, 0)

    def test_client_routes_are_admitted(self):
        token = self.mint()
        for path, method in (("/v1/chat/completions", "POST"), ("/chat/completions", "POST"),
                             ("/v1/responses", "POST"), ("/v1/messages", "POST"),
                             ("/v1/messages/count_tokens", "POST"), ("/v1/embeddings", "POST"),
                             ("/v1/models", "GET"), ("/models", "GET"),
                             ("/v1/models/flash", "GET"),
                             ("/v1/responses/resp_1", "GET"), ("/v1/responses/resp_1", "DELETE"),
                             ("/v1/responses/resp_1/input_items", "GET"),
                             ("/v1/responses/resp_1/cancel", "POST"),
                             ("/v1/responses/compact", "POST"),
                             ("/health/liveliness", "GET"), ("/health/liveness", "GET")):
            with self.subTest(path=path):
                scope, sent, _ = drive(self.mw(), path, bearer(token), method=method)
                self.assertEqual(reply(sent)[0], 200)
                self.assertEqual(scope["ferry.key"], "laptop")

    def assert_route_refused(self, token, path, method, scope_type="http"):
        _, sent, _ = drive(self.mw(), path, bearer(token), method=method,
                           scope_type=scope_type)
        if scope_type == "websocket":
            self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        else:
            status, _, doc = reply(sent)
            self.assertEqual(status, 403)
            self.assertEqual(doc["error"]["message"], FF.KEY_ROUTE_REFUSED)
            if FF.is_anthropic_path(path):
                self.assertEqual(doc["error"]["type"], "permission_error")
            else:
                self.assertEqual(doc["error"]["code"], "route_not_allowed")

    def test_param_first_litellm_routes_are_403(self):
        # Round-5 repros: litellm routes whose FIRST segment is a path param
        # (/{provider}/v1/files*, /{provider}/v1/batches*, /{mcp_server_name}/…)
        # took a client-looking first segment and reached files, batches and
        # MCP OAuth at master power.
        token = self.mint()
        for path, method in (
                ("/models/v1/files", "GET"), ("/models/v1/files", "POST"),
                ("/models/v1/files/x/content", "GET"), ("/models/v1/files/x", "DELETE"),
                ("/models/v1/batches", "GET"), ("/models/v1/batches", "POST"),
                ("/models/v1/batches/x/cancel", "POST"),
                ("/messages/v1/files", "GET"), ("/responses/v1/files", "GET"),
                ("/embeddings/v1/files", "GET"), ("/completions/v1/batches", "GET"),
                ("/chat/completions/v1/files", "GET"), ("/v1/models/v1/files", "GET"),
                ("/messages/v1/batches", "POST"),
                ("/models/mcp", "POST"), ("/messages/mcp", "POST"),
                ("/models/token", "POST"), ("/responses/token", "POST"),
                ("/models/register", "POST"), ("/responses/register", "POST"),
                ("/messages/authorize", "GET"), ("/models/token", "GET"),
                ("/models/v1/files/x:generateContent", "GET"),
                ("/models/v1/files/x:generateContent", "DELETE"),
                ("/models/v1/files/x:generateContent", "POST"),
                ("/models/v1/batches/x:countTokens", "GET")):
            with self.subTest(method=method, path=path):
                self.assert_route_refused(token, path, method)
        self.assertEqual(self.app.calls, 0)

    def test_bare_forms_that_collide_with_param_routes_are_403(self):
        # Only the /v1 (/v1beta) form is admitted where the bare form shares a
        # shape with /{mcp_server_name}/{authorize,token,…}.
        token = self.mint()
        for path, method in (("/models/flash", "GET"),
                             ("/models/flash:generateContent", "POST"),
                             ("/responses/resp_1", "GET"), ("/responses/resp_1", "DELETE"),
                             ("/responses/resp_1/cancel", "POST"),
                             ("/responses/resp_1/input_items", "GET")):
            with self.subTest(method=method, path=path):
                self.assert_route_refused(token, path, method)
        self.assertEqual(self.app.calls, 0)

    def test_unused_bare_forms_are_403(self):
        # These do not collide, but no client calls them: bare /messages has
        # no litellm route at all, and /responses/compact has a /v1 twin.
        token = self.mint()
        for path in ("/responses/compact", "/messages", "/messages/count_tokens"):
            with self.subTest(path=path):
                self.assert_route_refused(token, path, "POST")
        self.assertEqual(self.app.calls, 0)

    def test_the_method_is_part_of_the_rule(self):
        token = self.mint()
        for path, method in (("/v1/chat/completions", "GET"), ("/v1/chat/completions", "DELETE"),
                             ("/v1/models", "POST"), ("/v1/models/flash", "DELETE"),
                             ("/v1beta/models/flash:generateContent", "GET"),
                             ("/v1beta/models/flash:generateContent", "DELETE"),
                             ("/v1/responses", "GET"), ("/v1/responses/resp_1", "PUT"),
                             ("/v1/messages", "GET"), ("/health/liveliness", "POST"),
                             ("/v1/chat/completions", "HEAD"), ("/v1/models", "OPTIONS"),
                             ("/v1/ferry/fleet", "DELETE")):
            with self.subTest(method=method, path=path):
                self.assert_route_refused(token, path, method)
        self.assertEqual(self.app.calls, 0)

    def test_rules_are_anchored_to_the_whole_path(self):
        token = self.mint()
        for path, method in (("/v1/chat/completions/x", "POST"),
                             ("/v1/messages/x", "POST"), ("/v1/models/a/b", "GET"),
                             ("/v1/responses/a/b", "GET"),
                             ("/v1beta/models/a/b:generateContent", "POST"),
                             ("/v1beta/models/:generateContent", "POST"),
                             ("/v1/models/", "GET"), ("/v1/responses/", "GET")):
            with self.subTest(method=method, path=path):
                self.assert_route_refused(token, path, method)
        self.assertEqual(self.app.calls, 0)

    def test_every_websocket_is_refused(self):
        token = self.mint()
        for path in ("/models/v1/files", "/v1/chat/completions", "/openai/v1/realtime",
                     "/v1/realtime/x"):
            with self.subTest(path=path):
                self.assert_route_refused(token, path, "GET", scope_type="websocket")

    def test_unmetered_routes_refuse_device_keys(self):
        # Round 6: Gemini-native generate and every websocket skip _key_admit
        # (no lane check, RPM, budget or metering), so a device key there
        # would bypass its limits. Refused until metering exists.
        token = self.mint()
        for action in ("generateContent", "streamGenerateContent", "countTokens"):
            with self.subTest(action=action):
                self.assert_route_refused(token, "/v1beta/models/flash:" + action, "POST")
        for path in ("/v1/realtime", "/realtime", "/v1/responses", "/responses"):
            with self.subTest(websocket=path):
                self.assert_route_refused(token, path, "GET", scope_type="websocket")
        self.assertEqual(self.app.calls, 0)

    def test_limited_key_cannot_bypass_limits_on_gemini_native(self):
        # The Task 10 probe: lanes=[heavy], rpm=1, model flash. Chat was
        # 403/403/403 and generateContent was 200/200/200.
        _, token = K.add("probe", lanes=["heavy"], rpm=1)
        body = json.dumps({"model": "flash", "messages": []}).encode()
        statuses = [reply(drive(self.mw(), "/v1beta/models/flash:generateContent",
                                bearer(token), body=body)[1])[0] for _ in range(3)]
        self.assertEqual(statuses, [403, 403, 403])
        self.assertEqual(self.app.calls, 0)

    def test_master_still_reaches_gemini_native_and_realtime(self):
        for path, typ in (("/v1beta/models/flash:generateContent", "http"),
                          ("/v1/realtime", "websocket")):
            with self.subTest(path=path):
                scope, sent, send = drive(self.mw(), path, bearer(MASTER),
                                          method="POST" if typ == "http" else "GET",
                                          scope_type=typ)
                self.assertIs(self.app.send, send)
                self.assertEqual(scope["ferry.key"], "master")

    def test_gemini_query_key_is_refused_and_stripped(self):
        # The ?key= stripping stays: a refused key is never logged.
        token = self.mint()
        scope, sent, _ = drive(self.mw(), "/v1beta/models/flash:generateContent", (),
                               query=("alt=sse&key=" + token).encode())
        self.assertEqual(reply(sent)[0], 403)
        self.assertEqual(scope["query_string"], b"alt=sse")
        self.assertEqual(self.app.calls, 0)

    def test_fleet_route_is_admitted(self):
        token = self.mint()
        path = os.path.join(self.dir, "fleets.json")
        with open(path, "w") as fh:
            json.dump({"default": "domestic", "clients": {}}, fh)
        state = FF.FleetState(path, FLEETS)
        _, sent, _ = drive(self.mw(state, FLEETS), FF.FLEET_PATH, bearer(token), method="GET")
        self.assertEqual(reply(sent)[0], 200)

    def test_unknown_websocket_is_closed(self):
        token = self.mint()
        _, sent, _ = drive(self.mw(), "/v1/admin-socket", bearer(token), method="GET",
                           scope_type="websocket")
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])

    def test_master_and_bare_on_admin_routes_pass_untouched(self):
        boom = mock.Mock(side_effect=AssertionError("key store touched"))
        with mock.patch.object(FF, "_key_cache", boom):
            for headers, label in ((bearer(MASTER), "master"), ((), "")):
                with self.subTest(label=label):
                    scope, sent, send = drive(self.mw(), "/key/generate", headers)
                    self.assertEqual(reply(sent)[0], 200)
                    self.assertIs(self.app.send, send)
                    self.assertEqual(scope["ferry.key"], label)
                    self.assertEqual([(k.decode(), v.decode())
                                      for k, v in self.app.scope["headers"]], list(headers))
        boom.assert_not_called()

    def test_revoked_key_on_an_admin_route_is_still_401(self):
        _, gone = K.add("gone")
        K.revoke("gone")
        _, sent, _ = drive(self.mw(), "/key/generate", bearer(gone))
        self.assertEqual(reply(sent)[0], 401)


class TestEntryIsolation(KeyFrontCase):
    def test_key_entry_is_safe_against_downstream_mutation(self):
        token = self.mint(lanes=["flash"])
        mw = self.mw()
        scope, _, _ = drive(mw, "/v1/models", bearer(token), method="GET")
        scope["ferry.key_entry"]["lanes"].append("heavy")
        scope["ferry.key_entry"]["rpm"] = 999
        scope, _, _ = drive(mw, "/v1/models", bearer(token), method="GET")
        self.assertEqual(scope["ferry.key_entry"]["lanes"], ["flash"])
        self.assertIsNone(scope["ferry.key_entry"]["rpm"])


class TestIdentity(KeyFrontCase):
    def setUp(self):
        super().setUp()
        self.state_path = os.path.join(self.dir, "fleets.json")
        with open(self.state_path, "w") as fh:
            json.dump({"default": "domestic", "clients": {}}, fh)
        self.state = FF.FleetState(self.state_path, FLEETS)

    def test_fleet_get_names_the_key_not_the_claimed_client(self):
        token = self.mint("mbp")
        headers = bearer(token) + [("x-ferry-client", "someone-else")]
        _, sent, _ = drive(self.mw(self.state, FLEETS), FF.FLEET_PATH, headers, method="GET")
        status, _, doc = reply(sent)
        self.assertEqual(status, 200)
        self.assertEqual(doc["you"], "mbp")

    def test_sticky_selection_is_stored_under_the_key_name(self):
        token = self.mint("mbp")
        _, sent, _ = drive(self.mw(self.state, FLEETS), FF.FLEET_PATH, bearer(token),
                           body=json.dumps({"fleet": "international"}).encode())
        self.assertEqual(reply(sent)[0], 200)
        with open(self.state_path) as fh:
            self.assertEqual(json.load(fh)["clients"], {"mbp": "international"})

    def test_caller_identity_without_a_key_is_unchanged(self):
        scope = {"client": ("100.64.0.9", 1)}
        self.assertEqual(FF.caller_identity(scope, {b"x-ferry-client": b"named"}), "named")
        self.assertEqual(FF.caller_identity({"client": ("127.0.0.1", 1)}, {}), "host")


class TestErrorBodies(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(FF.key_error_body("/v1/chat/completions", 403, "no"),
                         {"error": {"message": "no", "type": "invalid_request_error",
                                    "param": None, "code": "model_not_allowed"}})
        self.assertEqual(FF.key_error_body("/v1/messages/count_tokens", 429, "slow"),
                         {"type": "error", "error": {"type": "rate_limit_error",
                                                     "message": "slow"}})
        self.assertEqual(FF.key_error_body("/v1/responses", 429, "q", code="insufficient_quota",
                                           openai_type="insufficient_quota")["error"]["type"],
                         "insufficient_quota")


import ferry_keys_usage as U  # noqa: E402


class TestLimits(KeyFrontCase):
    def usage(self):
        return U.Usage(self.db)

    def chat(self, token, model="flash", mw=None, **extra):
        body = json.dumps(dict({"model": model, "messages": []}, **extra)).encode()
        return drive(mw or self.mw(), "/v1/chat/completions", bearer(token), body=body)

    def test_lane_restricted_key_gets_403_for_another_lane(self):
        token = self.mint(lanes=["flash"])
        status, _, doc = reply(self.chat(token, "heavy")[1])
        self.assertEqual(status, 403)
        self.assertEqual(doc["error"]["code"], "model_not_allowed")
        self.assertEqual(self.app.calls, 0)
        self.assertEqual(reply(self.chat(token, "flash")[1])[0], 200)

    def test_fleet_qualified_lane_limit_sees_the_resolved_lane(self):
        state_path = os.path.join(self.dir, "fleets.json")
        with open(state_path, "w") as fh:
            json.dump({"default": "domestic", "clients": {}}, fh)
        mw = self.mw(FF.FleetState(state_path, FLEETS), FLEETS)
        token = self.mint(lanes=["international.flash"])
        self.assertEqual(reply(self.chat(token, "flash", mw=mw)[1])[0], 403)
        self.assertEqual(reply(self.chat(token, "international.flash", mw=mw)[1])[0], 200)

    def test_unparseable_body_on_a_lane_restricted_key_is_403(self):
        token = self.mint(lanes=["flash"])
        _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(token), body=b"{not json")
        self.assertEqual(reply(sent)[0], 403)
        self.assertEqual(self.app.calls, 0)

    def test_rpm_limit_is_429_with_retry_after_in_both_shapes(self):
        token = self.mint(rpm=1)
        self.assertEqual(reply(self.chat(token)[1])[0], 200)
        status, headers, doc = reply(self.chat(token)[1])
        self.assertEqual(status, 429)
        self.assertTrue(1 <= int(headers[b"retry-after"]) <= 60)
        self.assertEqual(doc["error"]["code"], "rate_limit_exceeded")
        _, sent, _ = drive(self.mw(), "/v1/messages", [("x-api-key", token)],
                           body=b'{"model":"flash","messages":[]}')
        status, headers, doc = reply(sent)
        self.assertEqual((status, doc["error"]["type"]), (429, "rate_limit_error"))
        self.assertIn(b"retry-after", headers)
        self.assertEqual(self.app.calls, 1)

    def test_budget_is_429_insufficient_quota(self):
        token = self.mint(budget_tokens=10)
        self.assertEqual(reply(self.chat(token)[1])[0], 200)  # charges 12
        status, headers, doc = reply(self.chat(token)[1])
        self.assertEqual(status, 429)
        self.assertEqual(doc["error"]["code"], "insufficient_quota")
        self.assertEqual(doc["error"]["type"], "insufficient_quota")
        self.assertGreater(int(headers[b"retry-after"]), 0)

    def test_accounting_works_with_the_tap_off(self):
        self.assertEqual(os.environ["FERRY_EVENTS"], "0")
        token = self.mint()
        self.chat(token)
        self.assertEqual(self.usage().month_tokens("laptop"), 12)  # 7 in + 5 out
        self.assertEqual(self.usage().minute_requests("laptop"), 1)

    def test_reasoning_tokens_are_not_double_counted(self):
        self.app.payload = json.dumps({"usage": {
            "prompt_tokens": 7, "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 4}}}).encode()
        self.chat(self.mint())
        self.assertEqual(self.usage().month_tokens("laptop"), 12)

    def test_a_refused_request_is_not_counted(self):
        token = self.mint(rpm=1)
        self.chat(token)
        self.chat(token)
        self.assertEqual(self.usage().month_tokens("laptop"), 12)
        self.assertEqual(self.usage().minute_requests("laptop"), 1)

    def test_include_usage_is_injected_for_device_keys_only(self):
        self.chat(self.mint(), stream=True)
        self.assertEqual(json.loads(self.app.body)["stream_options"], {"include_usage": True})
        body = json.dumps({"model": "flash", "messages": [], "stream": True}).encode()
        drive(self.mw(), "/v1/chat/completions", bearer(MASTER), body=body)
        self.assertEqual(self.app.body, body)

    def test_master_request_still_gets_the_original_send(self):
        _, _, send = drive(self.mw(), "/v1/chat/completions", bearer(MASTER))
        self.assertIs(self.app.send, send)
        # CONTROL: a device key does get the metering wrapper.
        _, _, send = self.chat(self.mint())
        self.assertIsNot(self.app.send, send)

    def test_unusable_usage_db_is_503_for_keys_and_invisible_to_the_master(self):
        token = self.mint()
        with mock.patch.dict(os.environ, {"FERRY_KEYS_DB": self.dir}), \
                mock.patch("sys.stderr"):
            status, _, doc = reply(self.chat(token)[1])
            self.assertEqual(status, 503)
            self.assertEqual(doc["error"]["code"], "key_store_unavailable")
            _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(MASTER))
            self.assertEqual(reply(sent)[0], 200)

    def test_accounting_failure_never_breaks_the_response(self):
        token = self.mint()
        with mock.patch.object(U.Usage, "add_tokens",
                               side_effect=U.UsageError("disk full")), \
                mock.patch("sys.stderr") as err:
            _, sent, _ = self.chat(token)
        status, _, _ = reply(sent)
        self.assertEqual(status, 200)
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertEqual(body, USAGE_BODY)
        self.assertTrue(any("disk full" in str(c.args[0]) for c in err.write.call_args_list if c.args))


class TestEventKeyField(KeyFrontCase):
    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.dir, "events.jsonl")
        os.environ["FERRY_EVENTS"] = "1"   # restored by the patch.dict in the base
        FF.reset_tap(self.events)
        self.addCleanup(FF.reset_tap, None)

    def test_record_names_the_key(self):
        token = self.mint()
        for headers in (bearer(token), bearer(MASTER), ()):
            drive(self.mw(), "/v1/chat/completions", headers)
        FF.tap_flush()
        with open(self.events) as fh:
            keys = [json.loads(line)["key"] for line in fh if line.strip()]
        self.assertEqual(keys, ["laptop", "master", ""])


class TestUsageStoreResilience(KeyFrontCase):
    """Task 4 seat additions: the usage DB is touched off the event loop,
    and a deleted DB heals on the next request instead of at restart."""

    def chat(self, token):
        return drive(self.mw(), "/v1/chat/completions", bearer(token),
                     body=b'{"model":"flash","messages":[]}')

    def test_admit_and_charge_run_off_the_event_loop(self):
        seen = []
        real_admit, real_add = U.Usage.admit, U.Usage.add_tokens

        def probe(real):
            def wrapped(*args, **kwargs):
                try:
                    asyncio.get_running_loop()
                    seen.append("loop")
                except RuntimeError:
                    seen.append("thread")
                return real(*args, **kwargs)
            return wrapped

        with mock.patch.object(U.Usage, "admit", probe(real_admit)), \
                mock.patch.object(U.Usage, "add_tokens", probe(real_add)):
            self.assertEqual(reply(self.chat(self.mint())[1])[0], 200)
        self.assertEqual(seen, ["thread", "thread"])

    def test_a_deleted_usage_db_heals_on_the_next_request(self):
        token = self.mint()
        self.assertEqual(reply(self.chat(token)[1])[0], 200)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)
        with mock.patch("sys.stderr"):
            # The cached Usage believed its schema existed ("no such table"):
            # the first request after the deletion is refused, fail-closed...
            self.assertEqual(reply(self.chat(token)[1])[0], 503)
        # ...and the next one is served by a fresh Usage that rebuilt it.
        self.assertEqual(reply(self.chat(token)[1])[0], 200)
        self.assertEqual(U.Usage(self.db).minute_requests("laptop"), 1)
        self.assertEqual(U.Usage(self.db).month_tokens("laptop"), 12)

    def test_the_heal_probe_can_fail(self):
        # Control for the test above: without dropping the cached instance
        # the deleted DB keeps failing ("no such table").
        token = self.mint()
        self.assertEqual(reply(self.chat(token)[1])[0], 200)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)
        with mock.patch.object(FF, "_usage_forget", lambda usage: None), \
                mock.patch("sys.stderr"):
            self.assertEqual(reply(self.chat(token)[1])[0], 503)
            self.assertEqual(reply(self.chat(token)[1])[0], 503)


SSE_CHUNKS = [b'data: {"id":"x","choices":[{"delta":{"content":"hi"}}]}\n\n',
              b'data: {"id":"x","choices":[],"usage":{"prompt_tokens":7,'
              b'"completion_tokens":5}}\n\n',
              b"data: [DONE]\n\n"]


class DisconnectingStream:
    """litellm behind uvicorn (ASGI spec 2.3) + Starlette's StreamingResponse:
    the body streams in one task while a second task listens for
    `http.disconnect`, and the stream task is CANCELLED as soon as the
    listener returns. uvicorn's receive() returns http.disconnect the moment
    the final body has been sent, so every streamed response ends with that
    cancel landing on whatever the send chain is awaiting."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        while True:
            msg = await receive()
            if not msg.get("more_body"):
                break

        async def stream():
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            for chunk in SSE_CHUNKS:
                await send({"type": "http.response.body", "body": chunk,
                            "more_body": True})
            await send({"type": "http.response.body", "body": b"",
                        "more_body": False})

        async def listen():
            while (await receive())["type"] != "http.disconnect":
                pass

        tasks = {asyncio.ensure_future(stream()), asyncio.ensure_future(listen())}
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def drive_disconnecting(mw, token, busy=0.3):
    """uvicorn-faithful transport: after the final body, receive() returns
    http.disconnect. The default executor has ONE worker, and it is kept busy
    for `busy` seconds from the final body on (concurrent requests' DB hops),
    so a charge queued at stream end is still waiting when the cancel lands."""
    import concurrent.futures
    import time as _time
    body = json.dumps({"model": "flash", "messages": [], "stream": True}).encode()
    scope = {"type": "http", "path": "/v1/chat/completions", "method": "POST",
             "client": ("100.64.0.9", 50000), "query_string": b"",
             "asgi": {"version": "3.0", "spec_version": "2.3"},
             "headers": [(b"authorization", ("Bearer " + token).encode())]}
    sent = []

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(1))
        done = asyncio.Event()
        state = {"read": False}

        async def receive():
            if not state["read"]:
                state["read"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                loop.run_in_executor(None, _time.sleep, busy)
                done.set()

        await mw(scope, receive, send)

    asyncio.run(main())
    return sent


class TestStreamingChargeSurvivesDisconnect(KeyFrontCase):
    """Fix round 1 (review Critical): a completed streamed response must be
    charged even though the task that streamed it is cancelled at stream end."""

    def test_streamed_response_is_charged_despite_the_disconnect_cancel(self):
        token = self.mint()
        app = DisconnectingStream()
        mw = FF.LaneCatalogueFilter(app, frozenset())
        sent = drive_disconnecting(mw, token)
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(app.calls, 1)
        self.assertEqual(U.Usage(self.db).month_tokens("laptop"), 12)

    def test_the_charge_is_shielded_from_a_cancelled_request_task(self):
        # The whole request task is cancelled while the charge is queued behind
        # a busy worker: the job still runs once the worker frees up.
        import concurrent.futures
        import time as _time
        token = self.mint()
        mw = self.mw()

        async def main():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(1))
            scope = {"type": "http", "path": "/v1/chat/completions", "method": "POST",
                     "client": ("100.64.0.9", 1), "query_string": b"",
                     "headers": [(b"authorization", ("Bearer " + token).encode())]}
            finished = asyncio.Event()

            async def receive():
                return {"type": "http.request", "body": b'{"model":"flash"}',
                        "more_body": False}

            async def send(message):
                if message["type"] == "http.response.body" and not message.get("more_body"):
                    loop.run_in_executor(None, _time.sleep, 0.3)  # worker busy
                    finished.set()

            task = asyncio.ensure_future(mw(scope, receive, send))
            await finished.wait()
            task.cancel()   # lands on the first await after the final body
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(main())
        self.assertEqual(U.Usage(self.db).month_tokens("laptop"), 12)


class TestStreamUsageForMeteredRequests(KeyFrontCase):
    """Fix round 1 (review Minor a): wherever litellm 1.99 would stream a
    metered request WITHOUT a usage chunk, the front asks for one."""

    def send_body(self, path, doc, token=None):
        body = json.dumps(doc).encode()
        drive(self.mw(), path, bearer(token or self.mint()), body=body)
        return json.loads(self.app.body)

    def test_legacy_completions_stream_gets_include_usage(self):
        for name, path in (("v1", "/v1/completions"), ("bare", "/completions")):
            with self.subTest(path=path):
                out = self.send_body(path, {"model": "flash", "prompt": "x",
                                            "stream": True}, self.mint(name))
                self.assertEqual(out["stream_options"], {"include_usage": True})

    def test_non_dict_stream_options_is_replaced(self):
        out = self.send_body("/v1/chat/completions",
                             {"model": "flash", "messages": [], "stream": True,
                              "stream_options": "yes"})
        self.assertEqual(out["stream_options"], {"include_usage": True})

    def test_integer_stream_is_normalised_and_gets_include_usage(self):
        out = self.send_body("/v1/chat/completions",
                             {"model": "flash", "messages": [], "stream": 1})
        self.assertIs(out["stream"], True)
        self.assertEqual(out["stream_options"], {"include_usage": True})

    def test_responses_and_messages_are_left_alone(self):
        # Both report usage in-stream without being asked (response.completed,
        # message_start/message_delta), so their bodies are forwarded as sent.
        for name, path in (("resp", "/v1/responses"), ("msgs", "/v1/messages")):
            with self.subTest(path=path):
                doc = {"model": "flash", "input": "x", "stream": True}
                body = json.dumps(doc).encode()
                drive(self.mw(), path, bearer(self.mint(name)), body=body)
                self.assertEqual(self.app.body, body)

    def test_master_bodies_are_untouched(self):
        for doc in ({"model": "flash", "prompt": "x", "stream": True},
                    {"model": "flash", "messages": [], "stream": 1}):
            with self.subTest(doc=doc):
                body = json.dumps(doc).encode()
                drive(self.mw(), "/v1/completions", bearer(MASTER), body=body)
                self.assertEqual(self.app.body, body)


class TestAnthropicLaneRefusal(KeyFrontCase):
    def test_lane_refusal_on_messages_is_anthropic_shaped(self):
        token = self.mint(lanes=["flash"])
        _, sent, _ = drive(self.mw(), "/v1/messages", [("x-api-key", token)],
                           body=b'{"model":"heavy","messages":[]}')
        status, _, doc = reply(sent)
        self.assertEqual(status, 403)
        self.assertEqual(doc["type"], "error")
        self.assertEqual(doc["error"]["type"], "permission_error")
        self.assertIn("heavy", doc["error"]["message"])
        self.assertEqual(self.app.calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
