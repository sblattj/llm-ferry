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
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "ferry-events.ndjson")

    def tearDown(self):
        FF.reset_tap(None)
        if self._prev is None:
            os.environ.pop("FERRY_EVENTS", None)
        else:
            os.environ["FERRY_EVENTS"] = self._prev

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
        _, with_tap, _ = self._drive(True)
        _, without, _ = self._drive(False)
        self.assertEqual(with_tap, without)

    def test_chunk_boundaries_and_more_body_survive(self):
        _, sent, _ = self._drive(True)
        bodies = [(m.get("body"), m.get("more_body")) for m in sent
                  if m["type"] == "http.response.body"]
        self.assertEqual(bodies, [(b"a", True), (b"bb", True), (b"ccc", False)])

    def test_headers_are_not_rewritten_on_the_hot_path(self):
        _, sent, _ = self._drive(True)
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual(start["headers"], list(self.ATTRIB))
        self.assertNotIn(b"content-length", dict(start["headers"]))

    def test_tap_off_still_hands_over_the_original_send(self):
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

    def test_the_app_is_not_wrapped_when_there_is_nothing_to_do(self):
        # The control: with no public lanes AND no tap, plain litellm is handed
        # back untouched, which is the behaviour that predates this change.
        os.environ["FERRY_EVENTS"] = "off"
        self.assertFalse(FF.should_wrap(frozenset()))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
