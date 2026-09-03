#!/usr/bin/env python3
"""Stdlib unittest for ferry-metrics-exporter.

Run:  python3 observ/ferry-metrics-exporter.test.py

The exporter file has no .py extension, so we load it via importlib. Tests use
a temp proxy-log fixture + inline litellm.yaml and NEVER touch the network for
inference — litellm calls are pointed at a guaranteed-closed localhost port, so
they degrade to ferry_up 0 exactly as a down proxy would.
"""
import importlib.machinery
import importlib.util
import os
import socket
import tempfile
import unittest
import warnings

# The reused ferry-dash.load_topology reads litellm.yaml without closing the fd
# (its code, not ours — we lift it verbatim). Silence its benign ResourceWarning
# so the suite output stays readable.
warnings.filterwarnings("ignore", category=ResourceWarning)


def _load_exporter():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "ferry-metrics-exporter")
    loader = importlib.machinery.SourceFileLoader("ferry_metrics_exporter", path)
    spec = importlib.util.spec_from_loader("ferry_metrics_exporter", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


EXP = _load_exporter()


def closed_port():
    """A localhost port that is bound then immediately released (=> refused)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# A realistic mix: two client IPs, 200s + a 429, plus a /v1/models poll and a
# /health poll that must NOT be counted, plus two backend-error log lines.
ACCESS_BATCH_1 = """\
INFO:     1.1.1.1:5001 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     1.1.1.1:5002 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     1.1.1.1:5003 - "POST /v1/chat/completions HTTP/1.1" 429 Too Many Requests
INFO:     2.2.2.2:6001 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     9.9.9.9:7001 - "GET /v1/models HTTP/1.1" 200 OK
INFO:     9.9.9.9:7002 - "GET /health/liveliness HTTP/1.1" 200 OK
2026-08-22 12:00:00 - LiteLLM:ERROR: litellm.RateLimitError: 429 from a gemini key
2026-08-22 12:00:01 - ERROR permission_error: usage limit reached for Kimi K3
"""

ACCESS_BATCH_2 = """\
INFO:     1.1.1.1:5004 - "POST /v1/chat/completions HTTP/1.1" 200 OK
INFO:     2.2.2.2:6002 - "POST /v1/chat/completions HTTP/1.1" 500 Internal Server Error
INFO:     9.9.9.9:7003 - "GET /v1/models HTTP/1.1" 200 OK
"""

# A tiny litellm.yaml in the PRE-RENAME shape (`orchestrator` / `gemini-3.7-flash`):
# a 3-key gemini pool + orchestrator + one fallback dep, and a chain of length 2.
# Kept deliberately: it is what proves the exporter's back-compat fallback-key
# lookup still reports a real chain length for a config written before the rename.
YAML_FIXTURE = """\
model_list:
  - model_name: orchestrator
    litellm_params:
      model: anthropic/k3-256k
  - model_name: orchestrator-deepseek
    litellm_params:
      model: fireworks_ai/deepseek-v4
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.8-flash
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.8-flash
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.8-flash

router_settings:
  routing_strategy: usage-based-routing-v2
  fallbacks: [{"orchestrator": ["orchestrator-gpt56-sol", "orchestrator-deepseek"]}]
"""

# The CURRENT lane shape: `orch` + its fallback hops, the `flash` pool, and the two
# local GPU lanes. local-orch/local-sub are intentionally absent from `fallbacks`.
YAML_FIXTURE_LANES = """\
model_list:
  - model_name: orch
    litellm_params:
      model: zai/glm-5.3
  - model_name: orch-deepseek
    litellm_params:
      model: fireworks_ai/deepseek-v4
  - model_name: flash
    litellm_params:
      model: gemini/gemini-3.8-flash
  - model_name: flash
    litellm_params:
      model: gemini/gemini-3.8-flash
  - model_name: flash
    litellm_params:
      model: gemini/gemini-3.8-flash
  - model_name: local-orch
    litellm_params:
      model: openai/mlx-community/Qwen3.8-27B-nvfp4
      api_base: http://127.0.0.1:8092/v1
  - model_name: local-sub
    litellm_params:
      model: openai/mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4
      api_base: http://127.0.0.1:8093/v1

router_settings:
  routing_strategy: usage-based-routing-v2
  fallbacks: [{"orch": ["orch-gpt56-sol", "orch-deepseek"]}, {"flash": ["orch"]}]
"""


# A lane fixture with the two things YAML_FIXTURE_LANES lacks: explicit
# model_info ids (the only key that joins a live event back to a deployment)
# and a chain long enough to have a middle. lane-b is a pool of two.
YAML_FIXTURE_IDS = """\
model_list:
  - model_name: lane-a
    litellm_params:
      model: someprovider/model-one
    model_info:
      id: a-1
      public: true
  - model_name: lane-b
    litellm_params:
      model: otherprovider/model-two
    model_info:
      id: b-1
  - model_name: lane-b
    litellm_params:
      model: otherprovider/model-three
    model_info:
      id: b-2
  - model_name: lane-c
    litellm_params:
      model: openai/some-local-model
      api_base: http://127.0.0.1:8093/v1
    model_info:
      id: c-1

router_settings:
  routing_strategy: usage-based-routing-v2
  fallbacks: [{"lane-a": ["lane-b", "lane-c"]}]
"""


def parse_metrics(text):
    """Return (types, samples). types: name->typ. samples: list of (name, labels_str, value_str)."""
    types = {}
    samples = []
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, typ = line.split()
            types.setdefault(name, [])
            types[name].append(typ)          # list, so duplicates are visible
        elif line.startswith("#") or not line.strip():
            continue
        else:
            # NAME{labels} VALUE   |   NAME VALUE
            body, _, value = line.rpartition(" ")
            if "{" in body:
                name, labels = body.split("{", 1)
                labels = "{" + labels
            else:
                name, labels = body, ""
            samples.append((name, labels, value))
    return types, samples


class TrafficCountersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "cloud-proxy-8090.log")
        open(self.log, "w").close()          # exists + empty => Activity starts at offset 0
        self.act = EXP.JointActivity(self.log)

    def _append(self, s):
        with open(self.log, "a") as f:
            f.write(s)

    def test_joint_counters_and_exclusion(self):
        self._append(ACCESS_BATCH_1)
        self.act.poll()
        snap = self.act.traffic_snapshot()
        cs = snap["by_client_status"]
        # correct (client, status) joint counters
        self.assertEqual(cs[("1.1.1.1", "200")], 2)
        self.assertEqual(cs[("1.1.1.1", "429")], 1)
        self.assertEqual(cs[("2.2.2.2", "200")], 1)
        # /v1/models + /health polls are NOT counted
        self.assertEqual(self.act.total, 4)
        self.assertNotIn(("9.9.9.9", "200"), cs)
        # backend events detected from the error log lines
        self.assertEqual(snap["backend_events"]["rate_limit"], 1)
        self.assertEqual(snap["backend_events"]["kimi_quota"], 1)
        self.assertIn("rate_limit", snap["backend_event_ts"])
        self.assertIn("kimi_quota", snap["backend_event_ts"])

    def test_counters_are_cumulative_across_polls(self):
        self._append(ACCESS_BATCH_1)
        self.act.poll()
        self._append(ACCESS_BATCH_2)
        self.act.poll()                       # incremental tail — must ADD, never reset
        snap = self.act.traffic_snapshot()
        cs = snap["by_client_status"]
        self.assertEqual(cs[("1.1.1.1", "200")], 3)     # 2 + 1
        self.assertEqual(cs[("2.2.2.2", "500")], 1)     # new in batch 2
        self.assertEqual(cs[("1.1.1.1", "429")], 1)     # preserved from batch 1
        self.assertEqual(self.act.total, 6)             # 4 + 2 (models poll still excluded)

    def test_missing_log_is_safe(self):
        act = EXP.JointActivity(os.path.join(self.tmp, "nope.log"))
        act.poll()                            # must not raise
        self.assertEqual(act.traffic_snapshot()["by_client_status"], {})


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "cloud-proxy-8090.log")
        open(self.log, "w").close()
        self.cfg = os.path.join(self.tmp, "litellm.yaml")
        with open(self.cfg, "w") as f:
            f.write(YAML_FIXTURE)
        self.act = EXP.JointActivity(self.log)
        with open(self.log, "a") as f:
            f.write(ACCESS_BATCH_1)
        self.act.poll()
        # ferry pointed at a closed port => litellm-down path (no network spend)
        self.col = EXP.Collector("http://127.0.0.1:%d" % closed_port(),
                                 "local", self.cfg, self.act)

    def test_render_never_throws_and_emits_meta(self):
        text = self.col.render()
        self.assertIn("ferry_exporter_up 1", text)
        self.assertIn('ferry_exporter_build_info{version="1"} 1', text)
        self.assertTrue(text.endswith("\n"))

    def test_litellm_down_degrades_gracefully(self):
        text = self.col.render()
        self.assertIn("ferry_up 0", text)                 # proxy down
        self.assertIn("ferry_exporter_up 1", text)        # exporter still up
        # no latency / model_info series when down
        self.assertNotIn("ferry_health_check_latency_ms", text)
        self.assertNotIn("ferry_model_info", text)

    def test_one_type_per_metric_name(self):
        types, _ = parse_metrics(self.col.render())
        for name, typs in types.items():
            self.assertEqual(len(typs), 1, "metric %s has %d TYPE lines" % (name, len(typs)))

    def test_no_nan_or_inf_values(self):
        _, samples = parse_metrics(self.col.render())
        self.assertTrue(samples)
        for name, _labels, value in samples:
            f = float(value)                              # every value parses as a plain float
            self.assertEqual(f, f, "NaN in %s" % name)    # NaN != NaN
            self.assertNotIn(value.lower(), ("nan", "inf", "-inf", "+inf"))

    def test_requests_total_series_rendered(self):
        text = self.col.render()
        self.assertRegex(text, r'ferry_requests_total\{client="1\.1\.1\.1",status="200"\} 2')
        self.assertRegex(text, r'ferry_requests_total\{client="1\.1\.1\.1",status="429"\} 1')
        # counter TYPE line present exactly for the counter
        self.assertIn("# TYPE ferry_requests_total counter", text)

    def test_backend_events_rendered(self):
        text = self.col.render()
        self.assertRegex(text, r'ferry_backend_events_total\{kind="rate_limit"\} 1')
        self.assertRegex(text, r'ferry_backend_events_total\{kind="kimi_quota"\} 1')

    def test_content_type_constant(self):
        self.assertEqual(EXP.CONTENT_TYPE, "text/plain; version=0.0.4; charset=utf-8")


class TopologyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "litellm.yaml")
        with open(self.cfg, "w") as f:
            f.write(YAML_FIXTURE)
        self.act = EXP.JointActivity(None)
        self.col = EXP.Collector("http://127.0.0.1:%d" % closed_port(),
                                 "local", self.cfg, self.act)

    def test_worker_pool_size_and_fallback_chain(self):
        text = self.col.render()
        self.assertIn("ferry_worker_pool_size 3", text)          # 3 gemini deployments
        self.assertIn("ferry_fallback_chain_length 2", text)     # chain of length 2
        # one deployment_info per unique (model_name, model)
        self.assertRegex(
            text,
            r'ferry_deployment_info\{model_name="gemini-3\.7-flash",model="gemini/gemini-3\.8-flash"\} 1')
        self.assertRegex(
            text,
            r'ferry_deployment_info\{model_name="orchestrator",model="anthropic/k3-256k"\} 1')
        # route config mtime is emitted (config is readable)
        self.assertIn("ferry_route_config_mtime_seconds", text)

    def test_current_lane_names_topology(self):
        """The post-rename config must report the same topology as the legacy one.

        Guards the exporter's fallback-key lookup: it reads `orch` first and only
        falls back to `orchestrator`, so a rename must not silently flatline
        ferry_fallback_chain_length at 0.
        """
        path = os.path.join(self.tmp, "lanes.yaml")
        with open(path, "w") as f:
            f.write(YAML_FIXTURE_LANES)
        col = EXP.Collector("http://127.0.0.1:%d" % closed_port(), "local", path, self.act)
        text = col.render()
        self.assertIn("ferry_worker_pool_size 3", text)          # 3 flash deployments
        self.assertIn("ferry_fallback_chain_length 2", text)     # orch chain of length 2
        self.assertRegex(
            text,
            r'ferry_deployment_info\{model_name="flash",model="gemini/gemini-3\.8-flash"\} 1')
        self.assertRegex(
            text,
            r'ferry_deployment_info\{model_name="orch",model="zai/glm-5\.3"\} 1')
        # Both local GPU lanes are exported as deployments even though they take
        # no fallbacks - a stopped lane must still be visible in the metrics.
        self.assertRegex(
            text,
            r'ferry_deployment_info\{model_name="local-orch",model="openai/mlx-community/Qwen3\.8-27B-nvfp4"\} 1')
        self.assertRegex(
            text,
            r'ferry_deployment_info\{model_name="local-sub",')

    def test_unreadable_config_omits_topology_but_not_meta(self):
        col = EXP.Collector("http://127.0.0.1:%d" % closed_port(),
                            "local", os.path.join(self.tmp, "does-not-exist.yaml"), self.act)
        text = col.render()                                       # must not raise
        self.assertIn("ferry_exporter_up 1", text)
        self.assertNotIn("ferry_worker_pool_size", text)
        self.assertNotIn("ferry_route_config_mtime_seconds", text)


class LaneTopologyMetricsTest(unittest.TestCase):
    """The labelled topology families, and the two legacy scalars they do NOT
    replace — ferry-backends.json panels 1-2 and the alert rules read those."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "litellm.yaml")
        with open(self.cfg, "w") as f:
            f.write(YAML_FIXTURE_IDS)
        self.col = EXP.Collector("http://127.0.0.1:%d" % closed_port(),
                                 "local", self.cfg, EXP.JointActivity(None))
        self.text = self.col.render()

    def test_every_hop_of_every_lane_is_one_series(self):
        self.assertIn('ferry_lane_hop{lane="lane-a",position="0",hop="lane-a",'
                      'deployment="a-1",model="someprovider/model-one",'
                      'provider="someprovider",pool_size="1"} 1', self.text)
        self.assertIn('ferry_lane_hop{lane="lane-a",position="1",hop="lane-b",'
                      'deployment="b-1",model="otherprovider/model-two",'
                      'provider="otherprovider",pool_size="2"} 1', self.text)
        self.assertIn('ferry_lane_hop{lane="lane-a",position="2",hop="lane-c",'
                      'deployment="c-1",model="openai/some-local-model",'
                      'provider="local",pool_size="1"} 1', self.text)

    def test_a_pooled_hop_emits_one_series_per_member(self):
        # A pool is several DEPLOYMENTS sharing one model_name. Collapsing it to
        # one series would hide exactly the member that went away.
        self.assertIn('deployment="b-1",model="otherprovider/model-two",', self.text)
        self.assertIn('deployment="b-2",model="otherprovider/model-three",', self.text)

    def test_chain_length_counts_hops_including_the_primary(self):
        self.assertIn('ferry_lane_chain_length{lane="lane-a"} 3', self.text)
        self.assertIn('ferry_lane_chain_length{lane="lane-b"} 1', self.text)
        self.assertIn('ferry_lane_chain_length{lane="lane-c"} 1', self.text)

    def test_pool_size_is_per_hop(self):
        self.assertIn('ferry_pool_size{hop="lane-a"} 1', self.text)
        self.assertIn('ferry_pool_size{hop="lane-b"} 2', self.text)
        self.assertIn('ferry_pool_size{hop="lane-c"} 1', self.text)

    def test_the_legacy_scalars_are_retained_unchanged(self):
        self.assertIn("ferry_worker_pool_size 2", self.text)      # largest pool
        self.assertIn("ferry_fallback_chain_length 0", self.text)  # no orch lane here
        self.assertIn("# TYPE ferry_worker_pool_size gauge", self.text)

    def test_an_unreadable_config_omits_the_lane_families_entirely(self):
        col = EXP.Collector("http://127.0.0.1:%d" % closed_port(), "local",
                            os.path.join(self.tmp, "gone.yaml"), EXP.JointActivity(None))
        text = col.render()
        for name in ("ferry_lane_hop", "ferry_lane_chain_length", "ferry_pool_size"):
            self.assertNotIn(name, text)      # no samples => no HELP/TYPE either
        self.assertIn("ferry_exporter_up 1", text)


class EventMetricsTest(unittest.TestCase):
    """The event-derived families. Counters are cumulative since exporter
    start, like ferry_requests_total — the tail opens at EOF, so a 64MB
    backlog is never replayed into a counter on restart."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "litellm.yaml")
        with open(self.cfg, "w") as f:
            f.write(YAML_FIXTURE_IDS)
        self.events = os.path.join(self.tmp, "ferry-events.ndjson")
        open(self.events, "w").close()        # exists + empty => tail starts at 0
        self.rules = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "event-rules.example.json")
        self.col = EXP.Collector("http://127.0.0.1:%d" % closed_port(), "local",
                                 self.cfg, EXP.JointActivity(None),
                                 events_path=self.events, rules_path=self.rules)

    def _write(self, *records):
        import json as _json
        with open(self.events, "a") as f:
            for r in records:
                f.write(_json.dumps(r) + "\n")

    def _event(self, **kw):
        rec = {"t": "2026-08-30T12:00:00.000Z", "call_id": "c", "lane": "lane-a",
               "deployment": "b-1", "model": "otherprovider/model-two",
               "provider": "otherprovider", "api_base": "", "status": 200,
               "fallbacks": 0, "retries": 0, "hop_errors": [],
               "duration_ms": 100.0, "overhead_ms": 1.0, "cost": None,
               "client_ip": "127.0.0.1", "path": "/v1/chat/completions"}
        rec.update(kw)
        return rec

    def test_no_events_file_omits_every_event_family(self):
        col = EXP.Collector("http://127.0.0.1:%d" % closed_port(), "local",
                            self.cfg, EXP.JointActivity(None))
        text = col.render()
        for name in ("ferry_events_total", "ferry_fallback_edges_total",
                     "ferry_deployment_state", "ferry_events_dropped_total"):
            self.assertNotIn(name, text)
        self.assertIn("ferry_exporter_up 1", text)   # the rest still renders

    def test_an_event_counts_by_lane_deployment_provider_and_outcome(self):
        self._write(self._event(), self._event(), self._event(status=500))
        text = self.col.render()
        self.assertIn('ferry_events_total{lane="lane-a",deployment="b-1",'
                      'provider="otherprovider",outcome="ok"} 2', text)
        self.assertIn('ferry_events_total{lane="lane-a",deployment="b-1",'
                      'provider="otherprovider",outcome="error"} 1', text)
        self.assertIn("# TYPE ferry_events_total counter", text)

    def test_counters_accumulate_across_scrapes(self):
        self._write(self._event())
        self.col.render()
        self._write(self._event())
        text = self.col.render()
        self.assertIn('provider="otherprovider",outcome="ok"} 2', text)

    def test_a_fallback_becomes_an_edge_between_two_deployments(self):
        # hop_errors[0] belongs to the hop tried BEFORE the one that answered,
        # so the edge runs a-1 -> b-1 and carries the code that caused it.
        self._write(self._event(fallbacks=1, hop_errors=[
            {"code": 429, "type": "RateLimitError",
             "message": "usage limit reached for this week"}]))
        text = self.col.render()
        self.assertIn('ferry_fallback_edges_total{lane="lane-a",'
                      'from_deployment="a-1",to_deployment="b-1",code="429"} 1', text)

    def test_a_failed_hop_becomes_a_deployment_state(self):
        self._write(self._event(fallbacks=1, hop_errors=[
            {"code": 429, "type": "RateLimitError",
             "message": "usage limit reached for this week"}]))
        text = self.col.render()
        self.assertIn('ferry_deployment_state{deployment="a-1",'
                      'provider="someprovider",state="quota_exhausted"} 1', text)
        self.assertIn('ferry_deployment_state{deployment="b-1",'
                      'provider="otherprovider",state="healthy"} 1', text)
        self.assertIn('ferry_deployment_state_since_seconds{deployment="a-1"}', text)

    def test_only_the_current_state_is_emitted_not_every_state_it_has_been(self):
        # A lingering series for a state the deployment has left would keep an
        # alert firing after the outage cleared.
        self._write(self._event(fallbacks=1, hop_errors=[
            {"code": 429, "message": "usage limit reached"}]))
        self.col.render()
        self._write(self._event(deployment="a-1", provider="someprovider",
                                model="someprovider/model-one"))
        text = self.col.render()
        self.assertIn('ferry_deployment_state{deployment="a-1",'
                      'provider="someprovider",state="healthy"} 1', text)
        self.assertNotIn('deployment="a-1",provider="someprovider",'
                         'state="quota_exhausted"', text)

    def test_a_drop_notice_becomes_a_counter_so_an_overflowing_tap_is_visible(self):
        self._write({"t": "2026-08-30T12:00:00.000Z", "notice": "dropped", "n": 4},
                    self._event())
        text = self.col.render()
        self.assertIn("ferry_events_dropped_total 4", text)
        self.assertIn("# TYPE ferry_events_dropped_total counter", text)
        # and the notice is NOT counted as a request
        self.assertIn('provider="otherprovider",outcome="ok"} 1', text)
        self.assertNotIn('lane=""', text)

    def test_an_unparseable_line_does_not_break_the_scrape(self):
        with open(self.events, "a") as f:
            f.write("{not json\n")
        self._write(self._event())
        text = self.col.render()
        self.assertIn('provider="otherprovider",outcome="ok"} 1', text)

    def test_every_value_is_still_a_plain_number(self):
        self._write(self._event(fallbacks=1, hop_errors=[{"code": 429}]))
        _, samples = parse_metrics(self.col.render())
        for name, _labels, value in samples:
            f = float(value)
            self.assertEqual(f, f, "NaN in %s" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
