#!/usr/bin/env python3
"""Stdlib unittest for ferry-log-shipper.

Run:  python3 observ/ferry-log-shipper.test.py

The shipper has no .py extension, so we load it via importlib. Every fixture
line below is a REAL shape taken from the ferry proxy log
(`$TMPDIR/ferry-logs/cloud-proxy-8090.log`), ANSI colour escapes included.
Nothing here touches the network: the sink is exercised through build_body()
and through a Shipper whose post() is monkeypatched, so a failing/absent
VictoriaLogs is simulated, never contacted.
"""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest


def _load_shipper():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "ferry-log-shipper")
    loader = importlib.machinery.SourceFileLoader("ferry_log_shipper", path)
    spec = importlib.util.spec_from_loader("ferry_log_shipper", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


S = _load_shipper()

ESC = "\x1b"

# ── real lines from the proxy log ──────────────────────────────────────────
ACCESS_OK = 'INFO:     192.168.0.167:54823 - "POST /v1/chat/completions HTTP/1.1" 200 OK'
ACCESS_MODELS = 'INFO:     127.0.0.1:63591 - "GET /v1/models HTTP/1.1" 200 OK'
ACCESS_REDIRECT = 'INFO:     127.0.0.1:63621 - "GET /metrics HTTP/1.1" 307 Temporary Redirect'
ACCESS_429 = 'INFO:     10.0.0.5:63500 - "POST /v1/chat/completions HTTP/1.1" 429 Too Many Requests'
ACCESS_500 = 'INFO:     10.0.0.5:5001 - "POST /v1/chat/completions HTTP/1.1" 500 Internal Server Error'
# an ephemeral port that CONTAINS "429"/"500" must not fake an error level
ACCESS_PORT_TRAP = 'INFO:     127.0.0.1:63429 - "GET /health/liveliness HTTP/1.1" 200 OK'

REGISTER_GLM = (ESC + "[92m15:51:27 - LiteLLM:WARNING" + ESC + "[0m: utils.py:2782 - "
                "register_model: model=zai/glm-5.3 not in built-in cost map and no "
                "prefix/region variant matched; cache cost fields will default to 0.")
REGISTER_K3 = (ESC + "[92m15:51:27 - LiteLLM:WARNING" + ESC + "[0m: utils.py:2782 - "
               "register_model: model=anthropic/k3-256k not in built-in cost map")
REGISTER_HASH = (ESC + "[92m15:51:27 - LiteLLM:WARNING" + ESC + "[0m: utils.py:2782 - "
                 "register_model: model=30ebd5fade16139b4ac2a3573307ae061b8fa6057550"
                 "baf8e74a4c4ff0c7f7f5 not in built-in cost map")
REGISTER_DEEPSEEK = ("15:51:27 - LiteLLM:WARNING: register_model: "
                     "model=fireworks_ai/accounts/fireworks/models/deepseek-v4-pro-0813 "
                     "not in built-in cost map")
FALLBACK = ("15:52:01 - LiteLLM Router:INFO: Falling back to model_group=orchestrator-"
            "fallback after litellm_model_name=anthropic/k3-256k failed")
COOLDOWN = "15:52:02 - LiteLLM Router:INFO: Adding deployment to cooldown for 60s"
RESOURCE_EXHAUSTED = ("15:53:00 - LiteLLM:ERROR: VertexAIException - 429 "
                      "RESOURCE_EXHAUSTED for model gemini/gemini-3.7-flash")
TRACEBACK = "Traceback (most recent call last):"
EXCEPTION = "Exception occurred during processing of request from ('192.168.0.167', 55264)"
BANNER = "   " + ESC + "[1;37m#--------------------------------------------#" + ESC + "[0m"
STARTUP = "INFO:     Uvicorn running on http://0.0.0.0:8090 (Press CTRL+C to quit)"


class TestScrub(unittest.TestCase):
    def test_ansi_and_control_bytes_are_removed(self):
        out = S.scrub(REGISTER_GLM)
        self.assertNotIn(ESC, out)
        self.assertNotIn("[92m", out)
        self.assertTrue(out.startswith("15:51:27 - LiteLLM:WARNING: "), out)
        self.assertIn("model=zai/glm-5.3", out)

    def test_plain_line_is_untouched(self):
        self.assertEqual(S.scrub(ACCESS_OK), ACCESS_OK)

    def test_trailing_newline_stripped(self):
        self.assertEqual(S.scrub(ACCESS_OK + "\r"), ACCESS_OK)


class TestAccessLines(unittest.TestCase):
    def test_ip_status_and_info_level(self):
        r = S.parse_line(ACCESS_OK)
        self.assertEqual(r["client_ip"], "192.168.0.167")
        self.assertEqual(r["status"], "200")
        self.assertEqual(r["level"], "info")
        self.assertEqual(r["source"], "proxy")
        self.assertEqual(r["_msg"], ACCESS_OK)

    def test_4xx_is_warn(self):
        r = S.parse_line(ACCESS_429)
        self.assertEqual((r["status"], r["level"], r["client_ip"]),
                         ("429", "warn", "10.0.0.5"))

    def test_5xx_is_error(self):
        r = S.parse_line(ACCESS_500)
        self.assertEqual((r["status"], r["level"]), ("500", "error"))

    def test_3xx_is_info(self):
        r = S.parse_line(ACCESS_REDIRECT)
        self.assertEqual((r["status"], r["level"]), ("307", "info"))

    def test_status_overrides_keyword_hints(self):
        # the phrase "Too Many Requests" plus a 429 in the port must not double-count;
        # the access status is authoritative, and 429 -> warn (not error).
        self.assertEqual(S.parse_line(ACCESS_429)["level"], "warn")

    def test_port_digits_do_not_fake_an_error(self):
        r = S.parse_line(ACCESS_PORT_TRAP)
        self.assertEqual((r["status"], r["level"]), ("200", "info"))

    def test_models_poll_still_parses(self):
        r = S.parse_line(ACCESS_MODELS)
        self.assertEqual((r["client_ip"], r["status"], r["level"]),
                         ("127.0.0.1", "200", "info"))

    def test_non_access_line_has_empty_ip_and_status(self):
        r = S.parse_line(REGISTER_GLM)
        self.assertEqual(r["client_ip"], "")
        self.assertEqual(r["status"], "")


class TestModelExtraction(unittest.TestCase):
    def test_model_equals_with_provider_prefix(self):
        self.assertEqual(S.parse_line(REGISTER_GLM)["model"], "zai/glm-5.3")

    def test_register_model_prefix_does_not_capture_the_word_model(self):
        # `register_model: model=...` must yield the id, never the literal "model"
        self.assertNotEqual(S.parse_line(REGISTER_GLM)["model"], "model")

    def test_anthropic_k3(self):
        self.assertEqual(S.parse_line(REGISTER_K3)["model"], "anthropic/k3-256k")

    def test_long_fireworks_deepseek_path(self):
        self.assertEqual(S.parse_line(REGISTER_DEEPSEEK)["model"],
                         "fireworks_ai/accounts/fireworks/models/deepseek-v4-pro-0813")

    def test_anonymised_hash_is_not_a_model(self):
        self.assertEqual(S.parse_line(REGISTER_HASH)["model"], "")

    def test_litellm_model_name_wins_over_model_group(self):
        r = S.parse_line(FALLBACK)
        self.assertEqual(r["model"], "anthropic/k3-256k")
        self.assertEqual(r["requested_model"], "orchestrator-fallback")

    def test_known_id_scan_when_no_key_value_pair(self):
        r = S.parse_line("15:53:00 - LiteLLM:INFO: selected gemini-3.7-flash for this call")
        self.assertEqual(r["model"], "gemini-3.7-flash")

    def test_known_id_scan_matches_bare_k3(self):
        # The orch lane moved from `k3-256k` to the 1M-context `k3` (2026-08-26).
        # `k3` is a WHOLE id, so the KNOWN_MODEL scan must match it with no
        # suffix — the key/value patterns cover `model=`, this covers prose.
        r = S.parse_line("15:53:00 - LiteLLM:INFO: selected anthropic/k3 for this call")
        self.assertEqual(r["model"], "anthropic/k3")

    def test_known_id_scan_still_matches_suffixed_k3(self):
        r = S.parse_line("15:53:00 - LiteLLM:INFO: selected anthropic/k3-256k for this call")
        self.assertEqual(r["model"], "anthropic/k3-256k")

    def test_known_group_scan_fills_requested_model(self):
        r = S.parse_line("LiteLLM: Proxy initialized with Config, Set models: orchestrator-kimi")
        self.assertEqual(r["requested_model"], "orchestrator-kimi")

    def test_current_lane_names_are_recognised_as_groups(self):
        """Every lane name the stack serves must scan as a model GROUP.

        The names were shortened (orchestrator->orch, gemini-3.7-flash->flash) and
        two GPU lanes were added; if the group regex misses one, its log lines lose
        their requested_model label and drop out of the Grafana per-lane views.
        """
        for lane in ("orch", "orch-kimi", "orch-deepseek", "orch-gpt56-sol",
                     "flash", "local-orch", "local-sub",
                     "orchestrator", "orchestrator-fallback"):
            r = S.parse_line(
                "LiteLLM: Proxy initialized with Config, Set models: %s" % lane)
            self.assertEqual(r["requested_model"], lane, "lane %r not recognised" % lane)

    def test_group_scan_does_not_match_inside_a_backend_id(self):
        """`flash` must not be clipped out of the middle of `gemini-3.7-flash`.

        The backend id is a MODEL, not a group; a bare-substring match there would
        mislabel every Gemini line as a request for the `flash` lane.
        """
        r = S.parse_line("15:53:00 - LiteLLM:INFO: selected gemini-3.7-flash for this call")
        self.assertEqual(r["model"], "gemini-3.7-flash")
        self.assertEqual(r["requested_model"], "")

    def test_model_absent_leaves_empty_strings(self):
        r = S.parse_line(ACCESS_OK)
        self.assertEqual(r["model"], "")
        self.assertEqual(r["requested_model"], "")

    def test_stopword_capture_is_rejected(self):
        r = S.parse_line("LiteLLM:WARNING: model not in built-in cost map")
        self.assertEqual(r["model"], "")

    def test_resource_exhausted_line_keeps_its_model(self):
        r = S.parse_line(RESOURCE_EXHAUSTED)
        self.assertEqual(r["model"], "gemini/gemini-3.7-flash")


class TestLevels(unittest.TestCase):
    def test_litellm_warning_is_warn(self):
        self.assertEqual(S.parse_line(REGISTER_GLM)["level"], "warn")

    def test_fallback_is_warn(self):
        self.assertEqual(S.parse_line(FALLBACK)["level"], "warn")

    def test_cooldown_is_warn(self):
        self.assertEqual(S.parse_line(COOLDOWN)["level"], "warn")

    def test_resource_exhausted_is_error(self):
        self.assertEqual(S.parse_line(RESOURCE_EXHAUSTED)["level"], "error")

    def test_traceback_is_error(self):
        self.assertEqual(S.parse_line(TRACEBACK)["level"], "error")

    def test_exception_is_error(self):
        self.assertEqual(S.parse_line(EXCEPTION)["level"], "error")

    def test_plain_startup_line_is_info(self):
        self.assertEqual(S.parse_line(STARTUP)["level"], "info")

    def test_banner_is_info(self):
        self.assertEqual(S.parse_line(BANNER)["level"], "info")


class TestRecordShape(unittest.TestCase):
    FIELDS = {"_time", "_msg", "source", "level", "status", "model",
              "requested_model", "client_ip"}

    def test_every_contract_field_present_and_a_string(self):
        sh = S.Shipper("http://127.0.0.1:9428", dry_run=True)
        for line in (ACCESS_OK, REGISTER_GLM, FALLBACK, TRACEBACK, BANNER):
            rec = S.parse_line(line)
            sh.add(rec)
            self.assertEqual(set(rec), self.FIELDS, line)
            for k, v in rec.items():
                self.assertIsInstance(v, str, "%s in %r" % (k, line))

    def test_time_is_rfc3339_utc_and_strictly_increasing(self):
        sh = S.Shipper("http://127.0.0.1:9428", dry_run=True)
        stamps = [sh.stamp() for _ in range(200)]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))
        for s in stamps[:5]:
            self.assertTrue(s.endswith("Z"), s)
            self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


class TestBody(unittest.TestCase):
    def test_url_matches_the_contract(self):
        sh = S.Shipper("http://127.0.0.1:9428/")
        self.assertEqual(sh.url,
                         "http://127.0.0.1:9428/insert/jsonline?_stream_fields=source,level")

    def test_body_is_newline_delimited_json(self):
        sh = S.Shipper("http://127.0.0.1:9428", dry_run=True)
        for line in (ACCESS_OK, REGISTER_GLM, ACCESS_500):
            sh.add(S.parse_line(line))
        body = S.Shipper.build_body(sh.pending).decode()
        rows = body.split("\n")
        self.assertEqual(len(rows), 3)
        for row in rows:
            obj = json.loads(row)
            self.assertEqual(obj["source"], "proxy")
            self.assertIn(obj["level"], ("info", "warn", "error"))
        self.assertEqual(json.loads(rows[2])["level"], "error")

    def test_embedded_quotes_survive_round_trip(self):
        rec = S.parse_line(ACCESS_OK)
        rec["_time"] = "2026-08-22T00:00:00.000000Z"
        self.assertEqual(json.loads(S.Shipper.build_body([rec]).decode())["_msg"],
                         ACCESS_OK)


class TestBatchingAndRetry(unittest.TestCase):
    def _shipper(self, ok):
        sh = S.Shipper("http://127.0.0.1:9428", batch_size=2, flush_interval=0)
        sh.posted = []

        def fake_post(records):
            sh.posted.append(list(records))
            return (True, None) if ok else (False, "Connection refused")

        sh.post = fake_post
        return sh

    def test_batch_is_one_post(self):
        sh = self._shipper(ok=True)
        for line in (ACCESS_OK, ACCESS_500):
            sh.add(S.parse_line(line))
        self.assertTrue(sh.flush())
        self.assertEqual(len(sh.posted), 1)
        self.assertEqual(len(sh.posted[0]), 2)
        self.assertEqual(sh.pending, [])
        self.assertEqual(sh.shipped, 2)

    def test_failure_keeps_the_lines_and_backs_off(self):
        sh = self._shipper(ok=False)
        sh.add(S.parse_line(ACCESS_OK))
        self.assertFalse(sh.flush())
        self.assertEqual(len(sh.pending), 1)          # nothing lost
        self.assertGreater(sh.retry_at, 0)            # backing off
        self.assertEqual(sh.backoff, S.BACKOFF_START * 2)
        self.assertFalse(sh.due())                    # suppressed until retry_at
        self.assertEqual(sh.failures, 1)

    def test_backoff_is_capped(self):
        sh = self._shipper(ok=False)
        for _ in range(20):
            sh.add(S.parse_line(ACCESS_OK))
            sh.flush(force=True)
        self.assertLessEqual(sh.backoff, S.BACKOFF_MAX)

    def test_pending_buffer_is_bounded_dropping_oldest(self):
        sh = S.Shipper("http://127.0.0.1:9428", dry_run=True)
        original = S.MAX_PENDING
        S.MAX_PENDING = 5
        try:
            for i in range(9):
                rec = S.parse_line(ACCESS_OK)
                rec["client_ip"] = str(i)             # tag so we can see which survived
                sh.add(rec)
        finally:
            S.MAX_PENDING = original
        self.assertEqual(len(sh.pending), 5)
        self.assertEqual(sh.dropped, 4)
        self.assertEqual([r["client_ip"] for r in sh.pending],
                         ["4", "5", "6", "7", "8"])   # oldest dropped, newest kept


class TestTailer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-log-shipper-test-")
        self.path = os.path.join(self.dir, "cloud-proxy-8090.log")

    def write(self, text, mode="a"):
        with open(self.path, mode) as f:
            f.write(text)

    def test_tail_from_end_skips_history(self):
        self.write("old line 1\nold line 2\n", "w")
        t = S.Tailer(self.path, pinned=True)
        self.assertEqual(t.poll(), [])                # history is NOT re-shipped
        self.write(ACCESS_OK + "\n")
        self.assertEqual(t.poll(), [ACCESS_OK])

    def test_from_start_ships_history(self):
        self.write("old line 1\nold line 2\n", "w")
        t = S.Tailer(self.path, from_start=True, pinned=True)
        self.assertEqual(t.poll(), ["old line 1", "old line 2"])

    def test_partial_line_is_held_until_its_newline_arrives(self):
        self.write("", "w")
        t = S.Tailer(self.path, pinned=True)
        t.poll()
        self.write('INFO:     1.2.3.4:1 - "POST /v1/chat')
        self.assertEqual(t.poll(), [])                # incomplete: nothing shipped
        self.write('/completions HTTP/1.1" 200 OK\n')
        got = t.poll()
        self.assertEqual(len(got), 1)
        self.assertEqual(S.parse_line(got[0])["status"], "200")

    def test_truncation_to_a_shorter_file_rereads_from_zero(self):
        self.write("a longer original line\nand another\n", "w")
        t = S.Tailer(self.path, pinned=True)
        t.poll()
        self.write("short\n", "w")                    # size shrinks below our offset
        self.assertEqual(t.poll(), ["short"])

    def test_in_place_rewrite_that_GROWS_rereads_from_zero(self):
        # The trap: `ferry up` reopens the log with `>` (same inode) and writes
        # MORE bytes than we had read, so `size < offset` never fires. Without
        # the head fingerprint this resumes mid-line and ships "h after truncate".
        self.write("a\nb\n", "w")
        t = S.Tailer(self.path, pinned=True)
        t.poll()
        self.write("fresh after truncate\n", "w")
        self.assertEqual(t.poll(), ["fresh after truncate"])

    def test_reading_continues_correctly_after_a_rewrite(self):
        self.write("a\nb\n", "w")
        t = S.Tailer(self.path, pinned=True)
        t.poll()
        self.write("INFO:     Started server process [1]\n", "w")
        t.poll()
        self.write(ACCESS_OK + "\n")                  # normal append after the restart
        self.assertEqual(t.poll(), [ACCESS_OK])

    def test_plain_appends_never_trigger_a_false_rewind(self):
        self.write("head line\n", "w")                # shorter than the fingerprint
        t = S.Tailer(self.path, from_start=True, pinned=True)
        self.assertEqual(t.poll(), ["head line"])
        for i in range(5):
            self.write("line %d\n" % i)
            self.assertEqual(t.poll(), ["line %d" % i])

    def test_rotation_by_inode_rereads_from_zero(self):
        self.write("a\nb\n", "w")
        t = S.Tailer(self.path, pinned=True)
        t.poll()
        rotated = self.path + ".1"
        os.rename(self.path, rotated)                 # new inode under the same name
        self.write("line in the rotated-in file\n", "w")
        self.assertEqual(t.poll(), ["line in the rotated-in file"])

    def test_missing_file_is_not_fatal_and_recovers(self):
        t = S.Tailer(self.path, pinned=True)
        self.assertEqual(t.poll(), [])                # file does not exist yet
        self.write("first line ever\n", "w")
        self.assertEqual(t.poll(), ["first line ever"])

    def test_deleted_then_recreated_file_is_read_from_zero(self):
        self.write("a\nb\n", "w")
        t = S.Tailer(self.path, pinned=True)
        t.poll()
        os.remove(self.path)
        self.assertEqual(t.poll(), [])
        self.write("brand new log\n", "w")
        self.assertEqual(t.poll(), ["brand new log"])

    def test_invalid_utf8_does_not_desync_offsets(self):
        with open(self.path, "wb") as f:
            f.write(b"")
        t = S.Tailer(self.path, pinned=True)
        t.poll()
        with open(self.path, "ab") as f:
            f.write(b"caf\xe9 bad byte\n" + ACCESS_OK.encode() + b"\n")
        got = t.poll()
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1], ACCESS_OK)

    def test_multibyte_split_across_polls(self):
        with open(self.path, "wb") as f:
            f.write(b"")
        t = S.Tailer(self.path, pinned=True)
        t.poll()
        with open(self.path, "ab") as f:              # first half of a 2-byte é
            f.write(b"caf\xc3")
        t.poll()
        with open(self.path, "ab") as f:
            f.write(b"\xa9\n")
        # the byte offset never desyncs; worst case the split char is replaced
        got = t.poll()
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("caf"), got[0])


class TestDiscovery(unittest.TestCase):
    def test_find_log_never_raises(self):
        got = S.find_log("8090")
        self.assertTrue(got is None or os.path.exists(got))

    def test_find_log_prefers_the_exact_port_file(self):
        d = tempfile.mkdtemp(prefix="ferry-find-log-test-")
        logs = os.path.join(d, "ferry-logs")
        os.makedirs(logs)
        exact = os.path.join(logs, "cloud-proxy-8090.log")
        other = os.path.join(logs, "cloud-proxy-9999.log")
        for p in (exact, other):
            with open(p, "w") as f:
                f.write("x\n")
        os.utime(other, (2 ** 31, 2 ** 31))           # make the WRONG one newest
        old = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = d
        try:
            self.assertEqual(S.find_log("8090"), exact)
        finally:
            if old is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = old


class TestCli(unittest.TestCase):
    def test_defaults_match_the_contract(self):
        a = S.parse_args([])
        self.assertEqual(a.vlogs, "http://127.0.0.1:9428")
        self.assertIsNone(a.log)
        self.assertFalse(a.from_start)                # tail from END by default

    def test_flags_parse(self):
        a = S.parse_args(["--vlogs", "http://h:1", "--log", "/x.log", "--from-start"])
        self.assertEqual((a.vlogs, a.log, a.from_start), ("http://h:1", "/x.log", True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
