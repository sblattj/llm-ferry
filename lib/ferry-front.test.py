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
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

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
        # through this module's Python instead of going straight out. The header
        # stripper wraps `send` by default, so this identity holds only with the
        # strip off — which is what proves the strip is the ONLY new wrapper.
        app = RecordingApp(b"whatever")
        mw = LaneCatalogueFilter(app, LANES)
        os.environ["FERRY_STRIP_HEADERS"] = "0"
        try:
            _, send = asyncio.run(drive(mw, "/v1/chat/completions"))
        finally:
            os.environ.pop("FERRY_STRIP_HEADERS", None)
        self.assertIs(app.seen_send, send)

    def test_the_inference_path_strips_identity_headers_by_default(self):
        # The strip wraps `send` and drops the identity headers — the lane
        # abstraction extends to the header surface. Bodies stay untouched.
        app = RecordingApp(b"whatever", headers=[
            (b"content-type", b"text/event-stream"),
            (b"x-litellm-model-name", b"anthropic/k3"),
            (b"x-litellm-model-id", b"kimi-k3-heavy"),
            (b"x-litellm-model-api-base", b"https://api.kimi.com/coding"),
            (b"x-litellm-model-group", b"heavy"),
            (b"x-litellm-response-cost", b"0.0"),
        ])
        mw = LaneCatalogueFilter(app, LANES)
        sent, _ = asyncio.run(drive(mw, "/v1/chat/completions"))
        start = next(m for m in sent if m["type"] == "http.response.start")
        keys = [k for k, _ in start["headers"]]
        for gone in (b"x-litellm-model-name", b"x-litellm-model-id",
                     b"x-litellm-model-api-base", b"x-litellm-model-group"):
            self.assertNotIn(gone, keys)
        # cost/timing and content-type survive — they leak nothing.
        self.assertIn(b"x-litellm-response-cost", keys)
        self.assertIn(b"content-type", keys)

    def test_the_inference_path_strips_provider_headers_but_keeps_backoff_hints(self):
        # The llm_provider-* family forwards the upstream's own headers — its
        # set-cookie Domain names the vendor, the cf-ray names the PoP. Strip it
        # all EXCEPT the rate-limit / retry hints a client uses to back off.
        app = RecordingApp(b"whatever", headers=[
            (b"llm_provider-server", b"cloudflare"),
            (b"llm_provider-set-cookie", b"__cf_bm=x; Domain=kimi.com"),
            (b"llm_provider-cf-ray", b"a35a-LAX"),
            (b"llm_provider-x-trace-id", b"4768a4"),
            (b"llm_provider-x-ratelimit-remaining", b"59"),
            (b"llm_provider-retry-after", b"2"),
            (b"retry-after", b"3"),
            (b"content-type", b"application/json"),
        ])
        mw = LaneCatalogueFilter(app, LANES)
        sent, _ = asyncio.run(drive(mw, "/v1/chat/completions"))
        start = next(m for m in sent if m["type"] == "http.response.start")
        keys = [k for k, _ in start["headers"]]
        for gone in (b"llm_provider-server", b"llm_provider-set-cookie",
                     b"llm_provider-cf-ray", b"llm_provider-x-trace-id"):
            self.assertNotIn(gone, keys)
        # backoff/rate-limit hints survive.
        self.assertIn(b"llm_provider-x-ratelimit-remaining", keys)
        self.assertIn(b"llm_provider-retry-after", keys)
        self.assertIn(b"retry-after", keys)
        self.assertIn(b"content-type", keys)

    def test_strip_header_predicate(self):
        sh = FF._strip_header
        # identity + provider-identity stripped
        for n in (b"x-litellm-model-name", b"llm_provider-set-cookie",
                  b"llm_provider-cf-ray", b"llm-provider-server"):
            self.assertTrue(sh(n), n)
        # optimization/backoff + cost kept
        for n in (b"llm_provider-x-ratelimit-limit", b"llm_provider-retry-after",
                  b"retry-after", b"x-litellm-response-cost",
                  b"content-type", b"x-litellm-attempted-fallbacks"):
            self.assertFalse(sh(n), n)

    def test_the_strip_keeps_headers_for_a_loopback_client(self):
        # ferry-dash's probe reads x-litellm-model-name over 127.0.0.1 — the
        # control plane keeps full headers.
        app = RecordingApp(b"whatever", headers=[
            (b"x-litellm-model-name", b"anthropic/k3"),
        ])
        mw = LaneCatalogueFilter(app, LANES)
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {"type": "http", "path": "/v1/chat/completions",
                 "client": ("127.0.0.1", 5000)}
        asyncio.run(mw(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertIn((b"x-litellm-model-name", b"anthropic/k3"),
                      start["headers"])

    def test_the_strip_can_be_disabled(self):
        app = RecordingApp(b"whatever", headers=[
            (b"x-litellm-model-name", b"anthropic/k3"),
        ])
        mw = LaneCatalogueFilter(app, LANES)
        os.environ["FERRY_STRIP_HEADERS"] = "0"
        try:
            sent, _ = asyncio.run(drive(mw, "/v1/chat/completions"))
        finally:
            os.environ.pop("FERRY_STRIP_HEADERS", None)
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertIn((b"x-litellm-model-name", b"anthropic/k3"),
                      start["headers"])

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

    def test_every_openrouter_deployment_sorts_providers_by_throughput(self):
        """OpenRouter's default is price-weighted; the template asks for speed.

        The knob is `extra_body.provider.sort: throughput` on the deployment,
        forwarded verbatim by litellm's openrouter transform. It has to be on
        EVERY openrouter/ deployment: a hop that lacks it silently lands on the
        cheapest provider (Z.AI at 22 tok/s for GLM 5.3 Flash on 2026-09-02,
        against 86-111 at the top), and nothing downstream can tell.
        """
        import yaml

        path = os.path.join(REPO, "litellm-route-example.yaml")
        with open(path) as handle:
            cfg = yaml.safe_load(handle)
        openrouter = [
            m for m in cfg["model_list"]
            if str(m.get("litellm_params", {}).get("model", "")).startswith("openrouter/")
        ]
        self.assertTrue(openrouter, "template has no openrouter/ deployment to check")
        for m in openrouter:
            provider = (m["litellm_params"].get("extra_body") or {}).get("provider") or {}
            self.assertEqual(
                provider.get("sort"), "throughput",
                f"{m['model_name']!r} rides OpenRouter without "
                "extra_body.provider.sort: throughput — it will be served by "
                "the cheapest provider, not the fastest",
            )


class TestReorderHotSwap(unittest.TestCase):
    """POST /v1/ferry/reorder swaps router.fallbacks with no restart.

    The rollout gate next to the byte-equivalence gate: a reorder must be
    atomic (all lanes validated before any assignment), must refuse unknown
    hops (litellm would silently skip them at 2am), and must never reach the
    LAN (loopback-only). The middleware is driven with a fake router so the
    suite stays offline; the live test below proves the real Router obeys.
    """

    NAMES = {"heavy", "orch-muse-spark", "orch-zai-glm53"}

    class FakeRouter:
        def __init__(self):
            self.fallbacks = [{"heavy": ["orch-muse-spark"]}]

        def get_model_groups(self):
            return list(TestReorderHotSwap.NAMES)

    def _mw(self, router):
        mw = LaneCatalogueFilter(RecordingApp(b""), LANES)
        prev = FF._live_router
        FF._live_router = lambda: router
        self.addCleanup(setattr, FF, "_live_router", prev)
        return mw

    def _scope(self, path, method="POST"):
        return {"type": "http", "path": path, "method": method,
                "client": ("127.0.0.1", 5000)}

    async def _post(self, mw, doc, path="/v1/ferry/reorder"):
        body = json.dumps(doc).encode()
        msgs = [{"type": "http.request", "body": body, "more_body": False}]
        sent = []

        async def receive():
            return msgs.pop(0) if msgs else {
                "type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)
        await mw(self._scope(path), receive, send)
        start, payload = collect(sent)
        return start["status"], json.loads(payload)

    def test_parse_accepts_the_unified_order_shape(self):
        chains, err = FF.parse_reorder_body(json.dumps(
            {"order": {"heavy": ["heavy", "orch-zai-glm53"]}}).encode())
        self.assertIsNone(err)
        self.assertEqual(chains, {"heavy": ["orch-zai-glm53"]})

    def test_parse_accepts_the_bare_chains_shape(self):
        chains, err = FF.parse_reorder_body(json.dumps(
            {"chains": {"heavy": ["orch-zai-glm53"]}}).encode())
        self.assertIsNone(err)
        self.assertEqual(chains, {"heavy": ["orch-zai-glm53"]})

    def test_parse_rejects_a_non_json_body(self):
        _, err = FF.parse_reorder_body(b"not json{")
        self.assertTrue(err)

    def test_parse_rejects_a_body_with_neither_shape(self):
        _, err = FF.parse_reorder_body(json.dumps({"nope": {}}).encode())
        self.assertIn("order", err)

    def test_parse_rejects_promoting_a_fallback_to_primary(self):
        _, err = FF.parse_reorder_body(json.dumps(
            {"order": {"heavy": ["orch-zai-glm53"]}}).encode())
        self.assertIn("primary changes not yet supported", err)

    def test_validate_refuses_an_unknown_hop(self):
        errs = FF.validate_reorder({"heavy": ["ghost"]}, self.NAMES)
        self.assertTrue(any("ghost" in e for e in errs))

    def test_validate_refuses_an_unknown_lane(self):
        errs = FF.validate_reorder({"ghost": ["heavy"]}, self.NAMES)
        self.assertTrue(any("ghost" in e for e in errs))

    def test_validate_refuses_a_self_fallback(self):
        self.assertTrue(FF.validate_reorder({"heavy": ["heavy"]}, self.NAMES))

    def test_validate_refuses_a_duplicated_hop(self):
        errs = FF.validate_reorder(
            {"heavy": ["orch-muse-spark", "orch-muse-spark"]}, self.NAMES)
        self.assertTrue(any("twice" in e for e in errs))

    def test_validate_accepts_a_sane_chain(self):
        self.assertEqual(FF.validate_reorder(
            {"heavy": ["orch-muse-spark", "orch-zai-glm53"]}, self.NAMES), [])

    def test_a_good_reorder_swaps_the_live_chain(self):
        router = self.FakeRouter()
        status, doc = asyncio.run(self._post(
            self._mw(router), {"chains": {"heavy": ["orch-zai-glm53"]}}))
        self.assertEqual(status, 200)
        self.assertTrue(doc["ok"])
        merged = {}
        for e in router.fallbacks:
            merged.update(e)
        self.assertEqual(merged["heavy"], ["orch-zai-glm53"])

    def test_a_bad_reorder_changes_nothing(self):
        router = self.FakeRouter()
        before = list(router.fallbacks)
        status, doc = asyncio.run(self._post(
            self._mw(router), {"chains": {"heavy": ["ghost"]}}))
        self.assertEqual(status, 409)
        self.assertTrue(doc["errors"])
        self.assertEqual(router.fallbacks, before)

    def test_a_failed_lane_leaves_the_good_lane_untouched(self):
        router = self.FakeRouter()
        before = list(router.fallbacks)
        status, _ = asyncio.run(self._post(self._mw(router), {"chains": {
            "heavy": ["orch-zai-glm53"], "ghost": ["heavy"]}}))
        self.assertEqual(status, 409)
        self.assertEqual(router.fallbacks, before)

    def test_off_host_reorder_is_forbidden(self):
        router = self.FakeRouter()
        mw = self._mw(router)
        scope = {"type": "http", "path": "/v1/ferry/reorder",
                 "method": "POST", "client": ("192.168.1.50", 5000)}

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}
        sent = []

        async def send(m):
            sent.append(m)
        asyncio.run(mw(scope, receive, send))
        start, _ = collect(sent)
        self.assertEqual(start["status"], 403)
        self.assertEqual(router.fallbacks, [{"heavy": ["orch-muse-spark"]}])

    def test_chains_reads_back_the_live_state(self):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = []

        async def send(m):
            sent.append(m)
        asyncio.run(self._mw(self.FakeRouter())(
            self._scope("/v1/ferry/chains", "GET"), receive, send))
        start, payload = collect(sent)
        self.assertEqual(start["status"], 200)
        self.assertEqual(dict(json.loads(payload)["chains"]),
                         {"heavy": ["orch-muse-spark"]})

    def test_reorder_paths_are_never_tapped_as_inference(self):
        for path in ("/v1/ferry/chains", "/v1/ferry/reorder",
                     "/v1/ferry/promote", "/v1/ferry/promote/preview"):
            self.assertFalse(FF.is_inference_path(path), path)


class FakeLiveRouter:
    """A litellm Router stand-in with real upsert/delete/index semantics.

    Dict-backed model_list plus the two index maps litellm maintains, so the
    promote tests exercise the same contract service_promote relies on: evict
    by id, upsert under the lane name, resolve-by-group returns the first
    member. If litellm ever changes that contract, the LIVE test (below) is
    the one that catches it — this fake only guards our own logic."""

    def __init__(self):
        self.model_list = [
            {"model_name": "heavy",
             "litellm_params": {"model": "anthropic/k3"},
             "model_info": {"id": "kimi-k3-heavy", "public": True}},
            {"model_name": "orch-muse-spark",
             "litellm_params": {"model": "openrouter/meta/muse-spark-1.3"},
             "model_info": {"id": "or-muse-spark-1"}},
        ]
        self.fallbacks = [{"heavy": ["orch-muse-spark"]}]
        self._reindex()

    def _reindex(self):
        self.model_id_to_deployment_index_map = {
            d["model_info"]["id"]: i for i, d in enumerate(self.model_list)}
        idx = {}
        for i, d in enumerate(self.model_list):
            idx.setdefault(d["model_name"], []).append(i)
        self.model_name_to_deployment_indices = idx

    def get_model_groups(self):
        return [{"model_name": n} for n in self.model_name_to_deployment_indices]

    def get_deployment_by_model_group_name(self, name):
        ids = self.model_name_to_deployment_indices.get(name, [])
        if not ids:
            return None
        d = self.model_list[ids[0]]
        return type("Dep", (), {"to_json": lambda self, **k: dict(d)})()

    def delete_deployment(self, id):
        i = self.model_id_to_deployment_index_map.get(id)
        if i is None:
            return None
        item = self.model_list.pop(i)
        self._reindex()
        return item

    def upsert_deployment(self, deployment):
        new = {"model_name": deployment.model_name,
               "litellm_params": dict(deployment.litellm_params
                                      if isinstance(deployment.litellm_params, dict)
                                      else deployment.litellm_params),
               "model_info": dict(deployment.model_info
                                  if isinstance(deployment.model_info, dict)
                                  else deployment.model_info)}
        old = self.model_id_to_deployment_index_map.get(new["model_info"]["id"])
        if old is not None:
            self.model_list.pop(old)
        self.model_list.append(new)
        self._reindex()
        return deployment


class TestPromoteHotSwap(unittest.TestCase):
    """POST /v1/ferry/promote swaps two backends with no restart.

    The operation is a BACKEND SWAP, not a reseat: lane and hop trade
    litellm_params under fresh ids, names and chains untouched. The guards
    that matter: the hop must already be in the lane's chain (no inventing
    backends), ids are minted server-side or freshness-checked (a reused id
    inherits the old backend's cooldowns, usage, and provider client), and a
    failed swap changes nothing."""

    def _mw(self, router):
        mw = LaneCatalogueFilter(RecordingApp(b""), LANES)
        prev = FF._live_router
        FF._live_router = lambda: router
        self.addCleanup(setattr, FF, "_live_router", prev)
        return mw

    def _scope(self, path):
        return {"type": "http", "path": path, "method": "POST",
                "client": ("127.0.0.1", 5000)}

    async def _post(self, mw, path, doc):
        body = json.dumps(doc).encode()
        msgs = [{"type": "http.request", "body": body, "more_body": False}]
        sent = []

        async def receive():
            return msgs.pop(0) if msgs else {
                "type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)
        await mw(self._scope(path), receive, send)
        start, payload = collect(sent)
        return start["status"], json.loads(payload)

    def test_parse_needs_lane_and_hop(self):
        for doc, frag in (({"lane": "heavy"}, "hop"),
                          ({"hop": "x"}, "lane"),
                          ({"lane": "heavy", "hop": "heavy"}, "already its own")):
            _, _, _, err = FF.parse_promote_body(json.dumps(doc).encode())
            self.assertIn(frag, err)

    def test_validate_refuses_a_hop_outside_the_chain(self):
        router = FakeLiveRouter()
        router.fallbacks = [{"heavy": []}]
        errs = FF.validate_promote(router, "heavy", "orch-muse-spark")
        self.assertTrue(any("not in" in e for e in errs), errs)

    def test_validate_refuses_an_unknown_hop(self):
        errs = FF.validate_promote(FakeLiveRouter(), "heavy", "ghost")
        self.assertTrue(any("ghost" in e for e in errs))

    def test_validate_refuses_an_unknown_lane(self):
        errs = FF.validate_promote(FakeLiveRouter(), "ghost", "heavy")
        self.assertTrue(any("ghost" in e for e in errs))

    def test_validate_refuses_a_reused_id(self):
        errs = FF.validate_promote(FakeLiveRouter(), "heavy", "orch-muse-spark",
                                   {"lane_id": "kimi-k3-heavy"})
        self.assertTrue(any("already served" in e for e in errs), errs)

    def test_a_good_swap_trades_backends_under_fresh_ids(self):
        router = FakeLiveRouter()
        ok, errs, ids = FF.service_promote(router, "heavy", "orch-muse-spark")
        self.assertTrue(ok, errs)
        lane = router.get_deployment_by_model_group_name("heavy").to_json()
        hop = router.get_deployment_by_model_group_name(
            "orch-muse-spark").to_json()
        self.assertEqual(lane["litellm_params"]["model"],
                         "openrouter/meta/muse-spark-1.3")
        self.assertEqual(hop["litellm_params"]["model"], "anthropic/k3")
        # Fresh ids, minted server-side — neither old id survives.
        self.assertNotIn(ids["lane"], ("kimi-k3-heavy", "or-muse-spark-1"))
        self.assertNotIn(ids["hop"], ("kimi-k3-heavy", "or-muse-spark-1"))
        self.assertNotEqual(ids["lane"], ids["hop"])
        self.assertEqual(lane["model_info"]["id"], ids["lane"])
        self.assertEqual(hop["model_info"]["id"], ids["hop"])
        self.assertNotIn("kimi-k3-heavy",
                         router.model_id_to_deployment_index_map)
        self.assertNotIn("or-muse-spark-1",
                         router.model_id_to_deployment_index_map)
        # Chains untouched — the demoted backend still serves under the hop.
        merged = {}
        for e in router.fallbacks:
            merged.update(e)
        self.assertEqual(merged["heavy"], ["orch-muse-spark"])

    def test_a_supplied_id_pair_is_honoured_for_file_echo(self):
        # The dash echoes the LIVE ids into the file so the two agree; the
        # pair still originates server-side (minted before the file write),
        # never from the user's keyboard.
        router = FakeLiveRouter()
        ok, errs, ids = FF.service_promote(
            router, "heavy", "orch-muse-spark",
            {"lane_id": "heavy-promoted-X", "hop_id": "muse-promoted-X"})
        self.assertTrue(ok, errs)
        self.assertEqual(ids, {"lane": "heavy-promoted-X",
                               "hop": "muse-promoted-X"})

    def test_a_failed_swap_changes_nothing(self):
        router = FakeLiveRouter()
        before_list = [dict(d) for d in router.model_list]
        before_fb = [dict(e) for e in router.fallbacks]
        ok, errs, ids = FF.service_promote(router, "heavy", "ghost")
        self.assertFalse(ok)
        self.assertIsNone(ids)
        self.assertEqual(router.model_list, before_list)
        self.assertEqual(router.fallbacks, before_fb)

    def test_promote_endpoint_swaps_live(self):
        router = FakeLiveRouter()
        status, doc = asyncio.run(self._post(
            self._mw(router), "/v1/ferry/promote",
            {"lane": "heavy", "hop": "orch-muse-spark"}))
        self.assertEqual(status, 200)
        self.assertTrue(doc["ok"])
        self.assertIn("ids", doc)

    def test_promote_preview_touches_nothing(self):
        router = FakeLiveRouter()
        before = [dict(d) for d in router.model_list]
        status, doc = asyncio.run(self._post(
            self._mw(router), "/v1/ferry/promote/preview",
            {"lane": "heavy", "hop": "orch-muse-spark"}))
        self.assertEqual(status, 200)
        self.assertEqual(doc["old_lane_model"], "anthropic/k3")
        self.assertEqual(doc["old_hop_model"],
                         "openrouter/meta/muse-spark-1.3")
        self.assertEqual(router.model_list, before)

    def test_promote_is_loopback_only(self):
        router = FakeLiveRouter()
        mw = self._mw(router)
        scope = {"type": "http", "path": "/v1/ferry/promote",
                 "method": "POST", "client": ("192.168.1.50", 5000)}

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}
        sent = []

        async def send(m):
            sent.append(m)
        asyncio.run(mw(scope, receive, send))
        start, _ = collect(sent)
        self.assertEqual(start["status"], 403)


import ferry_front as FF  # noqa: E402


class TestEventTap(unittest.TestCase):
    """The tap reads attribution headers off the response.

    `test_the_inference_path_is_handed_the_original_send` above stays true and
    unmodified: with the tap OFF — the default — the wrapped app still gets the
    caller's own send by identity. When the tap is ON the wrapper does exist,
    and the property that has to survive is no longer "no wrapper" but "the
    bytes are identical". The equivalence test below is what proves that, and it
    is the rollout gate: if it fails, the tap does not ship enabled.
    """

    ATTRIB = [
        (b"content-type", b"text/event-stream"),
        (b"x-litellm-model-group", b"flash"),
        (b"x-litellm-model-id", b"flash-alt-1"),
        (b"x-litellm-model-name", b"someprovider/some-model"),
        (b"x-litellm-attempted-fallbacks", b"1"),
        (b"x-litellm-fallback-errors",
         b'[{"message":"m","type":"RateLimitError","param":null,"code":"429"}]'),
    ]

    def setUp(self):
        self._prev = os.environ.get("FERRY_EVENTS")
        self._prev_strip = os.environ.get("FERRY_STRIP_HEADERS")
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "ferry-events.ndjson")

    def tearDown(self):
        FF.reset_tap(None)
        if self._prev is None:
            os.environ.pop("FERRY_EVENTS", None)
        else:
            os.environ["FERRY_EVENTS"] = self._prev
        if self._prev_strip is None:
            os.environ.pop("FERRY_STRIP_HEADERS", None)
        else:
            os.environ["FERRY_STRIP_HEADERS"] = self._prev_strip

    def _app(self):
        return RecordingApp(headers=list(self.ATTRIB),
                            chunks=[b"a", b"bb", b"ccc"])

    def _drive(self, enabled, path="/v1/chat/completions"):
        os.environ["FERRY_EVENTS"] = "on" if enabled else "off"
        FF.reset_tap(self.path if enabled else None)
        app = self._app()
        sent, send = asyncio.run(drive(LaneCatalogueFilter(app, LANES), path))
        return app, sent, send

    # ── the gate ───────────────────────────────────────────────────────────
    def test_the_byte_stream_is_identical_with_and_without_the_tap(self):
        # Byte identity is the tap's invariant, so the strip (a separate,
        # header-only concern) is held off here to isolate it.
        os.environ["FERRY_STRIP_HEADERS"] = "0"
        _, with_tap, _ = self._drive(True)
        _, without, _ = self._drive(False)
        self.assertEqual(with_tap, without)

    def test_chunk_boundaries_and_more_body_survive(self):
        _, sent, _ = self._drive(True)
        bodies = [(m.get("body"), m.get("more_body")) for m in sent
                  if m["type"] == "http.response.body"]
        self.assertEqual(bodies, [(b"a", True), (b"bb", True), (b"ccc", False)])

    def test_headers_are_not_rewritten_on_the_hot_path(self):
        os.environ["FERRY_STRIP_HEADERS"] = "0"
        _, sent, _ = self._drive(True)
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual(start["headers"], list(self.ATTRIB))
        self.assertNotIn(b"content-length", dict(start["headers"]))

    def test_the_tap_still_records_full_attribution_when_headers_are_stripped(self):
        # The strip runs AFTER the tap reads the headers, so the event keeps the
        # real lane/deployment even though the client never sees them.
        os.environ["FERRY_STRIP_HEADERS"] = "1"
        _, sent, _ = self._drive(True)
        FF.tap_flush()
        rec = json.loads(open(self.path).readline())
        self.assertEqual(rec["lane"], "flash")
        self.assertEqual(rec["deployment"], "flash-alt-1")
        self.assertEqual(rec["model"], "someprovider/some-model")
        # ... while the client-facing start line had them stripped.
        start = next(m for m in sent if m["type"] == "http.response.start")
        keys = [k for k, _ in start["headers"]]
        self.assertNotIn(b"x-litellm-model-name", keys)
        self.assertNotIn(b"x-litellm-model-group", keys)

    def test_tap_off_still_hands_over_the_original_send(self):
        os.environ["FERRY_STRIP_HEADERS"] = "0"
        app, _, send = self._drive(False)
        self.assertIs(app.seen_send, send)

    def test_tap_on_wraps_send_and_that_is_the_deliberate_trade(self):
        # The converse of the test above, so neither can pass by the middleware
        # being inert. A wrapper exists when the tap is on; the equivalence test
        # is what makes that acceptable.
        app, _, send = self._drive(True)
        self.assertIsNot(app.seen_send, send)

    # ── what it captures ───────────────────────────────────────────────────
    def test_an_event_carries_the_lane_deployment_and_hop_errors(self):
        self._drive(True)
        FF.tap_flush()
        rec = json.loads(open(self.path).readline())
        self.assertEqual(rec["lane"], "flash")
        self.assertEqual(rec["deployment"], "flash-alt-1")
        self.assertEqual(rec["fallbacks"], 1)
        self.assertEqual(rec["hop_errors"][0]["code"], "429")
        self.assertEqual(rec["status"], 200)

    def test_one_event_per_request_not_one_per_chunk(self):
        self._drive(True)
        FF.tap_flush()
        lines = [l for l in open(self.path) if l.strip()]
        self.assertEqual(len(lines), 1)

    def test_the_event_counts_every_body_byte(self):
        # b"a" + b"bb" + b"ccc" = 6, counted on the way past. The equivalence
        # gate above proves the same bytes still reach the client; this proves
        # the counter read them correctly.
        self._drive(True)
        FF.tap_flush()
        rec = json.loads(open(self.path).readline())
        self.assertEqual(rec["resp_bytes"], 6)

    def test_an_empty_body_counts_zero_bytes(self):
        os.environ["FERRY_EVENTS"] = "on"
        FF.reset_tap(self.path)
        asyncio.run(drive(LaneCatalogueFilter(RecordingApp(
            headers=list(self.ATTRIB), payload=b""), LANES),
            "/v1/chat/completions"))
        FF.tap_flush()
        rec = json.loads(open(self.path).readline())
        self.assertEqual(rec["resp_bytes"], 0)

    def test_a_raising_counter_still_writes_the_record(self):
        # Counting is best-effort; the record is not. A body whose length
        # cannot be read must degrade to 0 counted bytes, never lose the
        # attribution the whole live view runs on.
        os.environ["FERRY_EVENTS"] = "on"
        FF.reset_tap(self.path)

        class WeirdBody:
            def __len__(self):
                raise RuntimeError("no length")

        app = RecordingApp(headers=list(self.ATTRIB),
                           chunks=[WeirdBody(), b"ok"])
        asyncio.run(drive(LaneCatalogueFilter(app, LANES), "/v1/chat/completions"))
        FF.tap_flush()
        rec = json.loads(open(self.path).readline())
        self.assertEqual(rec["resp_bytes"], 2)

    def test_disabled_writes_nothing(self):
        self._drive(False)
        self.assertFalse(os.path.exists(self.path))

    def test_the_model_listing_path_is_not_tapped(self):
        # The catalogue path has its own wrapper and is not a served inference
        # request; an event there would be noise in every per-lane view.
        os.environ["FERRY_EVENTS"] = "on"
        FF.reset_tap(self.path)
        app = RecordingApp(body("orch", "orch-deepseek"))
        asyncio.run(drive(LaneCatalogueFilter(app, LANES), "/v1/models"))
        FF.tap_flush()
        self.assertFalse(os.path.exists(self.path))

    def test_the_listing_is_untapped_even_with_no_public_lanes(self):
        # With an empty public set the catalogue path falls into the SAME branch
        # as inference, so excluding it has to be explicit. Every other test here
        # uses a populated lane set and would miss this.
        os.environ["FERRY_EVENTS"] = "on"
        FF.reset_tap(self.path)
        app = RecordingApp(body("orch"))
        asyncio.run(drive(LaneCatalogueFilter(app, frozenset()), "/v1/models"))
        FF.tap_flush()
        self.assertFalse(os.path.exists(self.path))

    def test_inference_is_still_tapped_with_no_public_lanes(self):
        # The converse, so the exclusion above cannot pass by disabling the tap.
        os.environ["FERRY_EVENTS"] = "on"
        FF.reset_tap(self.path)
        asyncio.run(drive(LaneCatalogueFilter(self._app(), frozenset()),
                          "/v1/chat/completions"))
        FF.tap_flush()
        self.assertTrue(os.path.exists(self.path))

    def test_the_app_is_wrapped_when_the_tap_is_on_but_no_lane_is_public(self):
        # The defect a live run found: build_app returned the RAW litellm app
        # whenever no lane was marked public, so the tap was installed nowhere
        # and captured nothing. Every other test here builds the middleware by
        # hand and therefore cannot see it.
        os.environ["FERRY_EVENTS"] = "on"
        self.assertTrue(FF.should_wrap(frozenset()))

    def test_the_app_is_always_wrapped_for_the_reorder_control_plane(self):
        # The control plane (GET /v1/ferry/chains, POST /v1/ferry/reorder)
        # lives in the middleware, so the app must wrap even with no lanes,
        # no tap, and no strip — otherwise a config needing none of the other
        # three loses its no-restart write path.
        os.environ["FERRY_EVENTS"] = "off"
        os.environ["FERRY_STRIP_HEADERS"] = "0"
        self.assertTrue(FF.should_wrap(frozenset()))

    def test_the_app_is_wrapped_for_stripping_with_no_lanes_and_no_tap(self):
        # Header stripping (on by default) is itself a reason to wrap.
        os.environ["FERRY_EVENTS"] = "off"
        os.environ["FERRY_STRIP_HEADERS"] = "1"
        self.assertTrue(FF.should_wrap(frozenset()))

    def test_the_app_is_wrapped_for_filtering_even_with_the_tap_off(self):
        os.environ["FERRY_EVENTS"] = "off"
        self.assertTrue(FF.should_wrap(frozenset({"flash"})))

    def test_health_and_metrics_paths_are_never_tapped(self):
        # Caught by a live run: /health/liveliness produced a lane:"unknown"
        # event, and both ferry-dash and the exporter poll it every 5s.
        for path in ("/health/liveliness", "/health", "/metrics", "/metrics/",
                     "/v1/models", "/models", "/"):
            self.assertFalse(FF.is_inference_path(path), path)

    def test_every_inference_shape_is_tapped(self):
        for path in ("/v1/chat/completions", "/chat/completions",
                     "/v1/messages", "/v1/responses", "/v1/embeddings",
                     "/v1/chat/completions?stream=true"):
            self.assertTrue(FF.is_inference_path(path), path)

    def test_a_health_request_writes_no_event(self):
        os.environ["FERRY_EVENTS"] = "on"
        FF.reset_tap(self.path)
        asyncio.run(drive(LaneCatalogueFilter(self._app(), LANES),
                          "/health/liveliness"))
        FF.tap_flush()
        self.assertFalse(os.path.exists(self.path))

    # ── fail-open ──────────────────────────────────────────────────────────
    def test_a_raising_tap_does_not_disturb_the_response(self):
        os.environ["FERRY_EVENTS"] = "on"
        FF.reset_tap(self.path)
        app = self._app()
        mw = LaneCatalogueFilter(app, LANES)

        def boom(_rec):
            raise RuntimeError("tap exploded")

        FF._tap().offer = boom
        sent, _ = asyncio.run(drive(mw, "/v1/chat/completions"))
        self.assertEqual([m["type"] for m in sent],
                         ["http.response.start"] + ["http.response.body"] * 3)

    def test_a_non_http_scope_is_never_tapped(self):
        os.environ["FERRY_EVENTS"] = "on"
        FF.reset_tap(self.path)
        app = RecordingApp(b"")
        _, send = asyncio.run(
            drive(LaneCatalogueFilter(app, LANES), "/v1/models", scope_type="websocket"))
        self.assertIs(app.seen_send, send)


class MainWorkersTest(unittest.TestCase):
    """--workers: the uvicorn worker count and the multiproc metrics dir.

    litellm's /metrics serves a MultiProcessCollector only when
    PROMETHEUS_MULTIPROC_DIR is set (verified litellm 1.97.0). With workers > 1
    and no such dir, each worker answers a scrape with its own private
    counters and the litellm_* dashboards undercount by ~1/N. main() must set
    the dir itself when asked for more than one worker, and leave the
    environment untouched otherwise (single-worker /metrics needs no dir, and
    an unwanted dir would switch litellm to a collector that has nothing to
    collect from one process).

    uvicorn is faked in sys.modules, so no server ever starts and no litellm
    import happens (the app factory is passed as a string).
    """

    def _run_main(self, argv):
        import types
        captured = {}
        fake = types.ModuleType("uvicorn")
        fake.run = lambda *a, **kw: captured.update(kw)
        sys.modules["uvicorn"] = fake
        try:
            rc = FF.main(argv)
        finally:
            sys.modules.pop("uvicorn", None)
        return rc, captured

    def setUp(self):
        os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
        os.environ.pop("CONFIG_FILE_PATH", None)

    def tearDown(self):
        os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
        os.environ.pop("CONFIG_FILE_PATH", None)

    def test_single_worker_sets_no_multiproc_dir(self):
        rc, captured = self._run_main(["--config", "x.yaml", "--port", "8090"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["workers"], 1)
        self.assertNotIn("PROMETHEUS_MULTIPROC_DIR", os.environ)

    def test_multiple_workers_pass_through_and_set_the_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "ferry-prom-multiproc-9911")
            os.makedirs(stale)
            open(os.path.join(stale, "dead-series.db"), "w").close()
            # gettempdir() caches its first answer, so repoint the cache
            # itself rather than the env var.
            tempfile.tempdir = tmp
            try:
                rc, captured = self._run_main(
                    ["--config", "x.yaml", "--port", "9911", "--workers", "3"])
            finally:
                tempfile.tempdir = None
            self.assertEqual(rc, 0)
            self.assertEqual(captured["workers"], 3)
            d = os.environ["PROMETHEUS_MULTIPROC_DIR"]
            self.assertEqual(d, stale)          # derived per-port under TMPDIR
            self.assertEqual(os.listdir(d), [])  # stale .db files cleared, not merged

    def test_a_preset_multiproc_dir_is_honoured_and_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            preset = os.path.join(tmp, "preset-dir")
            os.makedirs(preset)
            open(os.path.join(preset, "dead-series.db"), "w").close()
            os.environ["PROMETHEUS_MULTIPROC_DIR"] = preset
            try:
                rc, captured = self._run_main(
                    ["--config", "x.yaml", "--port", "8090", "--workers", "2"])
                self.assertEqual(rc, 0)
                self.assertEqual(os.environ["PROMETHEUS_MULTIPROC_DIR"], preset)
                self.assertEqual(os.listdir(preset), [])
            finally:
                os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)


class TestFleetDiscovery(unittest.TestCase):
    """A fleet is DISCOVERED from model_name prefixes, never declared."""

    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_splits_at_the_first_dot_only(self):
        # A provider string with its own dots must not become a fleet: the
        # split is on the LANE name, and only its first dot.
        path = self._write("""
model_list:
  - model_name: domestic.heavy
    litellm_params: {model: chatgpt/responses/gpt-5.6-sol}
  - model_name: domestic.super-flash
    litellm_params: {model: openrouter/~google/gemini-flash-latest}
  - model_name: international.flash-or
    litellm_params: {model: openrouter/~z-ai/glm-flash-latest}
""")
        self.assertEqual(FF.discover_fleets(path), {
            "domestic": {"heavy": "chatgpt/responses/gpt-5.6-sol",
                         "super-flash": "openrouter/~google/gemini-flash-latest"},
            "international": {"flash-or": "openrouter/~z-ai/glm-flash-latest"},
        })

    def test_a_lane_name_with_two_dots_keeps_the_tail(self):
        path = self._write(
            "model_list:\n"
            "  - {model_name: domestic.gpt-3.5, litellm_params: {model: openai/gpt-3.5-turbo}}\n")
        self.assertEqual(FF.discover_fleets(path),
                         {"domestic": {"gpt-3.5": "openai/gpt-3.5-turbo"}})

    def test_undotted_names_are_ignored(self):
        # local-orch / local-sub are shared and unprefixed: they are not a
        # fleet and must never create one.
        path = self._write("""
model_list:
  - {model_name: local-orch, litellm_params: {model: hosted_vllm/qwen}}
  - {model_name: local-sub, litellm_params: {model: hosted_vllm/qwen-sub}}
""")
        self.assertEqual(FF.discover_fleets(path), {})

    def test_the_first_deployment_wins(self):
        path = self._write("""
model_list:
  - {model_name: domestic.flash, litellm_params: {model: first/one}}
  - {model_name: domestic.flash, litellm_params: {model: second/one}}
""")
        self.assertEqual(FF.discover_fleets(path), {"domestic": {"flash": "first/one"}})

    def test_a_missing_model_string_is_an_empty_value_not_a_crash(self):
        path = self._write("model_list:\n  - {model_name: domestic.flash}\n")
        self.assertEqual(FF.discover_fleets(path), {"domestic": {"flash": ""}})

    def test_missing_file_is_empty_not_an_error(self):
        # Fail-open: no fleets => the resolver is a no-op and every name
        # passes through, exactly as before this feature existed.
        self.assertEqual(FF.discover_fleets("/nonexistent/ferry.yaml"), {})
        self.assertEqual(FF.discover_fleets(""), {})

    def test_unparsable_file_is_empty_not_an_error(self):
        path = self._write("model_list: [oops\n  broken: :\n")
        self.assertEqual(FF.discover_fleets(path), {})

    def test_junk_entries_are_skipped_not_fatal(self):
        path = self._write("""
model_list:
  - "just a string"
  - {model_name: domestic.heavy, litellm_params: {model: chatgpt/x}}
""")
        self.assertEqual(FF.discover_fleets(path), {"domestic": {"heavy": "chatgpt/x"}})


class TestFleetGaps(unittest.TestCase):
    """A fleet missing a cloud lane degrades that lane, not the front door."""

    def test_names_each_missing_cloud_lane(self):
        gaps = FF.fleet_gaps({"domestic": {"heavy": "a", "flash": "b", "super-flash": "c"},
                              "international": {"heavy": "d"}})
        self.assertEqual(gaps, ["fleet 'international' has no lane 'flash'",
                                "fleet 'international' has no lane 'super-flash'"])

    def test_a_complete_fleet_reports_nothing(self):
        self.assertEqual(FF.fleet_gaps(
            {"domestic": {"heavy": "a", "flash": "b", "super-flash": "c", "flash-luna": "d"}}), [])

    def test_no_fleets_reports_nothing(self):
        self.assertEqual(FF.fleet_gaps({}), [])


class TestFleetStatePath(unittest.TestCase):
    def test_defaults_beside_the_config(self):
        self.assertEqual(FF.fleet_state_path("/home/x/.config/ferry/litellm.yaml"),
                         "/home/x/.config/ferry/fleets.json")

    def test_the_env_override_wins(self):
        with unittest.mock.patch.dict(
                os.environ, {FF.FLEET_STATE_ENV: "/tmp/other/fleets.json"}):
            self.assertEqual(FF.fleet_state_path("/home/x/.config/ferry/litellm.yaml"),
                             "/tmp/other/fleets.json")

    def test_an_empty_env_value_is_not_an_override(self):
        with unittest.mock.patch.dict(os.environ, {FF.FLEET_STATE_ENV: ""}):
            self.assertEqual(FF.fleet_state_path("/home/x/.config/ferry/litellm.yaml"),
                             "/home/x/.config/ferry/fleets.json")

    def test_an_empty_config_path_puts_the_state_in_the_cwd(self):
        self.assertEqual(FF.fleet_state_path(""), "fleets.json")

FLEETS = {"domestic": {"heavy": "chatgpt/responses/gpt-5.6-sol",
                       "flash": "openrouter/~google/gemini-flash-latest",
                       "super-flash": "openrouter/~google/gemini-flash-latest"},
          "international": {"heavy": "anthropic/k3",
                            "flash": "zai/glm-5.3-flash",
                            "super-flash": "zai/glm-5.3-flash"}}


class TestFleetState(unittest.TestCase):
    """The file is the truth: four uvicorn workers share it, none owns it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "fleets.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _state(self):
        return FF.FleetState(self.path, FLEETS)

    def _write(self, text):
        with open(self.path, "w") as handle:
            handle.write(text)

    def test_missing_file_defaults_to_the_first_fleet(self):
        st = self._state()
        self.assertEqual(st.load(), {"default": "domestic", "clients": {}})
        self.assertEqual(st.default(), "domestic")
        self.assertIsNone(st.selection_for("host"))

    def test_missing_file_with_no_fleets_has_an_empty_default(self):
        st = FF.FleetState(self.path, {})
        self.assertEqual(st.load(), {"default": "", "clients": {}})

    def test_reads_an_existing_document(self):
        self._write('{"default": "international", "clients": {"host": "domestic"}}')
        st = self._state()
        self.assertEqual(st.default(), "international")
        self.assertEqual(st.selection_for("host"), "domestic")
        self.assertIsNone(st.selection_for("stephens-laptop"))

    def test_an_unparsable_file_raises_with_the_path(self):
        # Never a silent fall-through to the default: a corrupt state file is
        # an operator problem and has to say which file.
        self._write("{not json")
        with self.assertRaises(FF.FleetStateError) as caught:
            self._state().load()
        self.assertIn(self.path, str(caught.exception))

    def test_a_non_object_document_raises(self):
        self._write("[1, 2]")
        with self.assertRaises(FF.FleetStateError) as caught:
            self._state().load()
        self.assertIn(self.path, str(caught.exception))

    def test_reloads_when_the_mtime_changes(self):
        self._write('{"default": "domestic", "clients": {}}')
        st = self._state()
        self.assertEqual(st.default(), "domestic")
        self._write('{"default": "international", "clients": {}}')
        # Force a distinct mtime: two writes inside one filesystem timestamp
        # tick would otherwise make this test prove nothing.
        stamp = os.stat(self.path).st_mtime + 5
        os.utime(self.path, (stamp, stamp))
        self.assertEqual(st.default(), "international")

    def test_two_instances_sharing_one_file_see_each_others_writes(self):
        # This is the four-workers property. A writes, B reads, no shared
        # memory between them.
        a, b = self._state(), self._state()
        self.assertEqual(b.default(), "domestic")
        a.set_selection("stephens-laptop", "international")
        self.assertEqual(b.selection_for("stephens-laptop"), "international")
        a.set_default("international")
        self.assertEqual(b.default(), "international")

    def test_set_selection_none_clears_the_entry(self):
        st = self._state()
        st.set_selection("host", "international")
        self.assertEqual(st.selection_for("host"), "international")
        doc = st.set_selection("host", None)
        self.assertEqual(doc["clients"], {})
        self.assertIsNone(self._state().selection_for("host"))

    def test_clearing_an_absent_entry_is_not_an_error(self):
        st = self._state()
        self.assertEqual(st.set_selection("nobody", None)["clients"], {})

    def test_a_write_leaves_no_tmp_file_behind(self):
        st = self._state()
        st.set_selection("host", "international")
        st.set_default("domestic")
        self.assertEqual(sorted(os.listdir(self.dir)), ["fleets.json"])

    def test_a_write_creates_the_file_when_it_was_missing(self):
        self.assertFalse(os.path.exists(self.path))
        self._state().set_default("international")
        with open(self.path) as handle:
            self.assertEqual(json.load(handle),
                             {"default": "international", "clients": {}})

    def test_the_document_shape(self):
        st = self._state()
        st.set_selection("host", "international")
        doc = st.document("host")
        self.assertEqual(doc["you"], "host")
        self.assertEqual(doc["fleet"], "international")
        self.assertEqual(doc["default"], "domestic")
        self.assertEqual(doc["fleets"], FLEETS)
        self.assertEqual(doc["clients"], {"host": "international"})
        self.assertEqual(sorted(doc), ["clients", "default", "fleet", "fleets", "you"])

    def test_the_document_falls_back_to_the_default_for_an_unknown_caller(self):
        doc = self._state().document("stephens-laptop")
        self.assertEqual(doc["fleet"], "domestic")
        self.assertEqual(doc["you"], "stephens-laptop")

    def test_a_document_written_by_a_write_is_returned_by_the_write(self):
        st = self._state()
        self.assertEqual(st.set_default("international"),
                         {"default": "international", "clients": {}})


class TestCallerIdentity(unittest.TestCase):
    """Header > loopback 'host' > peer IP, with a control per rank."""

    def test_the_client_header_wins(self):
        self.assertEqual(FF.caller_identity(
            {"client": ("127.0.0.1", 5000)},
            {FF.CLIENT_HEADER: b"stephens-laptop"}), "stephens-laptop")

    def test_control_without_the_header_loopback_is_host(self):
        self.assertEqual(FF.caller_identity({"client": ("127.0.0.1", 5000)}, {}),
                         FF.HOST_IDENTITY)

    def test_an_empty_header_is_absent(self):
        # tailscale serve and an unset {env:FERRY_FLEET} both produce empty
        # header values; empty must never become an identity of its own.
        self.assertEqual(FF.caller_identity({"client": ("127.0.0.1", 5000)},
                                            {FF.CLIENT_HEADER: b"   "}),
                         FF.HOST_IDENTITY)

    def test_control_off_host_without_the_header_is_the_peer_ip(self):
        self.assertEqual(FF.caller_identity({"client": ("192.168.1.50", 5000)}, {}),
                         "192.168.1.50")

    def test_ipv6_loopback_is_host(self):
        self.assertEqual(FF.caller_identity({"client": ("::1", 5000)}, {}),
                         FF.HOST_IDENTITY)

    def test_no_peer_at_all_is_the_empty_string(self):
        self.assertEqual(FF.caller_identity({}, {}), "")


class TestResolveModel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "fleets.json")
        with open(self.path, "w") as handle:
            handle.write('{"default": "domestic", "clients": {"host": "international"}}')
        self.state = FF.FleetState(self.path, FLEETS)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    # rank 1: an explicit fleet prefix
    def test_an_explicit_fleet_lane_passes_through(self):
        self.assertEqual(
            FF.resolve_model("international.flash-or", "domestic", "host", self.state),
            "international.flash-or")

    def test_control_a_dotted_name_with_an_unknown_prefix_is_not_a_fleet(self):
        # gpt-3.5-turbo has a dot and is not a fleet lane; it must fall through
        # to the "not a cloud lane" rule and pass through untouched, never 400.
        self.assertEqual(FF.resolve_model("gpt-3.5-turbo", "", "host", self.state),
                         "gpt-3.5-turbo")

    # rank 2: local lanes
    def test_local_lanes_are_untouched(self):
        for lane in ("local-orch", "local-sub"):
            self.assertEqual(FF.resolve_model(lane, "domestic", "host", self.state), lane)

    # rank 3: the header
    def test_the_header_beats_the_sticky_selection(self):
        self.assertEqual(FF.resolve_model("heavy", "domestic", "host", self.state),
                         "domestic.heavy")

    def test_control_no_header_uses_the_sticky_selection(self):
        self.assertEqual(FF.resolve_model("heavy", "", "host", self.state),
                         "international.heavy")

    def test_an_empty_header_is_absent(self):
        self.assertEqual(FF.resolve_model("heavy", "   ", "host", self.state),
                         "international.heavy")

    # rank 5: the default
    def test_an_unknown_caller_gets_the_default(self):
        self.assertEqual(FF.resolve_model("flash", "", "stephens-laptop", self.state),
                         "domestic.flash")

    def test_control_a_known_caller_does_not(self):
        self.assertEqual(FF.resolve_model("flash", "", "host", self.state),
                         "international.flash")

    # the legacy names
    def test_legacy_names_resolve_as_heavy(self):
        for legacy in ("orch", "orchestrator"):
            self.assertEqual(FF.resolve_model(legacy, "domestic", "host", self.state),
                             "domestic.heavy")

    # pass-through
    def test_an_unknown_name_is_untouched(self):
        # litellm 404s it exactly as it does today: the resolver never invents
        # a lane for a name it does not recognise.
        self.assertEqual(FF.resolve_model("gpt-4o", "", "host", self.state), "gpt-4o")

    def test_an_empty_model_is_untouched(self):
        self.assertEqual(FF.resolve_model("", "", "host", self.state), "")

    def test_with_no_fleets_the_resolver_is_a_no_op(self):
        # Pre-fleets config: no dotted names, nothing is rewritten and
        # nothing raises, whatever the header or sticky selection says.
        state = FF.FleetState(self.path, {})
        for name in ("heavy", "orch", "flash", "local-orch", "anything"):
            self.assertEqual(FF.resolve_model(name, "domestic", "host", state), name)

    # errors
    def test_an_unknown_header_fleet_raises_with_the_list(self):
        with self.assertRaises(FF.ResolveError) as caught:
            FF.resolve_model("heavy", "nope", "host", self.state)
        self.assertEqual(caught.exception.args[0],
                         "unknown fleet 'nope'; fleets: domestic, international")

    def test_a_stale_sticky_selection_raises_and_never_falls_through(self):
        with open(self.path, "w") as handle:
            handle.write('{"default": "domestic", "clients": {"host": "gone"}}')
        stamp = os.stat(self.path).st_mtime + 5
        os.utime(self.path, (stamp, stamp))
        with self.assertRaises(FF.ResolveError) as caught:
            FF.resolve_model("heavy", "", "host", self.state)
        self.assertEqual(caught.exception.args[0],
                         "unknown fleet 'gone'; fleets: domestic, international")

    def test_a_fleet_missing_the_lane_raises(self):
        state = FF.FleetState(self.path, {"domestic": {"heavy": "a"},
                                          "international": {"heavy": "b"}})
        with self.assertRaises(FF.ResolveError) as caught:
            FF.resolve_model("flash", "domestic", "host", state)
        self.assertEqual(caught.exception.args[0],
                         "fleet 'domestic' has no lane 'flash'")


class TestRewriteBodyModel(unittest.TestCase):
    def test_replaces_the_model_and_keeps_every_other_key(self):
        out = FF.rewrite_body_model(
            json.dumps({"model": "heavy", "messages": [{"role": "user", "content": "hi"}],
                        "stream": True}).encode(),
            "international.heavy")
        doc = json.loads(out)
        self.assertEqual(doc["model"], "international.heavy")
        self.assertEqual(doc["messages"], [{"role": "user", "content": "hi"}])
        self.assertIs(doc["stream"], True)

    def test_uses_compact_separators(self):
        out = FF.rewrite_body_model(b'{"model": "heavy", "n": 1}', "domestic.heavy")
        self.assertEqual(out, b'{"model":"domestic.heavy","n":1}')

    def test_returns_bytes(self):
        self.assertIsInstance(FF.rewrite_body_model(b'{"model":"heavy"}', "domestic.heavy"),
                              bytes)

    def test_unicode_survives_the_round_trip(self):
        out = FF.rewrite_body_model(
            json.dumps({"model": "heavy", "q": "naïve — ok"}).encode(), "domestic.heavy")
        self.assertEqual(json.loads(out)["q"], "naïve — ok")


class TestSynthesizeCatalogue(unittest.TestCase):
    """Bare lane names are synthesized from the caller's resolved fleet."""

    PUBLIC = frozenset({"domestic.heavy", "domestic.flash", "domestic.super-flash",
                        "international.heavy", "international.flash",
                        "local-orch", "local-sub"})

    def _payload(self, *names):
        return json.dumps({"object": "list",
                           "data": [{"id": n, "object": "model", "owned_by": "ferry"}
                                    for n in names]}).encode()

    def test_prepends_one_bare_entry_per_public_cloud_lane_of_the_fleet(self):
        out = FF.synthesize_catalogue(
            self._payload("domestic.heavy", "domestic.flash", "domestic.super-flash",
                          "domestic.flash-luna", "international.heavy",
                          "international.flash", "local-orch", "local-sub"),
            self.PUBLIC, FLEETS, "domestic")
        self.assertEqual(ids(out)[:3], ["heavy", "flash", "super-flash"])
        self.assertNotIn("domestic.flash-luna", ids(out))
        self.assertIn("domestic.heavy", ids(out))
        self.assertIn("local-orch", ids(out))

    def test_control_the_other_fleet_synthesizes_only_its_own_public_lanes(self):
        # international.super-flash is NOT public here, so no bare
        # "super-flash" may appear for a caller on international.
        out = FF.synthesize_catalogue(
            self._payload("domestic.heavy", "international.heavy", "international.flash",
                          "international.super-flash", "local-orch"),
            self.PUBLIC, FLEETS, "international")
        self.assertEqual(ids(out)[:2], ["heavy", "flash"])
        self.assertNotIn("super-flash", ids(out))
        self.assertNotIn("international.super-flash", ids(out))

    def test_a_bare_entry_copies_the_source_entry_with_the_id_replaced(self):
        out = FF.synthesize_catalogue(
            self._payload("domestic.heavy", "local-orch"), self.PUBLIC, FLEETS, "domestic")
        entry = next(e for e in json.loads(out)["data"] if e["id"] == "heavy")
        self.assertEqual(entry, {"id": "heavy", "object": "model", "owned_by": "ferry"})

    def test_a_lane_absent_from_the_upstream_body_is_not_invented(self):
        out = FF.synthesize_catalogue(
            self._payload("domestic.heavy", "local-orch"), self.PUBLIC, FLEETS, "domestic")
        self.assertEqual(ids(out), ["heavy", "domestic.heavy", "local-orch"])

    def test_an_unknown_fleet_synthesizes_nothing_but_still_filters(self):
        out = FF.synthesize_catalogue(
            self._payload("domestic.heavy", "domestic.flash-luna"),
            self.PUBLIC, FLEETS, "nope")
        self.assertEqual(ids(out), ["domestic.heavy"])

    def test_empty_public_set_returns_none(self):
        self.assertIsNone(FF.synthesize_catalogue(
            self._payload("domestic.heavy"), frozenset(), FLEETS, "domestic"))

    def test_non_json_returns_none(self):
        self.assertIsNone(FF.synthesize_catalogue(
            b"<html>gateway timeout</html>", self.PUBLIC, FLEETS, "domestic"))

    def test_unexpected_shape_returns_none(self):
        self.assertIsNone(FF.synthesize_catalogue(
            json.dumps({"error": "nope"}).encode(), self.PUBLIC, FLEETS, "domestic"))
        self.assertIsNone(FF.synthesize_catalogue(
            json.dumps([1, 2]).encode(), self.PUBLIC, FLEETS, "domestic"))

    def test_would_empty_a_populated_catalogue_returns_none(self):
        self.assertIsNone(FF.synthesize_catalogue(
            self._payload("a", "b"), self.PUBLIC, FLEETS, "domestic"))

    def test_nothing_to_do_returns_none(self):
        # Every entry public and nothing to synthesize: hand back the upstream
        # bytes rather than re-serializing them.
        self.assertIsNone(FF.synthesize_catalogue(
            self._payload("local-orch", "local-sub"), self.PUBLIC, FLEETS, "nope"))


class TestBearerOk(unittest.TestCase):
    def test_no_master_key_lets_everything_through(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LITELLM_MASTER_KEY", None)
            self.assertTrue(FF._bearer_ok({}))

    def test_an_empty_master_key_lets_everything_through(self):
        with unittest.mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
            self.assertTrue(FF._bearer_ok({}))

    def test_a_master_key_requires_the_matching_bearer(self):
        with unittest.mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-ferry"}):
            self.assertTrue(FF._bearer_ok({b"authorization": b"Bearer sk-ferry"}))
            self.assertFalse(FF._bearer_ok({b"authorization": b"Bearer wrong"}))
            self.assertFalse(FF._bearer_ok({b"authorization": b"sk-ferry"}))
            self.assertFalse(FF._bearer_ok({}))

    def test_the_bearer_scheme_is_case_insensitive(self):
        with unittest.mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-ferry"}):
            self.assertTrue(FF._bearer_ok({b"authorization": b"bearer sk-ferry"}))


# ── fleets ───────────────────────────────────────────────────────────────────
# The middleware gains one job on the hot path: rewrite the request body's
# `model` from a bare lane name to `<fleet>.<lane>`. The property that must
# survive is the one at the top of this file — the wrapped app still receives
# the caller's ORIGINAL `send` by identity. Only `receive` is ever wrapped, and
# only after the body has already been fully buffered, so nothing on the
# RESPONSE path (a streamed token) passes through new Python.
#
# This task reuses the module-level `FLEETS` constant Task 2 already added to
# this test file; it is not redefined here.


class BodyApp:
    """An ASGI app that drains `receive` and records the exact bytes it saw."""

    def __init__(self):
        self.body = None
        self.more = []
        self.scope = None
        self.seen_send = None
        self.seen_receive = None
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scope = scope
        self.seen_send = send
        self.seen_receive = receive
        buf = b""
        while True:
            msg = await receive()
            buf += msg.get("body", b"")
            self.more.append(bool(msg.get("more_body")))
            if not msg.get("more_body"):
                break
        self.body = buf
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}",
                    "more_body": False})


class FleetHarness(unittest.TestCase):
    """Base: a real FleetState over a real temp fleets.json."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-fleets-")
        self.state_path = os.path.join(self.dir, "fleets.json")
        with open(self.state_path, "w") as handle:
            json.dump({"default": "domestic", "clients": {}}, handle)
        self.state = FF.FleetState(self.state_path, FLEETS)
        # No strip wrapper: these tests are about `receive`, and the strip
        # wraps `send`, which would mask the identity assertion.
        os.environ["FERRY_STRIP_HEADERS"] = "0"

    def tearDown(self):
        os.environ.pop("FERRY_STRIP_HEADERS", None)
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_state(self, doc):
        with open(self.state_path, "w") as handle:
            json.dump(doc, handle)
        # Guarantee a distinct mtime_ns even on a coarse clock.
        st = os.stat(self.state_path)
        os.utime(self.state_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def mw(self, app, state="use", public=frozenset()):
        return LaneCatalogueFilter(
            app, public, fleets=FLEETS,
            state=self.state if state == "use" else state)

    def drive_body(self, mw, path, raw, headers=None, client=("192.168.1.50", 5000)):
        """Send one request with `raw` as the body. Returns the sent messages."""
        scope = {"type": "http", "path": path, "method": "POST",
                 "client": client,
                 "headers": list(headers or [])}
        msgs = [{"type": "http.request", "body": raw, "more_body": False}]
        sent = []

        async def receive():
            return msgs.pop(0) if msgs else {
                "type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        asyncio.run(mw(scope, receive, send))
        return scope, sent


class TestFleetMiddleware(FleetHarness):
    def test_state_none_is_byte_identical_passthrough(self):
        # THE compatibility guarantee: with no fleets in the config the
        # middleware behaves exactly as it did before this feature.
        raw = json.dumps({"model": "heavy", "messages": []}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app, state=None), "/v1/chat/completions", raw)
        self.assertEqual(app.body, raw)

    def test_control_state_set_rewrites_the_same_request(self):
        # The control for the test above: identical input, state set, body moves.
        raw = json.dumps({"model": "heavy", "messages": []}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw)
        self.assertEqual(json.loads(app.body)["model"], "domestic.heavy")

    def test_the_wrapped_app_still_gets_the_original_send(self):
        # The file's headline property, re-asserted WITH state set: we wrap
        # `receive`, never `send`.
        raw = json.dumps({"model": "heavy"}).encode()
        app = BodyApp()
        scope = {"type": "http", "path": "/v1/chat/completions", "method": "POST",
                 "client": ("192.168.1.50", 5000), "headers": []}
        msgs = [{"type": "http.request", "body": raw, "more_body": False}]
        sent = []

        async def receive():
            return msgs.pop(0) if msgs else {
                "type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        asyncio.run(self.mw(app)(scope, receive, send))
        self.assertIs(app.seen_send, send)
        self.assertIsNot(app.seen_receive, receive)

    def test_header_beats_sticky_and_default(self):
        self.write_state({"default": "domestic", "clients": {"laptop": "domestic"}})
        raw = json.dumps({"model": "flash"}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw, headers=[
            (b"x-ferry-fleet", b"international"),
            (b"x-ferry-client", b"laptop")])
        self.assertEqual(json.loads(app.body)["model"], "international.flash")

    def test_control_no_header_falls_to_sticky(self):
        self.write_state({"default": "domestic", "clients": {"laptop": "international"}})
        raw = json.dumps({"model": "flash"}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw, headers=[
            (b"x-ferry-client", b"laptop")])
        self.assertEqual(json.loads(app.body)["model"], "international.flash")

    def test_control_no_sticky_falls_to_default(self):
        self.write_state({"default": "international", "clients": {}})
        raw = json.dumps({"model": "flash"}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw, headers=[
            (b"x-ferry-client", b"laptop")])
        self.assertEqual(json.loads(app.body)["model"], "international.flash")

    def test_an_empty_fleet_header_is_absent_not_an_error(self):
        raw = json.dumps({"model": "flash"}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw, headers=[
            (b"x-ferry-fleet", b"")])
        self.assertEqual(json.loads(app.body)["model"], "domestic.flash")

    def test_a_loopback_peer_is_the_host_identity(self):
        self.write_state({"default": "domestic", "clients": {"host": "international"}})
        raw = json.dumps({"model": "heavy"}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw,
                        client=("127.0.0.1", 5000))
        self.assertEqual(json.loads(app.body)["model"], "international.heavy")

    def test_unknown_fleet_is_a_400_that_names_the_fleets(self):
        raw = json.dumps({"model": "heavy"}).encode()
        app = BodyApp()
        _, sent = self.drive_body(self.mw(app), "/v1/chat/completions", raw,
                                  headers=[(b"x-ferry-fleet", b"nope")])
        start, payload = collect(sent)
        self.assertEqual(start["status"], 400)
        doc = json.loads(payload)
        self.assertEqual(doc["error"]["type"], "ferry_fleet")
        self.assertIn("unknown fleet 'nope'", doc["error"]["message"])
        self.assertIn("domestic", doc["error"]["message"])
        self.assertIn("international", doc["error"]["message"])
        self.assertEqual(app.calls, 0)   # litellm never saw it
        # The wire format is part of the spec, not an accident of _reply.
        self.assertEqual(
            payload,
            b'{"error": {"message": "unknown fleet \'nope\'; fleets: '
            b'domestic, international", "type": "ferry_fleet"}}')

    def test_content_length_is_replaced_to_match_the_new_body(self):
        raw = json.dumps({"model": "heavy", "messages": []}).encode()
        app = BodyApp()
        scope, _ = self.drive_body(
            self.mw(app), "/v1/chat/completions", raw,
            headers=[(b"content-type", b"application/json"),
                     (b"content-length", str(len(raw)).encode())])
        lengths = [v for k, v in scope["headers"] if k.lower() == b"content-length"]
        self.assertEqual(lengths, [str(len(app.body)).encode()])
        self.assertNotEqual(len(app.body), len(raw))
        self.assertIn((b"content-type", b"application/json"), scope["headers"])

    def test_transfer_encoding_is_dropped_when_the_body_is_rewritten(self):
        # After buffering, the request is no longer chunked: leaving
        # transfer-encoding beside a fresh content-length is an invalid pair.
        raw = json.dumps({"model": "heavy", "messages": []}).encode()
        app = BodyApp()
        scope, _ = self.drive_body(
            self.mw(app), "/v1/chat/completions", raw,
            headers=[(b"content-type", b"application/json"),
                     (b"transfer-encoding", b"chunked")])
        names = [k.lower() for k, _v in scope["headers"]]
        self.assertNotIn(b"transfer-encoding", names)
        lengths = [v for k, v in scope["headers"] if k.lower() == b"content-length"]
        self.assertEqual(lengths, [str(len(app.body)).encode()])

    def test_control_transfer_encoding_survives_an_untouched_body(self):
        # The control: same headers, a model that resolves to itself, so the
        # scope is not rewritten at all.
        raw = json.dumps({"model": "local-orch"}).encode()
        app = BodyApp()
        scope, _ = self.drive_body(
            self.mw(app), "/v1/chat/completions", raw,
            headers=[(b"content-type", b"application/json"),
                     (b"transfer-encoding", b"chunked")])
        self.assertEqual(app.body, raw)
        self.assertIn((b"transfer-encoding", b"chunked"), scope["headers"])

    def test_a_non_json_body_is_replayed_byte_identical(self):
        raw = b"<html>not json at all</html>"
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw)
        self.assertEqual(app.body, raw)

    def test_a_model_less_json_body_is_replayed_byte_identical(self):
        raw = b'{"messages": [], "stream":   true}'
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw)
        self.assertEqual(app.body, raw)          # whitespace and all

    def test_a_non_string_model_is_replayed_byte_identical(self):
        raw = b'{"model": 7}'
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw)
        self.assertEqual(app.body, raw)

    def test_a_local_lane_passes_through_untouched(self):
        raw = json.dumps({"model": "local-orch"}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw)
        self.assertEqual(app.body, raw)

    def test_an_already_prefixed_model_passes_through_untouched(self):
        raw = json.dumps({"model": "international.flash-or"}).encode()
        app = BodyApp()
        self.drive_body(self.mw(app), "/v1/chat/completions", raw)
        self.assertEqual(app.body, raw)

    def test_legacy_orchestrator_resolves_to_heavy(self):
        for name in ("orch", "orchestrator"):
            app = BodyApp()
            self.drive_body(self.mw(app), "/v1/chat/completions",
                            json.dumps({"model": name}).encode())
            self.assertEqual(json.loads(app.body)["model"], "domestic.heavy", name)

    def test_messages_and_responses_paths_are_rewritten_too(self):
        for path in ("/v1/messages", "/v1/responses", "/chat/completions"):
            app = BodyApp()
            self.drive_body(self.mw(app), path,
                            json.dumps({"model": "flash"}).encode())
            self.assertEqual(json.loads(app.body)["model"], "domestic.flash", path)

    def test_a_chunked_request_body_is_reassembled_and_replayed_once(self):
        raw = json.dumps({"model": "heavy"}).encode()
        half = len(raw) // 2
        msgs = [{"type": "http.request", "body": raw[:half], "more_body": True},
                {"type": "http.request", "body": raw[half:], "more_body": False}]
        app = BodyApp()
        sent = []

        async def receive():
            return msgs.pop(0) if msgs else {
                "type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        asyncio.run(self.mw(app)({
            "type": "http", "path": "/v1/chat/completions", "method": "POST",
            "client": ("192.168.1.50", 5000), "headers": []}, receive, send))
        self.assertEqual(json.loads(app.body)["model"], "domestic.heavy")
        self.assertEqual(app.more, [False])      # one chunk, not two

    def test_a_non_inference_path_is_not_body_read(self):
        # /health must never be buffered: it is polled every few seconds.
        app = BodyApp()
        scope = {"type": "http", "path": "/health/liveliness", "method": "GET",
                 "client": ("127.0.0.1", 5000), "headers": []}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        asyncio.run(self.mw(app)(scope, receive, send))
        self.assertIs(app.seen_receive, receive)

    def test_an_unreadable_state_file_fails_open(self):
        # Doctrine from the module docstring: a broken state file degrades to
        # today's behaviour, it never turns the front door into a 500.
        with open(self.state_path, "w") as handle:
            handle.write("{ not json")
        st = os.stat(self.state_path)
        os.utime(self.state_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        raw = json.dumps({"model": "heavy"}).encode()
        app = BodyApp()
        # The warner is patched out only to keep the suite's output clean; that
        # it FIRES on this path is asserted by test_the_fail_open_path_warns.
        with unittest.mock.patch.object(FF, "_fleet_warn"):
            _, sent = self.drive_body(self.mw(app), "/v1/chat/completions", raw)
        self.assertEqual(app.body, raw)
        self.assertEqual(collect(sent)[0]["status"], 200)

    def test_the_fail_open_path_warns(self):
        # Failing open must not be SILENT: a permanently broken fleets.json
        # would otherwise degrade every client with no signal at all.
        with open(self.state_path, "w") as handle:
            handle.write("{ not json")
        st = os.stat(self.state_path)
        os.utime(self.state_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        raw = json.dumps({"model": "heavy"}).encode()
        app = BodyApp()
        with unittest.mock.patch.object(FF, "_fleet_warn") as warn:
            self.drive_body(self.mw(app), "/v1/chat/completions", raw)
        self.assertEqual(warn.call_count, 1)
        self.assertIsInstance(warn.call_args[0][0], FF.FleetStateError)


class TestFleetWarn(unittest.TestCase):
    """One line per distinct failure per interval — a signal, not a flood."""

    def setUp(self):
        FF._FLEET_WARNED.clear()

    def tearDown(self):
        FF._FLEET_WARNED.clear()

    def test_the_first_failure_writes_one_line(self):
        out = io.StringIO()
        self.assertTrue(FF._fleet_warn(ValueError("boom"), stream=out,
                                       clock=lambda: 100.0))
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)
        self.assertIn("boom", out.getvalue())
        self.assertIn("ValueError", out.getvalue())

    def test_the_same_failure_inside_the_interval_is_silent(self):
        out = io.StringIO()
        FF._fleet_warn(ValueError("boom"), stream=out, clock=lambda: 100.0)
        out.truncate(0)
        out.seek(0)
        self.assertFalse(FF._fleet_warn(ValueError("boom"), stream=out,
                                        clock=lambda: 100.0 + 59))
        self.assertEqual(out.getvalue(), "")

    def test_the_same_failure_after_the_interval_writes_again(self):
        out = io.StringIO()
        FF._fleet_warn(ValueError("boom"), stream=out, clock=lambda: 100.0)
        out.truncate(0)
        out.seek(0)
        self.assertTrue(FF._fleet_warn(ValueError("boom"), stream=out,
                                       clock=lambda: 100.0 + 61))
        self.assertIn("boom", out.getvalue())

    def test_a_different_failure_writes_immediately(self):
        out = io.StringIO()
        FF._fleet_warn(ValueError("boom"), stream=out, clock=lambda: 100.0)
        out.truncate(0)
        out.seek(0)
        self.assertTrue(FF._fleet_warn(ValueError("other"), stream=out,
                                       clock=lambda: 100.0))
        self.assertIn("other", out.getvalue())


class TestFleetCatalogue(FleetHarness):
    PUBLIC = frozenset({
        "domestic.heavy", "domestic.flash", "domestic.super-flash",
        "international.heavy", "international.flash", "international.super-flash",
        "local-orch", "local-sub"})

    def _payload(self, *names):
        # Own builder, not the shared `body()` helper: `body()` emits only
        # `{"id": i, "object": "model"}`, which is byte-identical across
        # fleets, so a fleet-carrying field is needed for the dom/intl and
        # sticky/plain assertions below to mean anything. Mirrors Task 3's
        # `_payload`, with `owned_by` set to the real per-lane model string
        # (from FLEETS) so `domestic.flash` and `international.flash` differ.
        def owner(name):
            if "." in name:
                fleet, lane = name.split(".", 1)
                return FLEETS.get(fleet, {}).get(lane, name)
            return name
        return json.dumps({"object": "list",
                           "data": [{"id": n, "object": "model", "owned_by": owner(n)}
                                    for n in names]}).encode()

    def _list(self, headers=None, client=("192.168.1.50", 5000), state="use"):
        payload = self._payload("domestic.heavy", "domestic.flash", "domestic.super-flash",
                       "international.heavy", "international.flash",
                       "international.super-flash", "local-orch", "local-sub",
                       "domestic.flash-luna")
        app = RecordingApp(payload)
        mw = self.mw(app, state=state, public=self.PUBLIC)
        scope = {"type": "http", "path": "/v1/models", "method": "GET",
                 "client": client, "headers": list(headers or [])}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        asyncio.run(mw(scope, receive, send))
        return collect(sent)

    def test_the_default_fleet_gets_bare_lanes_prepended(self):
        _, payload = self._list()
        listed = ids(payload)
        self.assertEqual(listed[:3], ["heavy", "flash", "super-flash"])
        self.assertIn("international.flash", listed)
        self.assertNotIn("domestic.flash-luna", listed)   # hops stay hidden

    def test_the_header_fleet_decides_which_lane_the_bare_name_shadows(self):
        # Same request, one header apart: the CONTROL for the test above. The
        # bare names are identical; what differs is which fleet they came from,
        # which the entry's own copied fields carry.
        _, dom = self._list(headers=[(b"x-ferry-fleet", b"domestic")])
        _, intl = self._list(headers=[(b"x-ferry-fleet", b"international")])
        self.assertEqual(ids(dom)[:3], ids(intl)[:3])
        self.assertNotEqual(dom, intl)

    def test_a_sticky_selection_picks_the_fleet(self):
        self.write_state({"default": "domestic", "clients": {"laptop": "international"}})
        _, sticky = self._list(headers=[(b"x-ferry-client", b"laptop")])
        _, plain = self._list(headers=[(b"x-ferry-client", b"other")])
        self.assertNotEqual(sticky, plain)
        self.assertEqual(ids(sticky)[:3], ["heavy", "flash", "super-flash"])

    def test_state_none_still_only_filters(self):
        _, payload = self._list(state=None)
        listed = ids(payload)
        self.assertNotIn("heavy", listed)
        self.assertIn("domestic.heavy", listed)
        self.assertNotIn("domestic.flash-luna", listed)

    def test_an_unknown_header_fleet_falls_back_to_plain_filtering(self):
        # Fail-open: the catalogue is a read, not a routing decision. A bad
        # header gets its 400 on the next inference call, where it matters.
        # And it is SILENT: an unknown name is an expected fallback, not a
        # degradation, so it must not spend the warner's rate-limit budget.
        with unittest.mock.patch.object(FF, "_fleet_warn") as warn:
            _, payload = self._list(headers=[(b"x-ferry-fleet", b"nope")])
        warn.assert_not_called()
        listed = ids(payload)
        self.assertNotIn("heavy", listed)
        self.assertIn("domestic.heavy", listed)

    def test_an_unreadable_state_file_warns_once_and_still_filters(self):
        # The catalogue path must be symmetric with the inference path: a
        # broken fleets.json degrades to plain filtering AND says so once,
        # rather than degrading every listing with no signal at all.
        with open(self.state_path, "w") as handle:
            handle.write("{ not json")
        st = os.stat(self.state_path)
        os.utime(self.state_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        with unittest.mock.patch.object(FF, "_fleet_warn") as warn:
            _, payload = self._list()
        self.assertEqual(warn.call_count, 1)
        self.assertIsInstance(warn.call_args[0][0], FF.FleetStateError)
        # The body is the plain-filtered catalogue: same ids as the state=None
        # control, which is what "fails open" has to MEAN here.
        _, control = self._list(state=None)
        self.assertEqual(ids(payload), ids(control))
        self.assertNotIn("heavy", ids(payload))

    def test_content_length_matches_the_synthesized_body(self):
        start, payload = self._list()
        length = dict((k.lower(), v) for k, v in start["headers"])[b"content-length"]
        self.assertEqual(int(length), len(payload))


if __name__ == "__main__":
    unittest.main(verbosity=2)
