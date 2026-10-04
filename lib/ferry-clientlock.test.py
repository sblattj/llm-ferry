#!/usr/bin/env python3
"""Stdlib unittest for the client lock: only the host may leave `domestic`.

Run:  python3 lib/ferry-clientlock.test.py

Every non-host caller is pinned to the domestic fleet. Three routes to another
fleet are refused with HTTP 403 (a fleet-qualified model name, the
X-Ferry-Fleet header, a sticky POST /v1/ferry/fleet), a bare lane means
`domestic.<lane>` whatever the sticky selection or the host-wide default says,
and a non-host JSON body gets `metadata.ferry_domestic_only = true`.

Who is the host is strict (ferry_front.is_host_caller): loopback peer, no
device key, no proxy / tailscale header, X-Ferry-Client absent or "host".
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

import ferry_front as FF  # noqa: E402
from ferry_front import LaneCatalogueFilter  # noqa: E402

FLEETS = {"domestic": {"heavy": "a", "medium": "b", "flash": "c", "super-flash": "d"},
          "international": {"heavy": "e", "medium": "f", "flash": "g", "super-flash": "h"}}

HOST = ("127.0.0.1", 5000)
LAN = ("100.64.1.2", 5000)
LOCK_MSG = ("this ferry host locks clients to the domestic fleet; "
            "'international' is host-only")


class BodyApp:
    def __init__(self):
        self.body = None
        self.scope = None
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scope = scope
        buf = b""
        while True:
            msg = await receive()
            buf += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        self.body = buf
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-lock-")
        self.state_path = os.path.join(self.dir, "fleets.json")
        self.write_state({"default": "domestic", "clients": {}})
        self.state = FF.FleetState(self.state_path, FLEETS)
        os.environ["FERRY_STRIP_HEADERS"] = "0"
        os.environ.pop("LITELLM_MASTER_KEY", None)

    def tearDown(self):
        os.environ.pop("FERRY_STRIP_HEADERS", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_state(self, doc):
        with open(self.state_path, "w") as handle:
            json.dump(doc, handle)
        st = os.stat(self.state_path)
        os.utime(self.state_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def drive(self, path, doc=None, raw=None, method="POST", client=HOST,
              headers=None, scope_extra=None):
        """Returns (status, response-json-or-bytes, app)."""
        if raw is None:
            raw = b"" if doc is None else json.dumps(doc).encode()
        app = BodyApp()
        mw = LaneCatalogueFilter(app, frozenset(), fleets=FLEETS, state=self.state)
        scope = {"type": "http", "path": path, "method": method, "client": client,
                 "headers": list(headers or [])}
        scope.update(scope_extra or {})
        msgs = [{"type": "http.request", "body": raw, "more_body": False}]
        sent = []

        async def receive():
            return msgs.pop(0) if msgs else {
                "type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        asyncio.run(mw(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        payload = b"".join(m.get("body", b"") for m in sent
                           if m["type"] == "http.response.body")
        try:
            payload = json.loads(payload)
        except Exception:
            pass
        return start["status"], payload, app

    def chat(self, model, **kw):
        return self.drive("/v1/chat/completions", {"model": model}, **kw)


class TestIsHostCaller(unittest.TestCase):
    def test_host_is_loopback_with_no_proxy_headers(self):
        self.assertTrue(FF.is_host_caller({"client": HOST}, {}))
        self.assertTrue(FF.is_host_caller({"client": ("::1", 1)}, {}))
        self.assertTrue(FF.is_host_caller(
            {"client": HOST}, {b"x-ferry-client": b"host"}))

    def test_everything_else_is_a_client(self):
        cases = {
            "peer ip": ({"client": LAN}, {}),
            "no peer": ({}, {}),
            "x-forwarded-for": ({"client": HOST}, {b"x-forwarded-for": b"100.1.1.1"}),
            "forwarded": ({"client": HOST}, {b"forwarded": b"for=1.2.3.4"}),
            "x-real-ip": ({"client": HOST}, {b"x-real-ip": b"100.1.1.1"}),
            "tailscale-user-login": ({"client": HOST},
                                     {b"tailscale-user-login": b"a@b.c"}),
            "tailscale-headers-info": ({"client": HOST},
                                       {b"tailscale-headers-info": b"x"}),
            "named client": ({"client": HOST}, {b"x-ferry-client": b"some-client"}),
            "device key": ({"client": HOST, FF.KEY_ENTRY_SCOPE: {"name": "k"},
                            FF.KEY_SCOPE: "k"}, {}),
        }
        for name, (scope, headers) in cases.items():
            with self.subTest(name):
                self.assertFalse(FF.is_host_caller(scope, headers))

    def test_caller_identity_is_unchanged(self):
        # sticky bookkeeping still counts a headerless relayed request as host
        self.assertEqual(FF.caller_identity(
            {"client": HOST}, {b"x-forwarded-for": b"1.1.1.1"}), "host")

    def test_the_lock_fleet_is_guard_restricted(self):
        import ferry_fleet_guard
        self.assertIn(FF.LOCK_FLEET, ferry_fleet_guard.RESTRICTED_FLEETS)
        self.assertEqual(FF.LOCK_FLEET, "domestic")


class TestHostIsFree(Base):
    def test_host_can_name_international_explicitly(self):
        status, _, app = self.chat("international.heavy")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(app.body)["model"], "international.heavy")

    def test_host_can_use_the_header(self):
        status, _, app = self.chat(
            "heavy", headers=[(b"x-ferry-fleet", b"international")])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(app.body)["model"], "international.heavy")

    def test_host_sticky_international_applies_and_can_be_posted(self):
        status, doc, _ = self.drive(FF.FLEET_PATH, {"fleet": "international"})
        self.assertEqual(status, 200)
        self.assertEqual(doc["fleet"], "international")
        _, _, app = self.chat("heavy")
        self.assertEqual(json.loads(app.body)["model"], "international.heavy")

    def test_host_body_has_no_marker(self):
        _, _, app = self.chat("heavy")
        self.assertNotIn("metadata", json.loads(app.body))


class TestClientsAreLocked(Base):
    def test_explicit_international_is_403(self):
        status, doc, app = self.chat("international.heavy", client=LAN)
        self.assertEqual(status, 403)
        self.assertEqual(doc["error"]["message"], LOCK_MSG)
        self.assertEqual(doc["error"]["type"], "ferry_fleet")
        self.assertEqual(app.calls, 0, "litellm must never see a refused request")

    def test_explicit_domestic_is_fine(self):
        status, _, app = self.chat("domestic.heavy", client=LAN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(app.body)["model"], "domestic.heavy")

    def test_header_international_is_403(self):
        status, doc, app = self.chat(
            "heavy", client=LAN, headers=[(b"x-ferry-fleet", b"international")])
        self.assertEqual(status, 403)
        self.assertEqual(doc["error"]["message"], LOCK_MSG)
        self.assertEqual(app.calls, 0)

    def test_header_domestic_is_fine(self):
        status, _, app = self.chat(
            "heavy", client=LAN, headers=[(b"x-ferry-fleet", b"domestic")])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(app.body)["model"], "domestic.heavy")

    def test_sticky_post_international_is_403_and_writes_nothing(self):
        status, doc, _ = self.drive(FF.FLEET_PATH, {"fleet": "international"},
                                    client=LAN)
        self.assertEqual(status, 403)
        self.assertEqual(doc["error"]["message"], LOCK_MSG)
        with open(self.state_path) as handle:
            self.assertEqual(json.load(handle)["clients"], {})

    def test_sticky_post_domestic_and_clear_are_fine_and_get_is_allowed(self):
        status, _, _ = self.drive(FF.FLEET_PATH, {"fleet": "domestic"}, client=LAN)
        self.assertEqual(status, 200)
        status, _, _ = self.drive(FF.FLEET_PATH, {"fleet": None}, client=LAN)
        self.assertEqual(status, 200)
        status, doc, _ = self.drive(FF.FLEET_PATH, method="GET", client=LAN)
        self.assertEqual(status, 200)
        self.assertEqual(doc["fleet"], "domestic")

    def test_bare_lane_is_domestic_even_when_the_default_is_international(self):
        self.write_state({"default": "international", "clients": {}})
        status, _, app = self.chat("heavy", client=LAN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(app.body)["model"], "domestic.heavy")

    def test_bare_lane_ignores_a_preexisting_international_sticky(self):
        self.write_state({"default": "domestic",
                          "clients": {"100.64.1.2": "international",
                                      "laptop": "international"}})
        for client, headers in ((LAN, []),
                                (LAN, [(b"x-ferry-client", b"laptop")])):
            with self.subTest(headers=headers):
                status, _, app = self.chat("flash", client=client, headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(app.body)["model"], "domestic.flash")
        # and the file was not rewritten
        with open(self.state_path) as handle:
            self.assertEqual(json.load(handle)["clients"]["laptop"], "international")

    def test_get_reports_the_effective_fleet(self):
        self.write_state({"default": "international",
                          "clients": {"laptop": "international"}})
        _, doc, _ = self.drive(FF.FLEET_PATH, method="GET", client=LAN,
                               headers=[(b"x-ferry-client", b"laptop")])
        self.assertEqual(doc["fleet"], "domestic")

    def test_alias_lanes_resolve_to_domestic(self):
        self.write_state({"default": "international", "clients": {}})
        _, _, app = self.chat("orch", client=LAN)
        self.assertEqual(json.loads(app.body)["model"], "domestic.heavy")


class TestLoopbackThatIsNotTheHost(Base):
    def test_each_proxied_or_named_loopback_request_is_a_client(self):
        cases = {
            "x-forwarded-for": [(b"x-forwarded-for", b"100.64.1.2")],
            "tailscale-user-login": [(b"tailscale-user-login", b"a@b.c")],
            "x-ferry-client": [(b"x-ferry-client", b"some-client")],
        }
        for name, headers in cases.items():
            with self.subTest(name):
                status, doc, _ = self.chat("international.heavy", headers=headers)
                self.assertEqual(status, 403)
                self.assertEqual(doc["error"]["message"], LOCK_MSG)
                self.write_state({"default": "international", "clients": {}})
                status, _, app = self.chat("heavy", headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(app.body)["model"], "domestic.heavy")
                self.write_state({"default": "domestic", "clients": {}})

    def test_a_device_key_caller_is_a_client(self):
        state = self.state
        locked = not FF.is_host_caller(
            {"client": HOST, FF.KEY_ENTRY_SCOPE: {"name": "k"}, FF.KEY_SCOPE: "k"}, {})
        self.assertTrue(locked)
        with self.assertRaises(FF.FleetLockError):
            FF.resolve_model("international.heavy", "", "k", state, locked=True)
        self.write_state({"default": "international", "clients": {}})
        self.assertEqual(
            FF.resolve_model("heavy", "", "k", state, locked=True), "domestic.heavy")
        self.assertEqual(
            FF.resolve_model("heavy", "", "k", state, locked=False),
            "international.heavy")


class TestMarker(Base):
    def test_client_body_gets_the_marker_and_keeps_its_metadata(self):
        raw = json.dumps({"model": "heavy", "metadata": {"trace": "t1"}}).encode()
        status, _, app = self.drive("/v1/chat/completions", raw=raw, client=LAN)
        self.assertEqual(status, 200)
        doc = json.loads(app.body)
        self.assertEqual(doc["metadata"], {"trace": "t1", "ferry_domestic_only": True})
        self.assertEqual(doc["model"], "domestic.heavy")

    def test_marker_created_when_metadata_is_absent_and_length_matches(self):
        status, _, app = self.chat("domestic.heavy", client=LAN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(app.body)["metadata"],
                         {"ferry_domestic_only": True})
        headers = dict(app.scope.get("headers") or [])
        self.assertEqual(headers[b"content-length"], str(len(app.body)).encode())

    def test_marker_also_lands_on_a_local_lane_and_an_unknown_model(self):
        for model in ("local-orch", "some-other-model"):
            with self.subTest(model):
                _, _, app = self.chat(model, client=LAN)
                doc = json.loads(app.body)
                self.assertEqual(doc["model"], model)
                self.assertIs(doc["metadata"]["ferry_domestic_only"], True)

    def test_a_modelless_object_still_gets_the_marker(self):
        _, _, app = self.drive("/v1/chat/completions", {"messages": []}, client=LAN)
        self.assertEqual(json.loads(app.body),
                         {"messages": [], "metadata": {"ferry_domestic_only": True}})

    def test_non_json_body_is_untouched(self):
        raw = b"not json at all"
        _, _, app = self.drive("/v1/chat/completions", raw=raw, client=LAN)
        self.assertEqual(app.body, raw)

    def test_non_object_json_body_is_untouched(self):
        raw = b"[1, 2, 3]"
        _, _, app = self.drive("/v1/chat/completions", raw=raw, client=LAN)
        self.assertEqual(app.body, raw)

    def test_host_body_is_byte_identical_when_nothing_else_changes(self):
        raw = json.dumps({"model": "international.heavy", "x": 1}).encode()
        _, _, app = self.drive("/v1/chat/completions", raw=raw)
        self.assertEqual(app.body, raw)

    def test_a_refused_request_never_reaches_litellm(self):
        status, _, app = self.chat("international.flash", client=LAN)
        self.assertEqual(status, 403)
        self.assertIsNone(app.body)


class TestCatalogueFleet(Base):
    def test_a_locked_caller_sees_domestic_bare_lanes(self):
        self.write_state({"default": "international", "clients": {}})
        mw = LaneCatalogueFilter(BodyApp(), frozenset(), fleets=FLEETS,
                                 state=self.state)
        self.assertEqual(mw._catalogue_fleet(
            {"headers": [], "client": LAN}), "domestic")
        self.assertEqual(mw._catalogue_fleet(
            {"headers": [], "client": HOST}), "international")


if __name__ == "__main__":
    unittest.main()
