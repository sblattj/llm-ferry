#!/usr/bin/env python3
"""Stdlib unittest for the `ferry dash` route editor — the config WRITER.

Run:  python3 lib/ferry-dashroutes.test.py

The dashboard has been able to READ lane topology out of litellm.yaml since v1.5.
This suite covers the half that can destroy something: writing a reordered
failover chain back.

The whole risk is one property. A ferry litellm.yaml is roughly two-thirds
commentary — the ToS warning behind a real account suspension, a duplicate-key
trap that silently conflated two providers' metrics, measured numbers telling a
future reader not to re-tune a parameter that was already tested. None of it is
recoverable from the config's data, and a yaml round-trip through any library
deletes all of it. So the writer never round-trips: it rewrites ONE anchored line
and passes every other byte through.

test_comments_survive is therefore the point of this file, and it ships with a
control (test_the_comment_check_can_fail) because a preservation assertion that
cannot fail proves nothing — which is exactly how a check like this rots.
"""
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ferry-dash has no .py extension; load it as a module by path.
_spec = importlib.util.spec_from_loader(
    "ferrydash",
    importlib.machinery.SourceFileLoader("ferrydash", os.path.join(REPO, "ferry-dash")),
)
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)


CONFIG = """\
# ---------------------------------------------------------------------------
# THE FERRY STACK — a comment block that must survive every write.
# Google APIs ToS 2.d: do not widen a pool with more projects.
# ---------------------------------------------------------------------------

model_list:
  # The driver lane. Its comment explains a trap.
  - model_name: heavy
    litellm_params:
      model: anthropic/k3
    model_info:
      public: true
      id: kimi-1

  - model_name: heavy-glm
    litellm_params:
      model: zai/glm-5.3
    model_info:
      id: glm-1

  # The worker lane.
  - model_name: flash
    litellm_params:
      model: zai/glm-5.3-flash
    model_info:
      public: true
      id: flash-1

  - model_name: flash-or
    litellm_params:
      model: openrouter/some/model
    model_info:
      id: or-1

litellm_settings:
  drop_params: true

router_settings:
  routing_strategy: usage-based-routing-v2
  # A comment directly above the anchor line, which must not move.
  fallbacks: [{"heavy": ["heavy-glm"]}, {"flash": ["flash-or"]}]
  # And one directly below it.
  cooldown_time: 5
"""


def comments(text):
    return [l for l in text.splitlines() if l.lstrip().startswith("#")]


class SpliceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "litellm.yaml")
        with open(self.path, "w") as f:
            f.write(CONFIG)

    # ── the property this file exists for ─────────────────────────────────
    def test_comments_survive(self):
        new = D.splice_fallbacks(CONFIG, {"heavy": ["heavy-glm"], "flash": ["flash-or", "heavy"]})
        self.assertEqual(comments(CONFIG), comments(new))

    def test_the_comment_check_can_fail(self):
        """Control. If deleting a comment does NOT change the comparison, the
        assertion above is vacuous and every later regression walks past it."""
        new = D.splice_fallbacks(CONFIG, {"heavy": ["heavy-glm"], "flash": ["flash-or"]})
        sabotaged = "\n".join(
            l for l in new.splitlines() if "ToS 2.d" not in l)
        self.assertNotEqual(comments(CONFIG), comments(sabotaged))

    def test_only_the_fallbacks_line_changes(self):
        new = D.splice_fallbacks(CONFIG, {"heavy": ["heavy-glm"], "flash": ["heavy"]})
        before, after = CONFIG.splitlines(), new.splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(differing), 1, "more than one line changed")
        self.assertIn("fallbacks:", before[differing[0]])

    def test_new_chain_is_present_and_ordered(self):
        new = D.splice_fallbacks(CONFIG, {"flash": ["heavy", "flash-or"]})
        line = [l for l in new.splitlines() if l.lstrip().startswith("fallbacks:")][0]
        parsed = json.loads(line.split("fallbacks:", 1)[1].strip())
        merged = {}
        for e in parsed:
            merged.update(e)
        self.assertEqual(merged["flash"], ["heavy", "flash-or"])

    def test_indentation_of_the_anchor_is_kept(self):
        new = D.splice_fallbacks(CONFIG, {"heavy": ["heavy-glm"]})
        line = [l for l in new.splitlines() if "fallbacks:" in l][0]
        self.assertTrue(line.startswith("  fallbacks:"), repr(line))

    def test_missing_anchor_raises_rather_than_appending(self):
        """A config with no fallbacks line is not one we know how to edit. Adding
        one at a guessed position would put it outside router_settings."""
        with self.assertRaises(D.SpliceError):
            D.splice_fallbacks("model_list:\n  - model_name: a\n", {"a": ["b"]})

    def test_two_fallbacks_lines_raise(self):
        doubled = CONFIG + '\n  fallbacks: [{"x": ["y"]}]\n'
        with self.assertRaises(D.SpliceError):
            D.splice_fallbacks(doubled, {"heavy": ["heavy-glm"]})


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.topo = D.parse_topology_text(CONFIG)

    def test_accepts_a_sane_rewire(self):
        self.assertEqual(D.validate_chains(self.topo, {"flash": ["flash-or", "heavy"]}), [])

    def test_rejects_a_hop_that_is_not_a_deployment(self):
        errs = D.validate_chains(self.topo, {"flash": ["nope"]})
        self.assertTrue(any("nope" in e for e in errs))

    def test_rejects_a_lane_that_is_not_a_deployment(self):
        errs = D.validate_chains(self.topo, {"ghost": ["flash"]})
        self.assertTrue(any("ghost" in e for e in errs))

    def test_rejects_a_lane_falling_back_to_itself(self):
        errs = D.validate_chains(self.topo, {"flash": ["flash"]})
        self.assertTrue(errs)

    def test_rejects_a_duplicated_hop(self):
        errs = D.validate_chains(self.topo, {"flash": ["heavy", "heavy"]})
        self.assertTrue(errs)

    def test_empty_chain_is_allowed(self):
        """Removing every hop is a legitimate choice: it means hard-fail rather
        than spill, which is what the local GPU lanes deliberately do."""
        self.assertEqual(D.validate_chains(self.topo, {"flash": []}), [])


class TopologyTest(unittest.TestCase):
    def test_pool_members_are_counted(self):
        pooled = CONFIG.replace(
            "  - model_name: flash-or\n", "  - model_name: flash\n", 1)
        topo = D.parse_topology_text(pooled)
        self.assertEqual(topo["groups"]["flash"]["count"], 2,
                         "a pool is deployments SHARING a model_name")

    def test_chain_and_pool_are_distinguishable(self):
        topo = D.parse_topology_text(CONFIG)
        self.assertEqual(topo["groups"]["flash"]["count"], 1)
        self.assertIn("flash", topo["fallbacks"])


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "litellm.yaml")
        with open(self.path, "w") as f:
            f.write(CONFIG)

    def test_snapshot_copies_the_original_verbatim(self):
        snap = D.snapshot_config(self.path)
        self.assertTrue(os.path.exists(snap))
        with open(snap) as f:
            self.assertEqual(f.read(), CONFIG)

    def test_snapshot_name_is_timestamped_and_distinct(self):
        a = D.snapshot_config(self.path)
        b = D.snapshot_config(self.path)
        self.assertNotEqual(a, b, "two snapshots in one second must not collide")

    def test_apply_snapshots_before_writing(self):
        snap, _ = D.apply_chains(self.path, {"flash": ["heavy"]})
        with open(snap) as f:
            self.assertEqual(f.read(), CONFIG, "snapshot is the PRE-write file")
        with open(self.path) as f:
            self.assertIn('"flash": ["heavy"]', f.read())

    def test_apply_refuses_an_invalid_chain_and_writes_nothing(self):
        with self.assertRaises(D.SpliceError):
            D.apply_chains(self.path, {"flash": ["nope"]})
        with open(self.path) as f:
            self.assertEqual(f.read(), CONFIG, "a rejected apply must not write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
