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
deletes all of it. So the writer never round-trips: it rewrites ONE anchored
line and passes every other byte through.

test_comments_survive is therefore the point of this file, and it ships with a
control (test_the_comment_check_can_fail) because a preservation assertion that
cannot fail proves nothing — which is exactly how a check like this rots.

The OrderTest class covers the UNIFIED order shape on top: each lane sent as
[primary, ...fallbacks] in one list. Position 0 is validated against the
lane's current primary (itself) and never written; positions 1..n go through
the same splice_fallbacks anchor as the legacy chains API.
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


class OrderTest(unittest.TestCase):
    """The unified [primary, ...fallbacks] shape: validate / diff / apply.

    Position 0 is a GUARD, not a write: it must equal the lane's current
    primary (the lane's own model_name), and the writer refuses anything else
    with the stable `primary changes not yet supported` prefix. Everything the
    guard admits is expressed through the fallbacks map, so an admitted order
    touches exactly the same one anchored line the legacy API touches."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "litellm.yaml")
        with open(self.path, "w") as f:
            f.write(CONFIG)
        self.topo = D.parse_topology_text(CONFIG)

    # ── order_to_chains ───────────────────────────────────────────────────
    def test_order_to_chains_drops_position_zero(self):
        self.assertEqual(
            D.order_to_chains({"flash": ["flash", "heavy", "flash-or"]}),
            {"flash": ["heavy", "flash-or"]})

    def test_order_to_chains_of_primary_only_is_an_empty_chain(self):
        """[lane] alone means hard-fail: the tail is empty, not absent."""
        self.assertEqual(D.order_to_chains({"flash": ["flash"]}), {"flash": []})

    # ── validate_order: the admitted shape ────────────────────────────────
    def test_accepts_a_sane_order(self):
        errs = D.validate_order(self.topo, {
            "flash": ["flash", "flash-or", "heavy"],
            "heavy": ["heavy"],
        })
        self.assertEqual(errs, [])

    # ── validate_order: the refused shapes ────────────────────────────────
    def test_rejects_promoting_a_fallback_to_primary(self):
        errs = D.validate_order(self.topo, {"flash": ["heavy", "flash-or"]})
        self.assertTrue(any("primary changes not yet supported" in e for e in errs),
                        errs)

    def test_primary_error_is_a_list_position_not_a_membership_error(self):
        """'heavy' IS a real deployment, so the only thing wrong with it at
        position 0 is that it is not the lane's current primary."""
        errs = D.validate_order(self.topo, {"flash": ["heavy"]})
        self.assertEqual([e for e in errs if "nope" in e], [])
        self.assertTrue(any("primary changes not yet supported" in e for e in errs))

    def test_rejects_an_empty_order(self):
        errs = D.validate_order(self.topo, {"flash": []})
        self.assertTrue(any("may not be empty" in e for e in errs), errs)

    def test_rejects_a_non_list_order(self):
        errs = D.validate_order(self.topo, {"flash": "flash"})
        self.assertTrue(any("must be a list" in e for e in errs), errs)

    def test_rejects_a_lane_that_is_not_a_deployment(self):
        errs = D.validate_order(self.topo, {"ghost": ["ghost", "flash"]})
        self.assertTrue(any("ghost" in e for e in errs))

    def test_rejects_a_hop_that_is_not_a_deployment(self):
        errs = D.validate_order(self.topo, {"flash": ["flash", "nope"]})
        self.assertTrue(any("nope" in e for e in errs))

    def test_rejects_a_duplicate_in_the_tail(self):
        errs = D.validate_order(self.topo, {"flash": ["flash", "heavy", "heavy"]})
        self.assertTrue(any("twice" in e for e in errs), errs)

    def test_rejects_the_primary_repeated_as_a_fallback(self):
        errs = D.validate_order(self.topo, {"flash": ["flash", "flash", "heavy"]})
        self.assertTrue(any("twice" in e for e in errs), errs)

    def test_rejects_a_lane_falling_back_to_itself(self):
        errs = D.validate_order(self.topo, {"flash": ["flash", "flash"]})
        self.assertTrue(any("own fallback" in e for e in errs), errs)

    # ── diff_order ────────────────────────────────────────────────────────
    def test_diff_of_a_reorder_changes_only_the_fallbacks_line(self):
        diff, errs = D.diff_order(
            self.path, {"flash": ["flash", "heavy", "flash-or"]})
        self.assertEqual(errs, [])
        changed = [l for l in diff.splitlines()
                   if l.startswith(("+", "-")) and "fallbacks:" in l]
        self.assertEqual(len(changed), 2, diff)

    def test_diff_of_a_primary_change_is_empty_with_errors(self):
        diff, errs = D.diff_order(self.path, {"flash": ["heavy", "flash-or"]})
        self.assertEqual(diff, "")
        self.assertTrue(any("primary changes not yet supported" in e for e in errs))

    def test_comments_survive_an_order_diff(self):
        new = D.splice_fallbacks(
            CONFIG, D.order_to_chains({"flash": ["flash", "heavy", "flash-or"]}))
        self.assertEqual(comments(CONFIG), comments(new))

    # ── apply_order ───────────────────────────────────────────────────────
    def test_apply_writes_the_tail_as_the_chain(self):
        snap, _ = D.apply_order(self.path, {"flash": ["flash", "heavy", "flash-or"]})
        self.assertTrue(os.path.exists(snap))
        with open(self.path) as f:
            written = f.read()
        line = [l for l in written.splitlines()
                if l.lstrip().startswith("fallbacks:")][0]
        merged = {}
        for e in json.loads(line.split("fallbacks:", 1)[1].strip()):
            merged.update(e)
        self.assertEqual(merged["flash"], ["heavy", "flash-or"])

    def test_apply_of_primary_only_leaves_a_hard_fail_lane(self):
        """[lane] alone strips the chain: litellm then hard-fails the lane
        instead of spilling — the local GPU lanes' deliberate posture."""
        D.apply_order(self.path, {"flash": ["flash"]})
        with open(self.path) as f:
            written = f.read()
        self.assertIn('{"flash": []}', written)

    def test_apply_refuses_a_primary_change_and_writes_nothing(self):
        with self.assertRaises(D.SpliceError) as ctx:
            D.apply_order(self.path, {"flash": ["heavy", "flash-or"]})
        self.assertIn("primary changes not yet supported", str(ctx.exception))
        with open(self.path) as f:
            self.assertEqual(f.read(), CONFIG, "a rejected apply must not write")

    def test_apply_snapshots_the_pre_write_file(self):
        snap, _ = D.apply_order(self.path, {"heavy": ["heavy"]})
        with open(snap) as f:
            self.assertEqual(f.read(), CONFIG)

    def test_apply_of_a_multi_lane_order_keeps_other_lanes(self):
        """Editing two lanes must not silently drop the third's chain — the
        merge rule splice_fallbacks already guarantees for the legacy shape."""
        D.apply_order(self.path, {"flash": ["flash", "heavy"],
                                  "heavy": ["heavy", "flash-or"]})
        with open(self.path) as f:
            written = f.read()
        line = [l for l in written.splitlines()
                if l.lstrip().startswith("fallbacks:")][0]
        merged = {}
        for e in json.loads(line.split("fallbacks:", 1)[1].strip()):
            merged.update(e)
        self.assertEqual(merged, {"heavy": ["flash-or"], "flash": ["heavy"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
