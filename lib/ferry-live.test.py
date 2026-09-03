"""Tests for the live view: the extended topology parse and the event tail.

Run: python3 lib/ferry-live.test.py
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path, name):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


D = _load(os.path.join(ROOT, "ferry-dash"), "ferry_dash")
L = _load(os.path.join(ROOT, "lib", "ferry_live.py"), "ferry_live")


# A pool (two deployments under one model_name), a chain, a public marker, and
# explicit model_info ids — the four things the live view needs to render.
CONFIG = """
model_list:
  - model_name: flash
    litellm_params:
      model: someprovider/some-model
      api_base: https://api.example.invalid/v1
    model_info:
      id: flash-a
      public: true
  - model_name: flash
    litellm_params:
      model: someprovider/some-other-model
    model_info:
      id: flash-b
  - model_name: backup
    litellm_params:
      model: otherprovider/backup-model
    model_info:
      id: backup-1
  - model_name: local-sub
    litellm_params:
      model: openai/some-local-model
      api_base: http://127.0.0.1:8093/v1
    model_info:
      id: local-sub-mlx
      public: true      # a trailing comment is the shape the REAL config uses

router_settings:
  routing_strategy: usage-based-routing-v2
  fallbacks: [{"flash": ["backup"]}]
"""


class TestTopologyExtension(unittest.TestCase):
    """The parse gains three keys. It must not lose the ones the route EDITOR
    validates its writes against — lib/ferry-dashroutes.test.py is the control
    that proves that, and it must stay green."""

    def setUp(self):
        self.t = D.parse_topology_text(CONFIG)

    # ── what already existed, unchanged ────────────────────────────────────
    def test_the_pool_is_still_detected_by_count(self):
        self.assertEqual(self.t["groups"]["flash"]["count"], 2)
        self.assertEqual(self.t["groups"]["backup"]["count"], 1)

    def test_the_chain_is_still_parsed(self):
        self.assertEqual(self.t["fallbacks"]["flash"], ["backup"])

    def test_models_and_order_and_routing_survive(self):
        self.assertEqual(self.t["groups"]["flash"]["models"],
                         ["someprovider/some-model", "someprovider/some-other-model"])
        self.assertEqual(self.t["order"][0], "flash")
        self.assertEqual(self.t["routing"]["routing_strategy"], "usage-based-routing-v2")

    def test_the_key_set_is_a_superset_not_a_replacement(self):
        self.assertTrue({"error", "fallbacks", "groups", "order", "routing"}
                        <= set(self.t))

    # ── what is new ────────────────────────────────────────────────────────
    def test_deployment_ids_are_captured_in_file_order(self):
        # model_info.id is what an event's x-litellm-model-id carries, so it is
        # the ONLY key that joins a live event back to a configured deployment.
        self.assertEqual(self.t["groups"]["flash"]["ids"], ["flash-a", "flash-b"])
        self.assertEqual(self.t["groups"]["local-sub"]["ids"], ["local-sub-mlx"])

    def test_public_is_true_when_any_deployment_marks_it(self):
        self.assertTrue(self.t["groups"]["flash"]["public"])
        self.assertTrue(self.t["groups"]["local-sub"]["public"])
        self.assertFalse(self.t["groups"]["backup"]["public"])

    def test_providers_use_the_same_rule_as_an_event(self):
        # One rule, one place: the live view joins topology to events by
        # provider, so two different derivations would silently fail to match.
        self.assertEqual(self.t["groups"]["backup"]["providers"], ["otherprovider"])
        self.assertEqual(self.t["groups"]["local-sub"]["providers"], ["local"])

    def test_a_deployment_with_no_explicit_id_yields_an_empty_slot(self):
        # An unset model_info.id means litellm generates a hash, which cannot be
        # matched against the config. Recording the gap is how the view can say
        # so rather than silently failing to join.
        t = D.parse_topology_text(
            "model_list:\n"
            "  - model_name: x\n    litellm_params:\n      model: p/m\n")
        self.assertEqual(t["groups"]["x"]["ids"], [""])


class TestLanes(unittest.TestCase):
    def test_lanes_resolves_a_chain_into_ordered_hops(self):
        lanes = L.lanes(D.parse_topology_text(CONFIG))
        flash = next(l for l in lanes if l["name"] == "flash")
        self.assertEqual([h["name"] for h in flash["hops"]], ["flash", "backup"])

    def test_the_primary_hop_is_the_lane_itself(self):
        lanes = L.lanes(D.parse_topology_text(CONFIG))
        flash = next(l for l in lanes if l["name"] == "flash")
        self.assertEqual(flash["hops"][0]["name"], "flash")
        self.assertTrue(flash["hops"][0]["is_pool"])
        self.assertEqual(flash["hops"][0]["pool_size"], 2)

    def test_a_lane_with_no_chain_is_a_single_hop(self):
        lanes = L.lanes(D.parse_topology_text(CONFIG))
        sub = next(l for l in lanes if l["name"] == "local-sub")
        self.assertEqual(len(sub["hops"]), 1)
        self.assertFalse(sub["hops"][0]["is_pool"])

    def test_public_lanes_are_flagged_for_the_default_view(self):
        lanes = L.lanes(D.parse_topology_text(CONFIG))
        self.assertEqual({l["name"] for l in lanes if l["public"]},
                         {"flash", "local-sub"})

    def test_a_hop_naming_a_missing_backend_is_reported_not_dropped(self):
        # A chain hop that resolves to nothing is the exact class of bug the
        # dashboard exists to surface; silently omitting it hides one.
        t = D.parse_topology_text(
            "model_list:\n"
            "  - model_name: a\n    litellm_params:\n      model: p/m\n"
            'router_settings:\n  fallbacks: [{"a": ["ghost"]}]\n')
        lane = next(l for l in L.lanes(t) if l["name"] == "a")
        ghost = lane["hops"][1]
        self.assertEqual(ghost["name"], "ghost")
        self.assertTrue(ghost["missing"])


class TestChains(unittest.TestCase):
    """The lane -> ordered-deployment-id map.

    This is what attributes an event's hop_errors back to the backends that
    produced them, so it lives in ONE place: `ferry-dash` renders from it and
    `observ/ferry-metrics-exporter` counts fallback edges from it. Two
    derivations that drifted apart would blame a healthy backend, which is
    worse than saying nothing.
    """

    def test_each_lane_maps_to_the_ids_of_its_hops_in_order(self):
        c = L.chains(D.parse_topology_text(CONFIG))
        self.assertEqual(c["flash"], ["flash-a", "backup-1"])
        self.assertEqual(c["local-sub"], ["local-sub-mlx"])

    def test_a_pool_hop_contributes_only_its_first_deployment(self):
        # litellm splits a pool across its members rather than trying them in
        # order, so there is no "next" member to attribute a hop error to. The
        # first id stands for the hop; anything finer would be invented.
        c = L.chains(D.parse_topology_text(CONFIG))
        self.assertEqual(c["flash"][0], "flash-a")

    def test_a_hop_with_no_configured_id_leaves_an_empty_slot(self):
        # An empty slot stops attribution at that hop instead of shifting every
        # later hop onto the wrong backend.
        t = D.parse_topology_text(
            "model_list:\n"
            "  - model_name: a\n    litellm_params:\n      model: p/m\n"
            "  - model_name: b\n    litellm_params:\n      model: p/n\n"
            "    model_info:\n      id: b-1\n"
            'router_settings:\n  fallbacks: [{"a": ["b"]}]\n')
        self.assertEqual(L.chains(t)["a"], ["", "b-1"])

    def test_a_missing_hop_still_occupies_its_position(self):
        t = D.parse_topology_text(
            "model_list:\n"
            "  - model_name: a\n    litellm_params:\n      model: p/m\n"
            "    model_info:\n      id: a-1\n"
            'router_settings:\n  fallbacks: [{"a": ["ghost"]}]\n')
        self.assertEqual(L.chains(t)["a"], ["a-1", ""])


class TestEventTail(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ferry-events.ndjson")

    def _append(self, *recs):
        with open(self.path, "a") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def test_a_tail_starts_at_eof_and_ignores_history(self):
        self._append({"lane": "old"})
        tail = L.EventTail(self.path)
        self.assertEqual(tail.read_new(), [])
        self._append({"lane": "new"})
        self.assertEqual([r["lane"] for r in tail.read_new()], ["new"])

    def test_a_missing_file_is_empty_not_an_error(self):
        tail = L.EventTail(os.path.join(self.dir, "absent.ndjson"))
        self.assertEqual(tail.read_new(), [])

    def test_a_half_written_line_is_buffered_until_its_newline(self):
        L.EventTail(self.path)
        tail = L.EventTail(self.path)
        with open(self.path, "a") as fh:
            fh.write('{"lane": "par')
        self.assertEqual(tail.read_new(), [])
        with open(self.path, "a") as fh:
            fh.write('tial"}\n')
        self.assertEqual([r["lane"] for r in tail.read_new()], ["partial"])

    def test_rotation_rereads_the_new_file_from_zero(self):
        self._append({"lane": "a"})
        tail = L.EventTail(self.path)
        tail.read_new()
        os.replace(self.path, self.path + ".1")
        time.sleep(0.01)
        self._append({"lane": "after-rotation"})
        self.assertEqual([r["lane"] for r in tail.read_new()], ["after-rotation"])

    def test_a_corrupt_line_is_skipped_not_fatal(self):
        tail = L.EventTail(self.path)
        with open(self.path, "a") as fh:
            fh.write("not json\n")
            fh.write('{"lane": "good"}\n')
        self.assertEqual([r["lane"] for r in tail.read_new()], ["good"])

    def test_sse_frame_is_a_data_line_and_a_blank_line(self):
        frame = L.sse_frame({"lane": "flash"})
        self.assertTrue(frame.startswith(b"data: "))
        self.assertTrue(frame.endswith(b"\n\n"))
        self.assertEqual(json.loads(frame[6:].decode())["lane"], "flash")

    def test_sse_frame_never_emits_an_embedded_newline(self):
        # A newline inside the payload would split one event into two frames and
        # desynchronise the client for the rest of the stream.
        frame = L.sse_frame({"lane": "a\nb", "msg": "x\ny"})
        self.assertEqual(frame.count(b"\n"), 2)


class TestThroughput(unittest.TestCase):
    """The per-request bytes/s proxy the dash renders.

    This is BYTES over duration, never a tokens/s claim: the tap counts body
    lengths because the event headers carry no token counts. `bps_of` is the
    one formula and it lives here, in python, so the browser never re-derives
    it — `ferry-dash` attaches the result to each SSE frame.
    """

    def test_bytes_over_duration_is_bytes_per_second(self):
        self.assertEqual(L.bps_of({"resp_bytes": 3000, "duration_ms": 1500}),
                         2000.0)

    def test_a_non_positive_duration_cannot_produce_a_rate(self):
        self.assertIsNone(L.bps_of({"resp_bytes": 3000, "duration_ms": 0}))
        self.assertIsNone(L.bps_of({"resp_bytes": 3000, "duration_ms": -5}))

    def test_a_missing_or_null_duration_yields_no_rate(self):
        self.assertIsNone(L.bps_of({"resp_bytes": 3000, "duration_ms": None}))
        self.assertIsNone(L.bps_of({"resp_bytes": 3000}))

    def test_a_record_without_resp_bytes_yields_no_rate(self):
        self.assertIsNone(L.bps_of({"duration_ms": 100}))
        self.assertIsNone(L.bps_of({}))

    def test_zero_bytes_is_no_data_not_zero_speed(self):
        # 0 is the default the record ships with, so it means "never counted"
        # (old event file, untapped proxy) exactly as often as it means an
        # empty body. Rendering it as 0 B/s would dress "cannot know" up as a
        # relay that moved no bytes.
        self.assertIsNone(L.bps_of({"resp_bytes": 0, "duration_ms": 100}))

    def test_a_non_numeric_field_is_refused_not_coerced(self):
        # The record contract types both fields; a string would mean a
        # different producer, and guessing formats here would paper over it.
        self.assertIsNone(L.bps_of({"resp_bytes": "100", "duration_ms": 100}))
        self.assertIsNone(L.bps_of({"resp_bytes": 100, "duration_ms": "100"}))


RULES = {
    "version": 1,
    "ttl": {"rate_limited": 60, "unreachable": 120, "unknown": 300},
    "rules": [
        {"state": "quota_exhausted", "status": [429, 403],
         "message_contains": ["usage limit", "quota", "insufficient balance"]},
        {"state": "quota_exhausted", "status": [402]},
        {"state": "auth_dead", "status": [401]},
        {"state": "auth_dead", "status": [403],
         "message_contains": ["invalid api key", "unauthorized"]},
        {"state": "rate_limited", "status": [429]},
        {"state": "unreachable", "type_contains": ["APIConnectionError", "Timeout"]},
    ],
}


class TestExhaustion(unittest.TestCase):
    """Per-deployment health folded out of the event stream.

    NOTE ON SHAPE: an event's `deployment` is the hop that SUCCEEDED, and its
    `hop_errors` belong to the hops tried before it, in order. So a fallback
    from d1 to d2 is `deployment="d2"` with one hop error, and `chain=["d1",
    "d2"]` is what maps that error back to d1. An event naming the same
    deployment as both server and failure cannot occur.
    """

    def _s(self, now=1000.0):
        return L.ExhaustionState(RULES, now=lambda: now)

    def _ev(self, served, status=200, hop_errors=None, lane="flash"):
        return {"lane": lane, "deployment": served, "status": status,
                "provider": "someprovider", "hop_errors": hop_errors or [],
                "fallbacks": len(hop_errors or [])}

    def _fellback(self, message, code="429", typ="RateLimitError"):
        """d1 failed with this error; d2 served."""
        return (self._ev("d2", 200,
                         [{"code": code, "type": typ, "message": message}]),
                ["d1", "d2"])

    def test_a_success_is_healthy(self):
        s = self._s()
        s.observe(self._ev("d1"))
        self.assertEqual(s.snapshot()["d1"]["state"], "healthy")

    def test_quota_wording_on_a_429_is_quota_exhausted_not_rate_limited(self):
        s = self._s()
        s.observe(*self._fellback("you have hit your weekly usage limit"))
        self.assertEqual(s.snapshot()["d1"]["state"], "quota_exhausted")

    def test_a_plain_429_is_only_rate_limited(self):
        s = self._s()
        s.observe(*self._fellback("slow down"))
        self.assertEqual(s.snapshot()["d1"]["state"], "rate_limited")

    def test_a_401_is_auth_dead(self):
        s = self._s()
        s.observe(*self._fellback("nope", code="401", typ="AuthenticationError"))
        self.assertEqual(s.snapshot()["d1"]["state"], "auth_dead")

    def test_a_connection_error_is_unreachable(self):
        s = self._s()
        s.observe(*self._fellback("refused", code="500", typ="APIConnectionError"))
        self.assertEqual(s.snapshot()["d1"]["state"], "unreachable")

    def test_an_unmatched_error_is_unknown_never_healthy(self):
        # Silently calling an unrecognised failure "healthy" is how a real
        # outage hides behind a green dashboard.
        s = self._s()
        s.observe(*self._fellback("???", code="418", typ="TeapotError"))
        self.assertEqual(s.snapshot()["d1"]["state"], "unknown")

    def test_the_provider_message_is_kept_verbatim(self):
        # The dashboard shows the provider's OWN words beside the inferred
        # state. ferry-dash used to print a hardcoded sentence naming one vendor
        # and one status code no matter what had actually happened.
        s = self._s()
        s.observe(*self._fellback("weekly usage limit reached; resets Monday"))
        self.assertEqual(s.snapshot()["d1"]["detail"],
                         "weekly usage limit reached; resets Monday")

    def test_hop_errors_attribute_to_the_failed_hop_not_the_server(self):
        s = self._s()
        s.observe(self._ev("d3", 200, [
            {"code": "429", "type": "RateLimitError", "message": "quota gone"},
            {"code": "401", "type": "AuthenticationError", "message": "bad key"}]),
            chain=["d1", "d2", "d3"])
        snap = s.snapshot()
        self.assertEqual(snap["d1"]["state"], "quota_exhausted")
        self.assertEqual(snap["d2"]["state"], "auth_dead")
        self.assertEqual(snap["d3"]["state"], "healthy")

    def test_without_a_chain_failures_are_not_attributed_to_anyone(self):
        # The safe direction. Guessing which deployment a hop error belongs to
        # would blame a healthy backend.
        s = self._s()
        s.observe(self._ev("d2", 200, [
            {"code": "429", "type": "RateLimitError", "message": "usage limit"}]))
        snap = s.snapshot()
        self.assertEqual(list(snap), ["d2"])
        self.assertEqual(snap["d2"]["state"], "healthy")

    def test_one_deployments_429_does_not_erase_anothers_outage(self):
        # The defect this replaces: ferry-dash kept ONE global last_event, so
        # any new backend event wiped a still-live outage on a different one.
        s = self._s()
        s.observe(*self._fellback("usage limit"))
        s.observe(self._ev("d4", 200, [
            {"code": "429", "type": "RateLimitError", "message": "slow down"}]),
            chain=["d3", "d4"])
        snap = s.snapshot()
        self.assertEqual(snap["d1"]["state"], "quota_exhausted")
        self.assertEqual(snap["d3"]["state"], "rate_limited")

    def test_quota_exhausted_is_sticky_and_a_success_clears_it(self):
        # A weekly quota does not clear on the 60s decay that suits a 429.
        clock = [1000.0]
        s = L.ExhaustionState(RULES, now=lambda: clock[0])
        s.observe(*self._fellback("usage limit"))
        clock[0] += 86400
        self.assertEqual(s.snapshot()["d1"]["state"], "quota_exhausted")
        s.observe(self._ev("d1", 200))
        self.assertEqual(s.snapshot()["d1"]["state"], "healthy")

    def test_rate_limited_decays_on_its_ttl(self):
        clock = [1000.0]
        s = L.ExhaustionState(RULES, now=lambda: clock[0])
        s.observe(*self._fellback("slow down"))
        self.assertEqual(s.snapshot()["d1"]["state"], "rate_limited")
        clock[0] += 61
        self.assertEqual(s.snapshot()["d1"]["state"], "healthy")

    def test_since_is_the_first_moment_of_the_current_state_not_the_latest(self):
        clock = [1000.0]
        s = L.ExhaustionState(RULES, now=lambda: clock[0])
        s.observe(*self._fellback("usage limit"))
        clock[0] += 30
        s.observe(*self._fellback("usage limit"))
        self.assertEqual(s.snapshot()["d1"]["since"], 1000.0)

    def test_a_deployment_never_seen_is_absent_rather_than_guessed(self):
        s = self._s()
        self.assertNotIn("never-seen", s.snapshot())

    def test_a_failing_response_marks_the_serving_deployment_too(self):
        s = self._s()
        s.observe(self._ev("d1", 429))
        self.assertEqual(s.snapshot()["d1"]["state"], "rate_limited")

    def test_rules_load_from_a_file_and_a_missing_file_is_not_fatal(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "event-rules.json")
        self.assertEqual(L.load_rules(p)["rules"], [])
        with open(p, "w") as fh:
            json.dump(RULES, fh)
        self.assertEqual(len(L.load_rules(p)["rules"]), 6)

    def test_a_corrupt_rules_file_yields_no_rules_rather_than_raising(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "event-rules.json")
        with open(p, "w") as fh:
            fh.write("{not json")
        self.assertEqual(L.load_rules(p)["rules"], [])

    def test_with_no_rules_every_failure_is_unknown_not_healthy(self):
        # The consequence of a missing rules file, made explicit: unrecognised,
        # therefore visible.
        s = L.ExhaustionState({"rules": [], "ttl": {}}, now=lambda: 1000.0)
        s.observe(*self._fellback("usage limit"))
        self.assertEqual(s.snapshot()["d1"]["state"], "unknown")

    def test_a_rule_with_no_conditions_is_ignored(self):
        # A rule matching everything is a bug, not a catch-all.
        s = L.ExhaustionState(
            {"rules": [{"state": "auth_dead"}], "ttl": {}}, now=lambda: 1000.0)
        s.observe(*self._fellback("anything"))
        self.assertEqual(s.snapshot()["d1"]["state"], "unknown")

    def test_the_shipped_example_rules_file_parses(self):
        # It is documentation AND a template; a broken one is worse than none.
        example = os.path.join(ROOT, "event-rules.example.json")
        self.assertTrue(os.path.exists(example), example)
        self.assertTrue(L.load_rules(example)["rules"])

    def test_the_shipped_example_classifies_the_states_it_documents(self):
        rules = L.load_rules(os.path.join(ROOT, "event-rules.example.json"))
        self.assertEqual(L.classify(rules, 429, "", "monthly quota reached"),
                         "quota_exhausted")
        self.assertEqual(L.classify(rules, 429, "", "too many requests"),
                         "rate_limited")
        self.assertEqual(L.classify(rules, 401, "", ""), "auth_dead")
        self.assertEqual(L.classify(rules, 500, "APIConnectionError", ""),
                         "unreachable")
        self.assertEqual(L.classify(rules, 418, "TeapotError", "hm"), "unknown")

    def test_the_shipped_example_names_no_real_vendor(self):
        # This repo is public. A rules template is as published as the README.
        text = open(os.path.join(ROOT, "event-rules.example.json")).read().lower()
        for vendor in ("kimi", "z.ai", "glm", "gemini", "deepseek",
                       "openrouter", "fireworks", "anthropic"):
            self.assertNotIn(vendor, text, vendor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
