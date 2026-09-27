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

    def test_websocket_with_a_valid_key_is_rewritten(self):
        token = self.mint()
        drive(self.mw(), "/v1/realtime", bearer(token), method="GET",
              scope_type="websocket")
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer " + MASTER.encode())


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
        scope, sent, _ = drive(self.mw(), "/v1beta/models/flash:generateContent", (),
                               query=query)
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
            scope, sent, _ = drive(self.mw(), "/v1beta/models/f:generateContent", (),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
