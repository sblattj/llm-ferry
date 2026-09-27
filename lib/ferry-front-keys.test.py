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
          client=("100.64.0.9", 50000), scope_type="http"):
    scope = {"type": scope_type, "path": path, "method": method, "client": client,
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
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
        for headers, label in ((bearer(MASTER), "master"), ((), "")):
            with self.subTest(label=label):
                scope, _, send = drive(self.mw(), "/v1/chat/completions", headers)
                self.assertEqual(self.app.headers().get(b"authorization"),
                                 dict(headers).get("authorization", "").encode() or None)
                self.assertEqual(scope["ferry.key"], label)
                self.assertNotIn("ferry.key_entry", scope)
                self.assertIs(self.app.send, send)
        self.assertFalse(os.path.exists(self.keys))
        self.assertFalse(os.path.exists(self.db))

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
                    self.assertIn(reason if reason != "unknown" else "unknown",
                                  doc["error"]["message"])
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
