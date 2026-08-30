"""Tests for lib/ferry_events.py — the per-request event record and its writer.

Run: python3 lib/ferry-events.test.py
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest


def _load():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ferry_events.py")
    spec = importlib.util.spec_from_loader(
        "ferry_events", importlib.machinery.SourceFileLoader("ferry_events", p))
    m = importlib.util.module_from_spec(spec)
    sys.modules["ferry_events"] = m
    spec.loader.exec_module(m)
    return m


E = _load()


def hdrs(**kw):
    """ASGI delivers headers as a list of (bytes, bytes)."""
    return [(k.replace("_", "-").encode(), str(v).encode()) for k, v in kw.items()]


class TestRecord(unittest.TestCase):
    def test_lane_comes_from_model_group_not_model_name(self):
        # x-litellm-model-name is the UNDERLYING model string; the lane the
        # client asked for is x-litellm-model-group. Confusing them labels every
        # event with a provider path instead of a lane.
        r = E.record_from_headers(
            hdrs(**{"x_litellm_model_group": "flash",
                    "x_litellm_model_name": "someprovider/some-model",
                    "x_litellm_model_id": "flash-alt-1"}),
            "10.0.0.9", "/v1/chat/completions", 200)
        self.assertEqual(r["lane"], "flash")
        self.assertEqual(r["deployment"], "flash-alt-1")
        self.assertEqual(r["model"], "someprovider/some-model")

    def test_provider_derives_from_model_prefix(self):
        r = E.record_from_headers(
            hdrs(**{"x_litellm_model_name": "someprovider/some-model"}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["provider"], "someprovider")

    def test_provider_falls_back_to_api_base_host(self):
        r = E.record_from_headers(
            hdrs(**{"x_litellm_model_name": "some-model",
                    "x_litellm_model_api_base": "https://api.example.invalid/v1"}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["provider"], "api.example.invalid")

    def test_a_dialect_prefix_over_a_loopback_base_is_local_not_openai(self):
        # The real shape of a local GPU lane: litellm addresses any
        # OpenAI-compatible server as openai/<model> with an explicit api_base.
        # Trusting the prefix labels the on-box inference server "openai" — this
        # is not hypothetical, it is what the naive rule did to real captured
        # headers during the plan pre-flight.
        r = E.record_from_headers(
            hdrs(**{"x_litellm_model_name": "openai/mlx-community/some-local-model",
                    "x_litellm_model_api_base": "http://127.0.0.1:8093/v1"}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["provider"], "local")

    def test_a_real_prefix_with_an_api_base_still_names_the_provider(self):
        # A cloud lane that pins api_base must NOT be collapsed to its host.
        r = E.record_from_headers(
            hdrs(**{"x_litellm_model_name": "someprovider/some-model",
                    "x_litellm_model_api_base": "https://api.example.invalid/v1"}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["provider"], "someprovider")

    def test_fallback_errors_are_parsed_into_hops(self):
        r = E.record_from_headers(
            hdrs(**{"x_litellm_attempted_fallbacks": 1,
                    "x_litellm_fallback_errors":
                        '[{"message":"m","type":"RateLimitError","param":null,"code":"429"}]'}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["fallbacks"], 1)
        self.assertEqual(r["hop_errors"][0]["code"], "429")

    def test_missing_headers_yield_unknown_lane_not_a_dropped_record(self):
        # An error response may carry no x-litellm-* headers at all. Dropping
        # the record loses exactly the failures worth seeing.
        r = E.record_from_headers([], "10.0.0.9", "/v1/chat/completions", 500)
        self.assertEqual(r["lane"], "unknown")
        self.assertEqual(r["status"], 500)

    def test_malformed_fallback_errors_do_not_raise(self):
        r = E.record_from_headers(
            hdrs(**{"x_litellm_fallback_errors": "{not json"}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["hop_errors"], [])

    def test_a_non_list_fallback_errors_payload_is_ignored(self):
        r = E.record_from_headers(
            hdrs(**{"x_litellm_fallback_errors": '{"code":"429"}'}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["hop_errors"], [])

    def test_undecodable_header_bytes_are_skipped_not_fatal(self):
        r = E.record_from_headers(
            [(b"x-litellm-model-group", b"flash"), (b"\xff\xfe", b"\xff")],
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["lane"], "flash")

    def test_key_set_is_exactly_the_contract(self):
        # The exporter and the live view read this record by name. Adding a key
        # is an allocation; this assertion is where it gets noticed.
        r = E.record_from_headers([], "", "/v1/chat/completions", 200)
        self.assertEqual(set(r), {
            "t", "call_id", "lane", "deployment", "model", "provider",
            "api_base", "status", "fallbacks", "retries", "hop_errors",
            "duration_ms", "overhead_ms", "cost", "client_ip", "path"})

    def test_timestamp_is_rfc3339_utc_with_milliseconds(self):
        r = E.record_from_headers([], "", "/v1/chat/completions", 200)
        self.assertRegex(r["t"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class TestEventLog(unittest.TestCase):
    def _log(self, **kw):
        d = tempfile.mkdtemp()
        return E.EventLog(os.path.join(d, "ferry-events.ndjson"), **kw)

    def test_offer_writes_one_json_line_per_record(self):
        log = self._log()
        log.offer({"lane": "flash"})
        log.offer({"lane": "heavy"})
        log.flush()
        lines = [l for l in open(log.path) if l.strip()]
        self.assertEqual([json.loads(l)["lane"] for l in lines], ["flash", "heavy"])
        log.close()

    def test_a_full_queue_drops_instead_of_blocking(self):
        log = self._log(max_queue=2)
        log.pause()                      # park the writer so the queue really fills
        accepted = [log.offer({"n": i}) for i in range(6)]
        self.assertEqual(accepted.count(True), 2)
        self.assertEqual(log.dropped, 4)
        log.close()

    def test_an_unwritable_path_disables_the_log_and_never_raises(self):
        log = E.EventLog("/proc/nonexistent/ferry-events.ndjson")
        log.offer({"lane": "flash"})     # must not raise
        log.flush()
        self.assertFalse(log.healthy)
        log.close()

    def test_rotation_at_max_bytes_keeps_writing(self):
        log = self._log(max_bytes=200)
        for i in range(50):
            log.offer({"lane": "flash", "i": i, "pad": "x" * 40})
        log.flush()
        self.assertTrue(os.path.exists(log.path))
        self.assertTrue(os.path.exists(log.path + ".1"))
        log.close()

    def test_default_path_is_in_the_ferry_log_dir_and_is_not_a_dot_log(self):
        p = E.default_path()
        self.assertTrue(p.endswith("/ferry-logs/ferry-events.ndjson"))
        # find_log() discovers cloud-proxy-<port>.log; a .log suffix here would
        # risk the shipper tailing the event stream back into itself.
        self.assertFalse(p.endswith(".log"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
