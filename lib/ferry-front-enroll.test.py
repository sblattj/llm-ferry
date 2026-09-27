#!/usr/bin/env python3
"""POST /v1/ferry/keys/enroll — how client-bootstrap.sh trades the master key
for a device key.

Run:  python3 lib/ferry-front-enroll.test.py

Drives LaneCatalogueFilter directly; FERRY_KEYS_FILE points at a temp dir.
"""
import asyncio
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
PATH = "/v1/ferry/keys/enroll"


class NeverApp:
    calls = 0

    async def __call__(self, scope, receive, send):
        NeverApp.calls += 1
        raise AssertionError("enroll must never reach litellm")


def post(body, headers=(), method="POST", client=("100.64.0.9", 50000)):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    scope = {"type": "http", "path": PATH, "method": method, "client": client,
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(FF.LaneCatalogueFilter(NeverApp(), frozenset())(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(payload)


def master():
    return [("authorization", "Bearer " + MASTER)]


class EnrollCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-front-enroll-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.keys = os.path.join(self.dir, "keys.json")
        patcher = mock.patch.dict(os.environ, {
            "FERRY_KEYS_FILE": self.keys,
            "FERRY_KEYS_DB": os.path.join(self.dir, "u.sqlite"),
            "LITELLM_MASTER_KEY": MASTER, "FERRY_EVENTS": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        FF._KEY_CACHE = None
        NeverApp.calls = 0

    def test_master_mints_a_key_that_then_authenticates(self):
        status, doc = post({"name": "MBP Work"}, master())
        self.assertEqual(status, 200)
        self.assertEqual(doc["name"], "mbp-work")
        self.assertRegex(doc["key"], r"^fk-mbp-work-[a-z2-7]{32}$")
        self.assertEqual(K.KeyCache().lookup(doc["key"])[0]["name"], "mbp-work")
        self.assertEqual(NeverApp.calls, 0)

    def test_taken_name_gets_a_suffix_without_replace(self):
        post({"name": "laptop"}, master())
        status, doc = post({"name": "laptop"}, master())
        self.assertEqual((status, doc["name"]), (200, "laptop-2"))

    def test_replace_rotates_in_place(self):
        _, first = post({"name": "laptop"}, master())
        status, second = post({"name": "laptop", "replace": True}, master())
        self.assertEqual((status, second["name"]), (200, "laptop"))
        self.assertEqual(len(K.load()["keys"]), 1)
        self.assertEqual(K.KeyCache().lookup(first["key"])[1], "unknown")

    def test_replace_keeps_the_existing_limits(self):
        K.add("laptop", rpm=7, lanes=["flash"])
        status, doc = post({"name": "laptop", "replace": True}, master())
        self.assertEqual((status, doc["name"]), (200, "laptop"))
        entry = K.load()["keys"][0]
        self.assertEqual((entry["rpm"], entry["lanes"]), (7, ["flash"]))

    def test_replace_must_be_a_real_boolean(self):
        post({"name": "laptop"}, master())
        _, doc = post({"name": "laptop", "replace": "yes"}, master())
        self.assertEqual(doc["name"], "laptop-2")

    def test_a_device_key_cannot_enroll(self):
        _, token = K.add("laptop")
        status, doc = post({"name": "evil"}, [("authorization", "Bearer " + token)])
        self.assertEqual(status, 401)
        self.assertIn("message", doc["error"])
        self.assertEqual([e["name"] for e in K.load()["keys"]], ["laptop"])
        self.assertEqual(NeverApp.calls, 0)

    def test_a_device_key_cannot_enroll_even_on_loopback_without_a_master(self):
        _, token = K.add("laptop")
        with mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
            status, _ = post({"name": "evil"}, [("authorization", "Bearer " + token)],
                             client=("127.0.0.1", 1))
        self.assertIn(status, (401, 403))
        self.assertEqual([e["name"] for e in K.load()["keys"]], ["laptop"])

    def test_a_scope_that_carries_a_key_entry_is_never_the_master(self):
        # Belt and braces: even a scope labelled "master" is refused when
        # authenticate() admitted it as a device key.
        scope = {"type": "http", "path": PATH, "method": "POST",
                 "client": ("100.64.0.9", 1), "headers": [],
                 FF.KEY_SCOPE: "master", FF.KEY_ENTRY_SCOPE: {"name": "master"}}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b'{"name": "x"}',
                    "more_body": False}

        async def send(message):
            sent.append(message)

        filt = FF.LaneCatalogueFilter(NeverApp(), frozenset())
        asyncio.run(filt._enroll(scope, receive, send))
        self.assertEqual(sent[0]["status"], 401)
        self.assertFalse(os.path.exists(self.keys))

    def test_no_or_wrong_credential_is_401(self):
        self.assertEqual(post({"name": "x"})[0], 401)
        self.assertEqual(post({"name": "x"}, [("authorization", "Bearer nope")])[0], 401)
        self.assertFalse(os.path.exists(self.keys))

    def test_without_a_master_only_loopback_may_enroll(self):
        with mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
            self.assertEqual(post({"name": "x"})[0], 403)
            status, doc = post({"name": "x"}, client=("127.0.0.1", 1))
            self.assertEqual((status, doc["name"]), (200, "x"))

    def test_bad_bodies_are_400(self):
        for body in (b"{nope", b"[]", {"name": 5}, {"name": "!!!"}, {}):
            with self.subTest(body=body):
                self.assertEqual(post(body, master())[0], 400)

    def test_reserved_names_are_400_not_500(self):
        for name in ("master", "HOST", " Master "):
            with self.subTest(name=name):
                status, doc = post({"name": name}, master())
                self.assertEqual(status, 400)
                self.assertIn("reserved", doc["error"]["message"])
                self.assertEqual(doc["error"]["type"], "invalid_request_error")
        self.assertFalse(os.path.exists(self.keys))

    def test_get_is_405(self):
        self.assertEqual(post(b"", master(), method="GET")[0], 405)

    def test_unwritable_store_is_503(self):
        with open(self.keys, "w") as fh:
            fh.write("{corrupt")
        with mock.patch("sys.stderr"):
            status, doc = post({"name": "x"}, master())
        self.assertEqual(status, 503)
        self.assertIn("message", doc["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
