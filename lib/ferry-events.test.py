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

    def test_a_dialect_prefix_on_its_own_provider_host_keeps_the_prefix(self):
        # Native use of a provider: litellm fills in that provider's own host,
        # so the dialect prefix was honest. A live run labelled genuine OpenAI
        # traffic "api.openai.com" before this case existed.
        r = E.record_from_headers(
            hdrs(**{"x_litellm_model_name": "openai/some-model",
                    "x_litellm_model_api_base": "https://api.openai.com"}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["provider"], "openai")

    def test_a_dialect_prefix_on_a_foreign_host_yields_the_host(self):
        # The control: same prefix, an endpoint that merely speaks the dialect.
        r = E.record_from_headers(
            hdrs(**{"x_litellm_model_name": "openai/some-model",
                    "x_litellm_model_api_base": "https://api.example.invalid/v1"}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["provider"], "api.example.invalid")

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
            "duration_ms", "overhead_ms", "cost", "resp_bytes",
            "client_ip", "path", "schema_warnings"})

    def test_schema_warnings_default_to_empty_until_the_front_door_scans(self):
        # record_from_headers never sees a request body; the front door attaches
        # what its scan found. [] means "nothing found or nothing scanned".
        r = E.record_from_headers([], "", "/v1/chat/completions", 200)
        self.assertEqual(r["schema_warnings"], [])
        r2 = E.record_from_headers([], "", "/v1/chat/completions", 200)
        self.assertIsNot(r["schema_warnings"], r2["schema_warnings"])

    def test_resp_bytes_defaults_to_zero_until_the_tap_counts(self):
        # record_from_headers never sees a body; the tap attaches the count it
        # measured. 0 means "not counted", which the dash renders as no data —
        # never as a request that moved no bytes.
        r = E.record_from_headers([], "", "/v1/chat/completions", 200)
        self.assertEqual(r["resp_bytes"], 0)

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

    def test_a_drop_is_announced_in_the_stream_so_a_reader_can_see_it(self):
        """A silently overflowing tap makes the whole live view lie.

        `dropped` lives in the writing process's memory, and every reader is a
        different process, so the only channel is the stream itself. The notice
        is a DIFFERENT SHAPE from a request record - it carries `notice`, never
        the record contract's keys - so a consumer discriminates on one key
        rather than guessing from missing fields.
        """
        log = self._log(max_queue=2)
        log.pause()
        for i in range(6):
            log.offer({"lane": "flash", "i": i})
        self.assertEqual(log.dropped, 4)
        log.close()                       # drains the two that were accepted
        lines = [json.loads(l) for l in open(log.path) if l.strip()]
        notices = [l for l in lines if l.get("notice") == "dropped"]
        self.assertEqual(len(notices), 1, "expected exactly one drop notice")
        self.assertEqual(notices[0]["n"], 4)
        self.assertIn("t", notices[0])
        # The notice precedes the record that triggered it, and that record is
        # unchanged - the drop count never rides on the record contract.
        self.assertEqual(lines[0]["notice"], "dropped")
        self.assertEqual(lines[1]["i"], 0)
        # Only ONE of the two accepted records lands: close() is a stop, not a
        # flush. It sets the stop flag before the writer's next loop check, so
        # whatever is still queued is lost - the deliberate trade against ever
        # blocking a response, and the reason flush() exists separately.
        self.assertEqual([l["i"] for l in lines if "i" in l], [0])

    def test_no_drops_means_no_notice_line(self):
        log = self._log()
        log.offer({"lane": "flash"})
        log.flush()
        log.close()
        lines = [json.loads(l) for l in open(log.path) if l.strip()]
        self.assertEqual([l.get("notice") for l in lines], [None])

    def test_the_notice_is_emitted_once_per_new_drop_not_once_per_record(self):
        # Re-announcing the same total on every subsequent record would bury the
        # stream in notices the moment the queue overflows once.
        log = self._log(max_queue=2)
        log.pause()
        for i in range(6):
            log.offer({"lane": "flash", "i": i})
        log.close()
        log2 = E.EventLog(log.path, max_queue=8)
        log2.offer({"lane": "flash", "i": 9})
        log2.flush()
        log2.close()
        lines = [json.loads(l) for l in open(log.path) if l.strip()]
        self.assertEqual(len([l for l in lines if l.get("notice")]), 1)

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


def tool(name, properties):
    """One OpenAI-shaped tool entry, the form every client here sends."""
    return {"type": "function", "function": {
        "name": name,
        "parameters": {"type": "object", "properties": properties}}}


class TestSchemaWarnings(unittest.TestCase):
    """tool_schema_warnings names the payload shape a provider rejects silently.

    The rule set is empirical, not a JSON-Schema validator: each entry is a
    shape that was SEEN to hang a lane. The first is the 2026-09-04 one — an
    array property with no `items`, which Gemini's function-declaration
    validator rejects and which OpenRouter then answered with no headers at
    all for the deployment's full 600s timeout.
    """

    # The exact property that hung every opencode flash call: cdp-toolkit's
    # evaluate_script `args`, captured from the wire that day.
    OUTAGE = tool("cdp-toolkit_evaluate_script", {
        "expression": {"type": "string"},
        "args": {"type": "array", "description": "Positional JSON arguments."},
    })

    def test_the_outage_payload_is_named_by_tool_and_path(self):
        found = E.tool_schema_warnings({"tools": [self.OUTAGE]})
        self.assertEqual(found, [{"tool": "cdp-toolkit_evaluate_script",
                                  "path": "args",
                                  "rule": "array_without_items"}])

    def test_control_the_fixed_payload_is_clean(self):
        # The one-line fix that ended the outage: `items: {}`. Same tool,
        # same everything else — one factor varied, and the finding must go.
        fixed = tool("cdp-toolkit_evaluate_script", {
            "expression": {"type": "string"},
            "args": {"type": "array", "items": {},
                     "description": "Positional JSON arguments."},
        })
        self.assertEqual(E.tool_schema_warnings({"tools": [fixed]}), [])

    def test_every_rule_key_is_documented(self):
        found = E.tool_schema_warnings({"tools": [self.OUTAGE]})
        for f in found:
            self.assertIn(f["rule"], E.SCHEMA_RULES)

    def test_a_body_with_no_tools_is_clean(self):
        self.assertEqual(E.tool_schema_warnings({"messages": []}), [])
        self.assertEqual(E.tool_schema_warnings({"tools": []}), [])
        self.assertEqual(E.tool_schema_warnings({"tools": None}), [])

    def test_arrays_nested_in_objects_and_arrays_are_found_with_a_path(self):
        t = tool("fill_form", {
            "fields": {"type": "array", "items": {
                "type": "object", "properties": {
                    "value": {"type": "string"},
                    "options": {"type": "array"},
                }}},
            "meta": {"type": "object", "properties": {
                "tags": {"type": "array"}}},
        })
        found = E.tool_schema_warnings({"tools": [t]})
        self.assertEqual([f["path"] for f in found],
                         ["fields[].options", "meta.tags"])
        self.assertTrue(all(f["tool"] == "fill_form" for f in found))

    def test_a_nullable_type_list_containing_array_still_counts(self):
        t = tool("t", {"xs": {"type": ["array", "null"]}})
        self.assertEqual([f["path"] for f in E.tool_schema_warnings({"tools": [t]})],
                         ["xs"])

    def test_alternatives_and_additional_properties_are_walked(self):
        t = tool("t", {
            "either": {"anyOf": [{"type": "string"}, {"type": "array"}]},
            "bag": {"type": "object",
                    "additionalProperties": {"type": "array"}},
        })
        paths = [f["path"] for f in E.tool_schema_warnings({"tools": [t]})]
        self.assertEqual(paths, ["either<anyOf 1>", "bag.*"])

    def test_a_bare_array_root_is_reported_as_root(self):
        t = {"type": "function", "function": {
            "name": "t", "parameters": {"type": "array"}}}
        self.assertEqual(E.tool_schema_warnings({"tools": [t]})[0]["path"],
                         "(root)")

    def test_malformed_tools_never_raise_and_clean_neighbours_still_scan(self):
        junk = [None, 3, "x", {"function": "nope"}, {"function": {"name": 7}},
                {"type": "function", "function": {"name": "p",
                                                   "parameters": "str"}},
                self.OUTAGE]
        found = E.tool_schema_warnings({"tools": junk})
        self.assertEqual([f["tool"] for f in found],
                         ["cdp-toolkit_evaluate_script"])
        self.assertEqual(E.tool_schema_warnings(None), [])
        self.assertEqual(E.tool_schema_warnings("not a dict"), [])
        self.assertEqual(E.tool_schema_warnings({"tools": "nope"}), [])

    def test_a_tool_with_no_usable_name_is_still_reported(self):
        t = {"type": "function", "function": {"name": 7, "parameters": {
            "type": "object", "properties": {"a": {"type": "array"}}}}}
        self.assertEqual(E.tool_schema_warnings({"tools": [t]}),
                         [{"tool": "", "path": "a", "rule": "array_without_items"}])

    def test_findings_are_capped_so_one_request_cannot_inflate_its_record(self):
        many = [tool("t%d" % i, {"a": {"type": "array"}}) for i in range(50)]
        found = E.tool_schema_warnings({"tools": many})
        self.assertEqual(len(found), E.SCHEMA_WARNINGS_LIMIT)
        self.assertEqual(len(E.tool_schema_warnings({"tools": many}, limit=3)), 3)

    def test_a_self_referencing_schema_terminates(self):
        loop = {"type": "object", "properties": {}}
        loop["properties"]["self"] = loop
        t = {"type": "function", "function": {"name": "t", "parameters": loop}}
        self.assertEqual(E.tool_schema_warnings({"tools": [t]}), [])

    def test_a_finding_is_json_serialisable_as_part_of_a_record(self):
        rec = E.record_from_headers([], "", "/v1/chat/completions", 200)
        rec["schema_warnings"] = E.tool_schema_warnings({"tools": [self.OUTAGE]})
        line = json.loads(json.dumps(rec))
        self.assertEqual(line["schema_warnings"][0]["path"], "args")


if __name__ == "__main__":
    unittest.main(verbosity=2)
