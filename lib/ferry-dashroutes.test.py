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

    # ── the second chain map: context_window_fallbacks ────────────────────
    CW_CONFIG = CONFIG.replace(
        "  fallbacks:",
        '  context_window_fallbacks: [{"heavy": ["heavy-glm"]}]\n'
        '  fallbacks:', 1)

    def test_a_reorder_syncs_the_context_window_map(self):
        """The cw map carries the overflow chain; leaving it on the old order
        while fallbacks moves would send a context overflow to the hop the
        reorder just demoted. Same one-line splice, same merge rule."""
        new = D.splice_fallbacks(self.CW_CONFIG, {"heavy": ["flash"]})
        cw = [l for l in new.splitlines()
              if l.lstrip().startswith("context_window_fallbacks:")][0]
        merged = {}
        for e in json.loads(cw.split("context_window_fallbacks:", 1)[1].strip()):
            merged.update(e)
        self.assertEqual(merged["heavy"], ["flash"])

    def test_the_cw_map_gains_no_entry_for_a_lane_it_never_had(self):
        new = D.splice_fallbacks(self.CW_CONFIG, {"flash": ["heavy"]})
        cw = [l for l in new.splitlines()
              if l.lstrip().startswith("context_window_fallbacks:")][0]
        merged = {}
        for e in json.loads(cw.split("context_window_fallbacks:", 1)[1].strip()):
            merged.update(e)
        self.assertNotIn("flash", merged)

    def test_the_fallbacks_anchor_does_not_match_the_cw_line(self):
        new = D.splice_fallbacks(self.CW_CONFIG, {"heavy": ["flash"]})
        lines = [l for l in new.splitlines()
                 if l.lstrip().startswith("context_window_fallbacks:")]
        self.assertEqual(len(lines), 1, "the cw line must survive as-is")


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


class HotswapNoteTest(unittest.TestCase):
    """hotswap_reorder: the live half of an apply never fails the file half.

    The file write already succeeded when this runs, so every outcome is a
    NOTE, never an exception: success, unreachable proxy, a proxy predating
    the endpoint, and a live router refusing what the file accepted (the one
    that must read as a warning, not a success — the next restart would serve
    the refused order). http_json is stubbed; no proxy is touched."""

    def _with(self, stub):
        prev = D.http_json
        D.http_json = stub
        self.addCleanup(setattr, D, "http_json", prev)

    def test_success_reports_no_restart_needed(self):
        self._with(lambda *a, **k: {"ok": True, "status": 200,
                                    "json": {"ok": True}})
        note = D.hotswap_reorder("http://x", "k",
                                 {"heavy": ["heavy", "flash-or"]})
        self.assertIn("no restart needed", note)

    def test_an_unreachable_proxy_names_the_restart_fallback(self):
        def boom(*a, **k):
            raise ConnectionError("refused")
        self._with(boom)
        note = D.hotswap_reorder("http://x", "k",
                                 {"heavy": ["heavy", "flash-or"]})
        self.assertIn("restart it", note)

    def test_a_proxy_predating_the_endpoint_names_the_restart_fallback(self):
        self._with(lambda *a, **k: {"ok": False, "status": 404,
                                    "json": {"errors": ["not found"]}})
        note = D.hotswap_reorder("http://x", "k",
                                 {"heavy": ["heavy", "flash-or"]})
        self.assertIn("restart the proxy", note)

    def test_a_live_refusal_reads_as_a_warning_not_a_success(self):
        self._with(lambda *a, **k: {"ok": False, "status": 409,
                                    "json": {"errors": ["hop 'ghost' "
                                                        "not served"]}})
        note = D.hotswap_reorder("http://x", "k",
                                 {"heavy": ["heavy", "ghost"]})
        self.assertIn("REFUSED", note)
        self.assertIn("ghost", note)

    def test_the_legacy_shape_sends_chains_not_order(self):
        seen = {}

        def cap(url, key, method, body, timeout=None):
            seen.update(body)
            return {"ok": True, "status": 200, "json": {"ok": True}}
        self._with(cap)
        D.hotswap_reorder("http://x", "k",
                          {"__chains__": {"heavy": ["flash-or"]}})
        # A bare chains map has no position 0: sending it as `order` would
        # eat its first hop as a primary. It must go out as `chains`.
        self.assertEqual(seen, {"chains": {"heavy": ["flash-or"]}})

    def test_the_unified_shape_sends_order_verbatim(self):
        seen = {}

        def cap(url, key, method, body, timeout=None):
            seen.update(body)
            return {"ok": True, "status": 200, "json": {"ok": True}}
        self._with(cap)
        D.hotswap_reorder("http://x", "k",
                          {"heavy": ["heavy", "flash-or"]})
        self.assertEqual(seen, {"order": {"heavy": ["heavy", "flash-or"]}})


SWAP_CONFIG = """\
# A comment that must not move.
model_list:
  - model_name: lane-a
    litellm_params:
      model: provider-a/model-a
      api_key: os.environ/KEY_A
    model_info:
      public: true
      id: id-a

  # A comment owned by lane-b.
  - model_name: lane-b
    litellm_params:
      model: provider-b/model-b
      api_key: os.environ/KEY_B
      api_base: https://b.example/v1
    model_info:
      id: id-b

router_settings:
  fallbacks: [{"lane-a": ["lane-b"]}]
"""


# The 2026-09-04 outage shape: the HOP's block sits ABOVE the lane's and its
# litellm_params body is one line longer (api_base). A third block follows the
# lane so an off-by-one splice has a neighbour to damage.
SWAP_CONFIG_HOP_FIRST = """\
model_list:
  # A comment owned by lane-b.
  - model_name: lane-b
    litellm_params:
      model: provider-b/model-b
      api_key: os.environ/KEY_B
      api_base: https://b.example/v1
    model_info:
      id: id-b

  # A comment owned by lane-a.
  - model_name: lane-a
    litellm_params:
      model: provider-a/model-a
      api_key: os.environ/KEY_A
    model_info:
      public: true
      id: id-a

  # A comment owned by lane-c.
  - model_name: lane-c
    litellm_params:
      model: provider-c/model-c
    model_info:
      id: id-c

router_settings:
  fallbacks: [{"lane-a": ["lane-b"]}]
"""


class SwapPrimariesTest(unittest.TestCase):
    """swap_primaries: the file half of a promote.

    Byte discipline is the property: only the litellm_params bodies trade
    places (exact bytes) and the two id: values change. Comments, key order,
    and every other byte stay put — the diff must show the moved lines as
    moved, never a rewrite."""

    def test_backends_trade_places_ids_take_the_supplied_fresh_pair(self):
        new = D.swap_primaries(SWAP_CONFIG, "lane-a", "lane-b",
                               "lane-a-promoted-T", "lane-b-promoted-T")
        _, blocks = D._deploy_blocks(new.splitlines())
        for name, want_model, want_id in (
                ("lane-a", "provider-b/model-b", "lane-a-promoted-T"),
                ("lane-b", "provider-a/model-a", "lane-b-promoted-T")):
            s, e = blocks[name]
            blk = "\n".join(new.splitlines()[s:e])
            self.assertIn("model: " + want_model, blk)
            self.assertIn("id: " + want_id, blk)

    def test_names_public_and_chains_are_untouched(self):
        new = D.swap_primaries(SWAP_CONFIG, "lane-a", "lane-b", "x", "y")
        self.assertIn("- model_name: lane-a", new)
        self.assertIn("- model_name: lane-b", new)
        self.assertIn("public: true", new)
        self.assertIn('fallbacks: [{"lane-a": ["lane-b"]}]', new)

    def test_comments_survive_byte_identical(self):
        def comments(t):
            return [l for l in t.splitlines() if l.lstrip().startswith("#")]
        new = D.swap_primaries(SWAP_CONFIG, "lane-a", "lane-b", "x", "y")
        self.assertEqual(comments(SWAP_CONFIG), comments(new))

    def test_only_backend_lines_and_ids_change(self):
        new = D.swap_primaries(SWAP_CONFIG, "lane-a", "lane-b", "x", "y")
        a, b = SWAP_CONFIG.splitlines(), new.splitlines()
        # The bodies differ in length by one (api_base), so the file grows by
        # one line: every UNCHANGED line keeps its bytes, and every line the
        # diff touches is a moved backend line, an id, or a block-boundary
        # line whose address shifted by the length delta.
        import difflib
        ops = [op for op in difflib.SequenceMatcher(None, a, b).get_opcodes()
               if op[0] != "equal"]
        touched = set()
        for tag, i1, i2, j1, j2 in ops:
            touched.update(a[i1:i2])
            touched.update(b[j1:j2])
        for l in touched:
            s = l.strip()
            self.assertTrue(
                not s or s.startswith("#") or s.startswith("- model_name:")
                or s.split(":")[0] in (
                    "model", "api_key", "api_base", "timeout",
                    "reasoning_effort", "extra_body", "provider", "sort",
                    "litellm_params",
                    "model_info", "public", "id", "max_input_tokens")
                or s.startswith(("model:", "api_key:", "api_base:", "id:")),
                l)

    def test_hop_block_above_lane_block_with_unequal_bodies(self):
        """Regression for the 2026-09-04 front-door outage. Both spans were
        computed on the original text, then spliced into ONE list in dict
        order (hop first). The hop's body was a line shorter after the swap,
        so every later line shifted up by one and the lane's splice landed
        one line off: it ate the blank line above the lane's anchor and left
        the lane's last comment duplicated. The swap BACK then doubled the
        lane's anchor and overwrote the NEXT block's, litellm raised
        KeyError('litellm_params') on startup, and the proxy stayed down."""
        new = D.swap_primaries(SWAP_CONFIG_HOP_FIRST, "lane-a", "lane-b",
                               "lane-a-promoted-T", "lane-b-promoted-T")
        lines = new.splitlines()
        _, blocks = D._deploy_blocks(lines)          # duplicate anchors raise
        self.assertEqual(sorted(blocks), ["lane-a", "lane-b", "lane-c"])
        for name in ("lane-a", "lane-b", "lane-c"):
            self.assertEqual(new.count("- model_name: %s" % name), 1, name)
        for name, want_model, want_id in (
                ("lane-a", "provider-b/model-b", "lane-a-promoted-T"),
                ("lane-b", "provider-a/model-a", "lane-b-promoted-T"),
                ("lane-c", "provider-c/model-c", "id-c")):
            s, e = blocks[name]
            blk = "\n".join(lines[s:e])
            self.assertIn("model: " + want_model, blk)
            self.assertIn("id: " + want_id, blk)
        self.assertIn("api_base: https://b.example/v1",
                      "\n".join(lines[slice(*blocks["lane-a"])]))
        self.assertNotIn("api_base",
                         "\n".join(lines[slice(*blocks["lane-b"])]))

    def test_comments_survive_when_the_hop_block_is_above(self):
        def comments(t):
            return [l for l in t.splitlines() if l.lstrip().startswith("#")]
        new = D.swap_primaries(SWAP_CONFIG_HOP_FIRST, "lane-a", "lane-b",
                               "x", "y")
        self.assertEqual(comments(SWAP_CONFIG_HOP_FIRST), comments(new))

    def test_swapping_back_restores_the_original_bytes(self):
        """The outage's exact sequence: promote, then promote back with the
        original ids. The file must come back byte-identical."""
        once = D.swap_primaries(SWAP_CONFIG_HOP_FIRST, "lane-a", "lane-b",
                                "lane-a-promoted-T", "lane-b-promoted-T")
        back = D.swap_primaries(once, "lane-a", "lane-b", "id-a", "id-b")
        self.assertEqual(back, SWAP_CONFIG_HOP_FIRST)

    def test_missing_block_raises_and_writes_nothing(self):
        with self.assertRaises(D.SpliceError):
            D.swap_primaries(SWAP_CONFIG, "lane-a", "ghost", "x", "y")

    def test_duplicate_anchors_raise(self):
        doubled = SWAP_CONFIG.replace("- model_name: lane-b",
                                      "- model_name: lane-a", 1)
        with self.assertRaises(D.SpliceError):
            D.swap_primaries(doubled, "lane-a", "lane-b", "x", "y")

    def test_a_block_without_params_raises(self):
        stripped = SWAP_CONFIG.replace("    litellm_params:\n", "", 1)
        with self.assertRaises(D.SpliceError):
            D.swap_primaries(stripped, "lane-a", "lane-b", "x", "y")


class PromoteFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "litellm.yaml")
        with open(self.path, "w") as f:
            f.write(SWAP_CONFIG)
        self.topo = D.parse_topology_text(SWAP_CONFIG)

    def test_validate_accepts_a_chain_hop(self):
        self.assertEqual(D.validate_promote_file(
            self.topo, "lane-a", "lane-b"), [])

    def test_validate_refuses_a_hop_outside_the_chain(self):
        errs = D.validate_promote_file(self.topo, "lane-b", "lane-a")
        self.assertTrue(any("not in" in e for e in errs), errs)

    def test_validate_refuses_unknown_names(self):
        self.assertTrue(D.validate_promote_file(self.topo, "ghost", "lane-b"))
        self.assertTrue(D.validate_promote_file(self.topo, "lane-a", "ghost"))

    def test_apply_swaps_and_snapshots(self):
        snap, diff = D.apply_promote(self.path, "lane-a", "lane-b",
                                     "lane-a-promoted-T", "lane-b-promoted-T")
        with open(snap) as f:
            self.assertEqual(f.read(), SWAP_CONFIG)
        with open(self.path) as f:
            written = f.read()
        self.assertIn("id: lane-a-promoted-T", written)
        self.assertIn("id: lane-b-promoted-T", written)
        self.assertTrue(diff)

    def test_apply_refuses_and_writes_nothing(self):
        with self.assertRaises(D.SpliceError):
            D.apply_promote(self.path, "lane-b", "lane-a", "x", "y")
        with open(self.path) as f:
            self.assertEqual(f.read(), SWAP_CONFIG)

    def test_minted_ids_are_distinct_and_prefixed(self):
        a, b = D._mint_file_ids("lane-a", "lane-b")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("lane-a-promoted-"))
        self.assertTrue(b.startswith("lane-b-promoted-"))


class PromoteHotswapTest(unittest.TestCase):
    """hotswap_promote: the live half of a promote apply.

    Preview posts read-only and returns the verdict dict; apply posts the
    file-first ids so file and router agree; every transport failure is a
    note, never an exception."""

    def _with(self, stub):
        prev = D.http_json
        D.http_json = stub
        self.addCleanup(setattr, D, "http_json", prev)

    def test_preview_returns_the_verdict_dict(self):
        verdict = {"ok": True, "lane": "heavy", "hop": "x"}
        seen = {}

        def cap(url, key, method, body, timeout=None):
            seen["url"] = url
            seen["body"] = body
            return {"ok": True, "status": 200, "json": verdict}
        self._with(cap)
        out = D.hotswap_promote("http://x", "k", "heavy", "x", preview_only=True)
        self.assertEqual(out, verdict)
        self.assertTrue(seen["url"].endswith("/v1/ferry/promote/preview"))
        # Preview sends no ids — there is nothing to echo yet.
        self.assertEqual(seen["body"], {"lane": "heavy", "hop": "x"})

    def test_apply_sends_the_file_first_ids(self):
        seen = {}

        def cap(url, key, method, body, timeout=None):
            seen["body"] = body
            return {"ok": True, "status": 200, "json": {"ok": True}}
        self._with(cap)
        note = D.hotswap_promote("http://x", "k", "heavy", "x",
                                 lane_id="L", hop_id="H")
        self.assertIn("no restart needed", note)
        self.assertEqual(seen["body"],
                         {"lane": "heavy", "hop": "x",
                          "lane_id": "L", "hop_id": "H"})

    def test_a_live_refusal_reads_as_a_warning(self):
        self._with(lambda *a, **k: {"ok": False, "status": 409,
                                    "json": {"errors": ["not in chain"]}})
        note = D.hotswap_promote("http://x", "k", "heavy", "x",
                                 lane_id="L", hop_id="H")
        self.assertIn("REFUSED", note)

    def test_an_unreachable_proxy_names_the_restart_fallback(self):
        def boom(*a, **k):
            raise ConnectionError("refused")
        self._with(boom)
        note = D.hotswap_promote("http://x", "k", "heavy", "x",
                                 lane_id="L", hop_id="H")
        self.assertIn("restart it", note)


class ProbeBackendsTest(unittest.TestCase):
    """probe_backends: drives the "Test backends" button. Rewritten
    2026-09-04 to derive the probe list from the config topology (every lane
    AND every hop — a hop the operator never sees fail until it fires) rather
    than a hardcoded 4-tuple, and to stream every call, since the ChatGPT-
    subscription driver 500s on a non-streamed request even while healthy.
    http_stream/http_json are stubbed; no proxy is touched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "litellm.yaml")
        with open(self.path, "w") as f:
            f.write(CONFIG)
        prev_cfg = dict(D.CFG)

        def restore():
            D.CFG.clear()
            D.CFG.update(prev_cfg)
        self.addCleanup(restore)
        D.CFG.update(ferry="http://x", key="k", config_path=self.path)

    def _with_stream(self, stub):
        prev = D.http_stream
        D.http_stream = stub
        self.addCleanup(setattr, D, "http_stream", prev)

    def _with_json(self, stub):
        prev = D.http_json
        D.http_json = stub
        self.addCleanup(setattr, D, "http_json", prev)

    def test_probes_every_name_in_topology_order_lanes_and_hops(self):
        seen = []

        def stub(url, key, method, body, timeout=None):
            seen.append(body["model"])
            return {"ok": True, "status": 200, "ms": 12, "headers": {}}
        self._with_stream(stub)
        D.probe_backends()
        order = D.parse_topology_text(CONFIG)["order"]
        self.assertEqual(seen, order)
        self.assertIn("heavy-glm", seen, "a fallback hop must be probed too")
        self.assertIn("flash-or", seen, "a fallback hop must be probed too")

    def test_every_probe_body_streams_with_a_bigger_budget(self):
        bodies = []

        def stub(url, key, method, body, timeout=None):
            bodies.append(body)
            return {"ok": True, "status": 200, "ms": 5, "headers": {}}
        self._with_stream(stub)
        D.probe_backends()
        self.assertTrue(bodies, "no requests were captured")
        for b in bodies:
            self.assertIs(b.get("stream"), True)
            self.assertEqual(b.get("max_tokens"), 16)

    def test_results_are_keyed_by_lane_name(self):
        self._with_stream(lambda url, key, method, body, timeout=None:
                          {"ok": True, "status": 200, "ms": 7, "headers": {}})
        out = D.probe_backends()
        self.assertEqual(set(out.keys()),
                         set(D.parse_topology_text(CONFIG)["order"]))

    def test_served_by_model_id_and_fallbacks_come_from_headers(self):
        def stub(url, key, method, body, timeout=None):
            return {"ok": True, "status": 200, "ms": 9,
                   "headers": {"x-litellm-model-name": "openrouter/openai/gpt-5.6-luna",
                               "x-litellm-model-id": "or-luna-flash-fb",
                               "x-litellm-attempted-fallbacks": "1"}}
        self._with_stream(stub)
        out = D.probe_backends()
        row = out["flash"]
        self.assertEqual(row["served_by"], "openrouter/openai/gpt-5.6-luna")
        self.assertEqual(row["model_id"], "or-luna-flash-fb")
        self.assertEqual(row["fallbacks"], "1")

    def test_requests_hit_the_chat_completions_endpoint(self):
        urls = []

        def stub(url, key, method, body, timeout=None):
            urls.append(url)
            return {"ok": True, "status": 200, "ms": 1, "headers": {}}
        self._with_stream(stub)
        D.probe_backends()
        for u in urls:
            self.assertTrue(u.endswith("/v1/chat/completions"), u)

    def test_a_non_2xx_keeps_the_response_body_as_the_error(self):
        def stub(url, key, method, body, timeout=None):
            return {"ok": False, "status": 500, "ms": 3, "headers": {},
                   "error": "Unknown items in responses API response: []"}
        self._with_stream(stub)
        out = D.probe_backends()
        self.assertFalse(out["heavy"]["ok"])
        self.assertIn("Unknown items", out["heavy"]["error"])

    def test_an_unreadable_config_falls_back_to_v1_models(self):
        D.CFG["config_path"] = os.path.join(self.tmp, "does-not-exist.yaml")

        def json_stub(url, key, method="GET", body=None, timeout=None):
            self.assertTrue(url.endswith("/v1/models"))
            return {"ok": True, "status": 200,
                   "json": {"data": [{"id": "heavy"}, {"id": "flash"}]}}
        self._with_json(json_stub)
        seen = []

        def stream_stub(url, key, method, body, timeout=None):
            seen.append(body["model"])
            return {"ok": True, "status": 200, "ms": 1, "headers": {}}
        self._with_stream(stream_stub)
        out = D.probe_backends()
        self.assertEqual(set(out.keys()), {"heavy", "flash"})
        self.assertEqual(sorted(seen), ["flash", "heavy"])


class TapClassifierTest(unittest.TestCase):
    """Activity.poll(): the tap's last_event, now sourced from the shared
    classifier in lib/ferry_live.py (classify_log_line) instead of the old
    Kimi-specific "permission_error" + "usage limit" string match."""

    RULES = {"rules": [
        {"state": "quota_exhausted", "status": [403],
         "message_contains": ["usage limit"]},
    ], "ttl": {}}

    # litellm's real cooldown line shape (mirrors
    # lib/ferry-live.test.py's LogLineTapTests.COOLDOWN) — the status sits
    # in a python-repr dict, not a bare HTTP status line.
    COOLDOWN = ("Cooldown Deployments=[('dep-1', {'exception_received': "
               "'litellm.PermissionDeniedError: usage limit reached', "
               "'status_code': '403', 'cooldown_time': 5})]")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "access.log")
        with open(self.path, "w"):
            pass   # start empty; Activity begins reading at EOF

    def _tail(self, rules):
        return D.Activity(self.path, rules)

    def _append(self, line):
        with open(self.path, "a") as f:
            f.write(line + "\n")

    def test_a_line_that_classifies_quota_exhausted_sets_the_event(self):
        a = self._tail(self.RULES)
        self._append(self.COOLDOWN)
        a.poll()
        self.assertIsNotNone(a.last_event)
        self.assertEqual(a.last_event["kind"], "quota_exhausted")

    def test_a_ratelimiterror_line_with_no_rules_sets_rate_limited(self):
        a = self._tail({"rules": [], "ttl": {}})
        self._append("litellm.RateLimitError: rate limited, try again")
        a.poll()
        self.assertIsNotNone(a.last_event)
        self.assertEqual(a.last_event["kind"], "rate_limited")

    def test_a_plain_200_access_line_sets_no_event(self):
        a = self._tail({"rules": [], "ttl": {}})
        self._append('10.0.0.2:5555 - "POST /v1/chat/completions HTTP/1.1" 200 OK')
        a.poll()
        self.assertIsNone(a.last_event)
        self.assertEqual(a.total, 1, "the request is still counted")

    def test_default_rules_is_the_empty_table_not_a_crash(self):
        a = D.Activity(self.path)
        self.assertEqual(a.rules, {"rules": [], "ttl": {}})

    def test_event_text_has_no_vendor_name_or_stale_403_suffix(self):
        a = self._tail(self.RULES)
        self._append(self.COOLDOWN)
        a.poll()
        self.assertNotIn("Kimi", a.last_event["text"])
        self.assertNotIn("(403)", a.last_event["text"])

    def test_the_inline_floor_still_classifies_when_ferry_live_is_unavailable(self):
        """_live() unavailable (the sibling module missing/broken): the tap
        must still classify through its own vendor-neutral floor rather than
        going dark, same as ferry_live.classify_log_line's own floor."""
        prev_live = D._live
        D._live = lambda: None
        self.addCleanup(setattr, D, "_live", prev_live)
        a = self._tail({"rules": [], "ttl": {}})
        self._append("litellm.RateLimitError: x")
        a.poll()
        self.assertEqual(a.last_event["kind"], "rate_limited")
        self._append("{'error': {'code': 'insufficient_quota'}}")
        a.poll()
        self.assertEqual(a.last_event["kind"], "quota_exhausted")


FLEET_CONFIG = """\
# ---------------------------------------------------------------------------
# THE FERRY STACK — fleets fixture. This block must survive every write.
# ---------------------------------------------------------------------------

model_list:
  # domestic fleet
  - model_name: domestic.flash
    litellm_params:
      model: openrouter/~google/gemini-flash-latest
    model_info:
      public: true
      id: d-flash-1

  - model_name: domestic.flash-luna
    litellm_params:
      model: openrouter/openai/gpt-5.6-luna
    model_info:
      id: d-flash-luna-1

  # international fleet
  - model_name: international.flash
    litellm_params:
      model: zai/glm-5.3-flash
    model_info:
      public: true
      id: i-flash-1

  - model_name: international.flash-or
    litellm_params:
      model: openrouter/~z-ai/glm-flash-latest
    model_info:
      id: i-flash-or-1

  # shared GPU lane, no fleet prefix
  - model_name: local-orch
    litellm_params:
      model: openai/local-orch
    model_info:
      public: true
      id: local-orch-1

router_settings:
  # A comment directly above the anchor line, which must not move.
  fallbacks: [{"domestic.flash": ["domestic.flash-luna"]}, {"international.flash": ["international.flash-or"]}]
  # And one directly below it.
  cooldown_time: 5
"""


class FleetTopologyTest(unittest.TestCase):
    """Grouping lane names by fleet prefix, and the cross-fleet hop refusal.

    FLEET_CONFIG mirrors the layout in the fleets design spec section 3: two
    prefixed fleets (domestic, international) plus one unprefixed shared GPU
    lane. validate_order is exercised too, but only to confirm it inherits the
    rule through validate_chains rather than needing a second copy of it."""

    def setUp(self):
        self.topo = D.parse_topology_text(FLEET_CONFIG)

    def test_fleets_and_shared_grouping(self):
        self.assertEqual(self.topo["fleets"], {
            "domestic": ["domestic.flash", "domestic.flash-luna"],
            "international": ["international.flash", "international.flash-or"],
        })
        self.assertEqual(self.topo["shared"], ["local-orch"])

    def test_cross_fleet_hop_rejected(self):
        errs = D.validate_chains(
            self.topo, {"domestic.flash": ["international.flash-or"]})
        self.assertEqual(
            errs, ["hop 'international.flash-or' is not in fleet 'domestic'"])

    def test_same_fleet_hop_allowed(self):
        self.assertEqual(
            D.validate_chains(
                self.topo, {"domestic.flash": ["domestic.flash-luna"]}),
            [])

    def test_validate_order_inherits_the_cross_fleet_rule(self):
        errs = D.validate_order(self.topo, {
            "domestic.flash": ["domestic.flash", "international.flash-or"],
        })
        self.assertIn(
            "hop 'international.flash-or' is not in fleet 'domestic'", errs)

    def test_same_fleet_reorder_preserves_comments(self):
        """Control: proves the byte-preserving splice still holds for dotted
        fleet lane names, same style as SpliceTest.test_comments_survive."""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "litellm.yaml")
        with open(path, "w") as f:
            f.write(FLEET_CONFIG)
        D.apply_chains(path, {
            "domestic.flash": ["domestic.flash-luna"],
            "international.flash": ["international.flash-or"],
        })
        with open(path) as f:
            new = f.read()
        self.assertEqual(comments(FLEET_CONFIG), comments(new))

    def test_a_dotted_non_fleet_lane_is_not_a_fleet(self):
        # gpt-3.5-turbo has a dot and is not one of this topology's declared
        # fleets (domestic, international); it must not be treated as a fleet
        # of its own, mirroring the guard resolve_model applies
        # (front/ferry_front.py: "." in model and model.split(".", 1)[0] in
        # fleets). The lane is not itself declared here, so `validate_chains`
        # may still complain it is not a model_name — only the "not in fleet"
        # message is what must never appear.
        errs = D.validate_chains(
            self.topo, {"gpt-3.5-turbo": ["domestic.flash-luna"]})
        self.assertFalse(any("not in fleet" in e for e in errs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
