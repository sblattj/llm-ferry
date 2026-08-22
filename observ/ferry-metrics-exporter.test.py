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

# A tiny litellm.yaml: a 3-key gemini pool + orchestrator + one fallback dep,
# and a strict fallback chain of length 2.
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
      model: gemini/gemini-3.7-flash
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.7-flash
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.7-flash

router_settings:
  routing_strategy: usage-based-routing-v2
  fallbacks: [{"orchestrator": ["orchestrator-gpt56-sol", "orchestrator-deepseek"]}]
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
            r'ferry_deployment_info\{model_name="gemini-3\.7-flash",model="gemini/gemini-3\.7-flash"\} 1')
        self.assertRegex(
            text,
            r'ferry_deployment_info\{model_name="orchestrator",model="anthropic/k3-256k"\} 1')
        # route config mtime is emitted (config is readable)
        self.assertIn("ferry_route_config_mtime_seconds", text)

    def test_unreadable_config_omits_topology_but_not_meta(self):
        col = EXP.Collector("http://127.0.0.1:%d" % closed_port(),
                            "local", os.path.join(self.tmp, "does-not-exist.yaml"), self.act)
        text = col.render()                                       # must not raise
        self.assertIn("ferry_exporter_up 1", text)
        self.assertNotIn("ferry_worker_pool_size", text)
        self.assertNotIn("ferry_route_config_mtime_seconds", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
