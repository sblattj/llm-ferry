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


if __name__ == "__main__":
    unittest.main(verbosity=2)
