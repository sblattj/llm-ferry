#!/usr/bin/env python3
"""Stdlib unittest for the lane-catalogue filter.

Run:  python3 lib/ferry-front.test.py

front/ferry_front.py wraps litellm's proxy app so `/v1/models` advertises only
the lanes, not the fallback deployments a client must never select directly.

Two properties matter more than the filtering itself:

  1. The inference path must not be touched. The middleware sits in front of
     EVERY request, so a wrapper accidentally left around `send` would put
     Python between the client and every streamed token. The tests below assert
     the wrapped app receives the caller's ORIGINAL send callable, by identity.

  2. It must fail open. A visible fallback hop is a wart; a front door that
     refuses to answer is an outage. Every malformed input is asserted to pass
     the upstream body through byte-for-byte.

No litellm import is needed: the filter and the middleware are exercised
directly, so the suite runs offline in milliseconds.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

from ferry_front import (  # noqa: E402
    LaneCatalogueFilter,
    _public_lane_names,
    filter_catalogue,
)

LANES = frozenset({"orch", "flash"})


def body(*ids):
    return json.dumps({
        "object": "list",
        "data": [{"id": i, "object": "model"} for i in ids],
    }).encode()


def ids(payload):
    return [item["id"] for item in json.loads(payload)["data"]]


class TestFilterCatalogue(unittest.TestCase):
    def test_drops_everything_not_marked_public(self):
        out = filter_catalogue(body("orch", "orch-deepseek", "flash", "flash-gem"), LANES)
        self.assertEqual(ids(out), ["orch", "flash"])

    def test_preserves_the_envelope(self):
        out = filter_catalogue(body("orch", "orch-deepseek"), LANES)
        self.assertEqual(json.loads(out)["object"], "list")

    def test_no_change_returns_none(self):
        # Signals "send the upstream bytes", so an untouched body is never
        # re-serialized under us.
        self.assertIsNone(filter_catalogue(body("orch", "flash"), LANES))

    def test_empty_public_set_returns_none(self):
        self.assertIsNone(filter_catalogue(body("orch", "orch-deepseek"), frozenset()))

    def test_would_empty_a_populated_catalogue_returns_none(self):
        # A config predating the marker would otherwise leave every client
        # staring at an empty model list — worse than the leak being fixed.
        self.assertIsNone(filter_catalogue(body("a", "b"), LANES))

    def test_non_json_returns_none(self):
        self.assertIsNone(filter_catalogue(b"<html>gateway timeout</html>", LANES))

    def test_unexpected_shape_returns_none(self):
        self.assertIsNone(filter_catalogue(json.dumps({"error": "nope"}).encode(), LANES))
        self.assertIsNone(filter_catalogue(json.dumps([1, 2]).encode(), LANES))

    def test_non_dict_entries_are_dropped_not_fatal(self):
        payload = json.dumps({"data": ["junk", {"id": "orch"}]}).encode()
        self.assertEqual(ids(filter_catalogue(payload, LANES)), ["orch"])


class TestPublicLaneNames(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_reads_the_public_marker(self):
        path = self._write("""
model_list:
  - model_name: orch
    model_info: {id: a, public: true}
  - model_name: orch-deepseek
    model_info: {id: b}
  - model_name: flash
    model_info: {public: true}
""")
        self.assertEqual(_public_lane_names(path), frozenset({"orch", "flash"}))

    def test_a_truthy_string_is_not_public(self):
        # `public: "yes"` is a typo, not an opt-in. Only the boolean counts, so
        # a stray value cannot quietly widen what is advertised.
        path = self._write("model_list:\n  - {model_name: orch, model_info: {public: 'yes'}}\n")
        self.assertEqual(_public_lane_names(path), frozenset())

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(_public_lane_names("/nonexistent/ferry.yaml"), frozenset())
        self.assertEqual(_public_lane_names(""), frozenset())

    def test_unparseable_yaml_is_empty_not_an_error(self):
        path = self._write("model_list: [oops\n  broken: :\n")
        self.assertEqual(_public_lane_names(path), frozenset())

    def test_a_config_with_no_markers_is_empty(self):
        path = self._write("model_list:\n  - {model_name: orch, model_info: {id: a}}\n")
        self.assertEqual(_public_lane_names(path), frozenset())


class RecordingApp:
    """A minimal ASGI app that records exactly what it was handed."""

    def __init__(self, payload=b"", status=200, headers=None, chunks=None):
        self.payload = payload
        self.status = status
        self.headers = headers or [(b"content-type", b"application/json")]
        self.chunks = chunks
        self.seen_send = None
        self.seen_receive = None
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.seen_send = send
        self.seen_receive = receive
        await send({
            "type": "http.response.start",
            "status": self.status,
            "headers": list(self.headers),
        })
        if self.chunks is None:
            await send({"type": "http.response.body", "body": self.payload})
            return
        for index, chunk in enumerate(self.chunks):
            await send({
                "type": "http.response.body",
                "body": chunk,
                "more_body": index < len(self.chunks) - 1,
            })


async def drive(middleware, path, scope_type="http"):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await middleware({"type": scope_type, "path": path}, receive, send)
    return sent, send


def collect(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start, payload


class TestMiddleware(unittest.TestCase):
    def test_the_inference_path_is_handed_the_original_send(self):
        # THE test. If this ever fails, every streamed token is being funnelled
        # through this module's Python instead of going straight out.
        app = RecordingApp(b"whatever")
        mw = LaneCatalogueFilter(app, LANES)
        _, send = asyncio.run(drive(mw, "/v1/chat/completions"))
        self.assertIs(app.seen_send, send)

    def test_the_model_listing_is_not_handed_the_original_send(self):
        # The converse, so the test above cannot pass by the middleware being
        # inert everywhere.
        app = RecordingApp(body("orch", "orch-deepseek"))
        mw = LaneCatalogueFilter(app, LANES)
        _, send = asyncio.run(drive(mw, "/v1/models"))
        self.assertIsNot(app.seen_send, send)

    def test_a_non_http_scope_passes_straight_through(self):
        app = RecordingApp(b"")
        mw = LaneCatalogueFilter(app, LANES)
        _, send = asyncio.run(drive(mw, "/v1/models", scope_type="websocket"))
        self.assertIs(app.seen_send, send)

    def test_an_empty_public_set_disables_the_wrapper_entirely(self):
        app = RecordingApp(body("orch", "orch-deepseek"))
        mw = LaneCatalogueFilter(app, frozenset())
        _, send = asyncio.run(drive(mw, "/v1/models"))
        self.assertIs(app.seen_send, send)

    def test_filters_the_listing(self):
        app = RecordingApp(body("orch", "orch-deepseek", "flash", "flash-gem"))
        sent, _ = asyncio.run(drive(LaneCatalogueFilter(app, LANES), "/v1/models"))
        _, payload = collect(sent)
        self.assertEqual(ids(payload), ["orch", "flash"])

    def test_bare_models_path_is_filtered_too(self):
        # openai-compatible clients hit both spellings.
        app = RecordingApp(body("orch", "orch-deepseek"))
        sent, _ = asyncio.run(drive(LaneCatalogueFilter(app, LANES), "/models"))
        _, payload = collect(sent)
        self.assertEqual(ids(payload), ["orch"])

    def test_content_length_matches_the_rewritten_body(self):
        # A stale content-length is how a client hangs waiting for bytes that
        # are never coming.
        original = body("orch", "orch-deepseek", "flash-gem")
        app = RecordingApp(
            original,
            headers=[(b"content-type", b"application/json"),
                     (b"content-length", str(len(original)).encode())],
        )
        sent, _ = asyncio.run(drive(LaneCatalogueFilter(app, LANES), "/v1/models"))
        start, payload = collect(sent)
        lengths = [v for k, v in start["headers"] if k.lower() == b"content-length"]
        self.assertEqual(lengths, [str(len(payload)).encode()])
        self.assertLess(len(payload), len(original))

    def test_other_headers_and_status_survive(self):
        app = RecordingApp(
            body("orch", "orch-deepseek"),
            status=200,
            headers=[(b"content-type", b"application/json"), (b"x-request-id", b"abc")],
        )
        sent, _ = asyncio.run(drive(LaneCatalogueFilter(app, LANES), "/v1/models"))
        start, _ = collect(sent)
        self.assertEqual(start["status"], 200)
        self.assertIn((b"x-request-id", b"abc"), start["headers"])

    def test_a_chunked_listing_is_reassembled_before_filtering(self):
        raw = body("orch", "orch-deepseek", "flash")
        half = len(raw) // 2
        app = RecordingApp(chunks=[raw[:half], raw[half:]])
        sent, _ = asyncio.run(drive(LaneCatalogueFilter(app, LANES), "/v1/models"))
        _, payload = collect(sent)
        self.assertEqual(ids(payload), ["orch", "flash"])

    def test_an_error_body_on_the_listing_path_is_passed_through(self):
        app = RecordingApp(b"upstream exploded", status=500,
                           headers=[(b"content-type", b"text/plain")])
        sent, _ = asyncio.run(drive(LaneCatalogueFilter(app, LANES), "/v1/models"))
        start, payload = collect(sent)
        self.assertEqual(start["status"], 500)
        self.assertEqual(payload, b"upstream exploded")


class TestShippedConfigTemplate(unittest.TestCase):
    """The template must mark its lanes, or the filter is inert where it ships."""

    def test_the_route_template_marks_exactly_its_lanes(self):
        import yaml

        path = os.path.join(REPO, "litellm-route-example.yaml")
        with open(path) as handle:
            cfg = yaml.safe_load(handle)
        names = {m["model_name"] for m in cfg["model_list"]}
        public = _public_lane_names(path)

        self.assertTrue(public, "no lane in the shipped template marks public: true")
        self.assertTrue(public <= names)

        # The property that matters is narrower than "is a fallback target".
        # A lane may legitimately be one — the template spills `flash` onto
        # `orch` — and `orch` still has a chain of its own, so a client that
        # picks it is not stranded. What must never be advertised is a
        # deployment reachable ONLY as a hop: a fallback target that is not
        # itself a key in `fallbacks`, and therefore has no failover behind it.
        keys = {g for entry in cfg["router_settings"]["fallbacks"] for g in entry}
        targets = {
            t for entry in cfg["router_settings"]["fallbacks"]
            for ts in entry.values() for t in ts
        }
        hop_only = {t for t in targets if t in names and t not in keys}
        self.assertTrue(hop_only, "template has no hop-only deployment to check")
        for hop in sorted(hop_only):
            self.assertNotIn(
                hop, public,
                f"{hop!r} is reachable only as a fallback hop but is marked "
                "public; a client selecting it gets no failover at all",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
