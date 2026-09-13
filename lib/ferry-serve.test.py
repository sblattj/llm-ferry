#!/usr/bin/env python3
"""Stdlib unittest for the ferry-serve launch/log lifecycle.

Run:  python3 lib/ferry-serve.test.py

Covers the restart bug that silently blanks the observability pipeline
(2026-08-26): `ferry up` truncated the proxy log with `>` while the previous
litellm was still shutting down. uvicorn's straggler writes ("Application
shutdown complete", "Finished server process") went out on an fd whose offset
was already ~92KB in, so they landed past the truncation point and punched a
SPARSE HOLE of NUL bytes that restored the file's size. The newly launched
process, holding its own offset-0 fd, then wrote underneath that hole and never
reached EOF — the log's mtime ticked on every request while its size never
moved, and ferry-log-shipper (which attaches at EOF) shipped nothing.

Two halves:
  * TestSparseHoleSemantics reproduces the OS-level behaviour directly with
    os.open/os.write/os.ftruncate, so the fix is pinned to why it works rather
    than to the shape of a shell line.
  * TestShippedLaunchLines asserts the generated `ferry` still carries the safe
    pattern, which is what a future edit would quietly revert.
"""
import os
import re
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

# The real incident's log sat at 91,956 bytes. Any offset far past what the new
# process writes reproduces it; keep the fixture small.
OFFSET = 4096
STRAGGLER = b"INFO:     Finished server process [94599]\n"
NEWLINE = b'INFO:     127.0.0.1:50670 - "POST /v1/chat/completions HTTP/1.1" 200 OK\n'


class TestSparseHoleSemantics(unittest.TestCase):
    """The filesystem behaviour the fix is built on, asserted directly."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "cloud-proxy-8090.log")

    def _dying_writer(self):
        """An fd positioned deep into the log, as a shutting-down uvicorn has."""
        fd = os.open(self.log, os.O_WRONLY | os.O_CREAT, 0o644)
        os.write(fd, b"\n" * OFFSET)
        return fd

    def test_truncate_under_a_live_fd_resurrects_the_size(self):
        # This asserts the BROKEN behaviour on purpose: if it ever stops holding,
        # the platform changed and _ferry_reset_log is no longer load-bearing.
        dying = self._dying_writer()
        os.truncate(self.log, 0)                     # the old `> "$cloud_log"`
        os.write(dying, STRAGGLER)                   # lands at OFFSET -> sparse hole
        os.close(dying)
        self.assertEqual(os.path.getsize(self.log), OFFSET + len(STRAGGLER))

    def test_the_new_process_then_writes_behind_eof_forever(self):
        dying = self._dying_writer()
        os.truncate(self.log, 0)
        os.write(dying, STRAGGLER)
        os.close(dying)
        eof = os.path.getsize(self.log)              # where a tailer would attach

        fresh = os.open(self.log, os.O_WRONLY | os.O_CREAT)   # the relaunched proxy
        for _ in range(5):
            os.write(fresh, NEWLINE)
        os.close(fresh)

        # Size never moves, so a shipper attached at `eof` never sees a byte of it.
        self.assertEqual(os.path.getsize(self.log), eof)

    def test_unlink_then_append_keeps_the_log_clean(self):
        # _ferry_reset_log's contract: unlink, so the straggler keeps its now
        # nameless inode and the launch below gets a brand-new one.
        dying = self._dying_writer()
        os.unlink(self.log)
        os.write(dying, STRAGGLER)                   # goes to the unlinked inode
        os.close(dying)

        fresh = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.write(fresh, NEWLINE)
        before = os.path.getsize(self.log)
        os.write(fresh, NEWLINE)
        os.close(fresh)

        self.assertEqual(before, len(NEWLINE))       # no hole, no straggler bytes
        self.assertEqual(os.path.getsize(self.log), 2 * len(NEWLINE))
        with open(self.log, "rb") as fh:
            self.assertNotIn(b"\x00", fh.read())

    def test_append_mode_survives_a_second_racing_writer(self):
        # O_APPEND is why the launch line uses `>>`: two writers on the same log
        # (a straggler that reopened, a concurrent `ferry up`) interleave whole
        # lines instead of overwriting each other.
        a = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        b = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.write(a, NEWLINE)
        os.write(b, STRAGGLER)
        os.write(a, NEWLINE)
        os.close(a); os.close(b)
        self.assertEqual(os.path.getsize(self.log), 2 * len(NEWLINE) + len(STRAGGLER))


class TestShippedLaunchLines(unittest.TestCase):
    """`ferry` is generated from lib/; these guard the pattern against a revert."""

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as fh:
            cls.src = fh.read()

    def test_no_litellm_launch_truncates_its_log(self):
        self.assertEqual(self.src.count('host 0.0.0.0 > "$cloud_log"'), 0)

    def test_every_litellm_launch_appends(self):
        # stack + route + cloud + the cmd_reload fallback launch.
        self.assertEqual(self.src.count('host 0.0.0.0 >> "$cloud_log"'), 4)

    def test_every_litellm_launch_resets_its_log_first(self):
        # Only the cold-start modes reset the log; cmd_reload deliberately
        # appends so a config-change reload does not wipe the log that records
        # what the previous config was doing.
        self.assertEqual(self.src.count('_ferry_reset_log "$cloud_log"'), 3)

    def test_the_mlx_launch_resets_and_appends(self):
        self.assertEqual(self.src.count('_ferry_reset_log "$log"'), 1)
        self.assertEqual(self.src.count('nohup mlx_vlm.server "${mlargs[@]}" >> "$log"'), 1)

    def test_reset_log_unlinks_rather_than_truncating(self):
        body = re.search(r"_ferry_reset_log\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body, "_ferry_reset_log is missing from ferry")
        self.assertIn("rm -f", body.group(1))

    def test_stop_litellm_waits_for_exit(self):
        body = re.search(r"_ferry_stop_litellm\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body, "_ferry_stop_litellm is missing from ferry")
        text = body.group(1)
        self.assertIn("pkill -f", text)
        self.assertIn("pgrep -f", text)       # confirms the exit, not just signals it
        self.assertIn("while", text)          # polls for exit, never a bare sleep
        self.assertIn("pkill -9 -f", text)    # escalates rather than hanging forever

    def test_stop_litellm_is_scoped_to_a_port_when_given_one(self):
        # `ferry up --port 8099` must not reap a lane serving :8090.
        body = re.search(r"_ferry_stop_litellm\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIn("--port ${port}", body.group(1))

    def test_cmd_up_reaps_only_its_target_port(self):
        self.assertIn('_ferry_stop_litellm "$target_port"', self.src)

    def test_cmd_down_reaps_every_proxy(self):
        self.assertIn("\n  _ferry_stop_litellm\n", self.src)

    def test_free_port_polls_instead_of_a_fixed_sleep(self):
        body = re.search(r"_ferry_free_port\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body)
        self.assertIn("while", body.group(1))

    def test_launch_front_passes_workers(self):
        body = re.search(r"_ferry_launch_front\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body, "_ferry_launch_front is missing from ferry")
        self.assertIn('--workers "$workers"', body.group(1))
        # 4th arg overrides, else the FERRY_WORKERS-derived default
        self.assertIn('${4:-$FERRY_FRONT_WORKERS}', body.group(1))

    def test_workers_default_is_a_small_pool_overridable_by_env(self):
        # litellm's benchmark guidance is one worker per CPU; a LAN host needs
        # only a small pool. 4 is the shipped default, FERRY_WORKERS overrides
        # (1 restores the old single-process shape).
        self.assertIn('FERRY_FRONT_WORKERS="${FERRY_WORKERS:-4}"', self.src)

    def test_port_precedes_workers_on_the_launch_line(self):
        # _ferry_stop_litellm reaps by `(litellm|ferry_front\.py) .*--port N( |$)`.
        # --workers must come AFTER --port so the space in that pattern still
        # matches the longer cmdline; if someone reorders the flags the reap
        # silently misses the master and `ferry up` fights a zombie front.
        body = re.search(r"_ferry_launch_front\(\) \{(.*?)\n\}", self.src, re.S)
        text = body.group(1)
        self.assertIn("--port", text)
        self.assertIn("--workers", text)
        self.assertLess(text.index("--port"), text.index("--workers"))

    def test_plain_litellm_fallback_keeps_worker_parity(self):
        # If the catalogue front is unavailable and ferry falls back to the raw
        # `litellm` CLI, it must launch the SAME worker count, not silently
        # drop to one process (the CLI flag is --num_workers, not --workers).
        # stack + route + cmd_reload's fallback all keep worker parity.
        self.assertEqual(self.src.count('--num_workers "$FERRY_FRONT_WORKERS"'), 3)

    # ---- the injected Codex prompt ---------------------------------------
    # litellm's ChatGPT provider prepends its own "you are Codex in the Codex
    # CLI" instructions ahead of the client's system prompt. ferry_front.py
    # exports CHATGPT_DEFAULT_INSTRUCTIONS over it; these plain `litellm` CLI
    # launches never load that module, so the shell must mirror it or the
    # fallback path quietly serves the Codex prompt again.

    def _litellm_launch_blocks(self):
        """(line no, executable lines since the enclosing block opened) per launch.

        Comment lines are dropped: a substring scan that keeps them accepts a
        commented-out `# _ferry_export_chatgpt_instructions` as a real call,
        which is exactly how this guard would be disarmed by accident.
        """
        lines = self.src.splitlines()
        blocks = []
        for i, line in enumerate(lines):
            if "nohup litellm" not in line:
                continue
            j = i - 1
            while j >= 0:
                stripped = lines[j].strip()
                if stripped.startswith(("if ", "elif ", "else", "fi", "then")) \
                        or stripped.endswith("() {"):
                    break
                j -= 1
            body = [l for l in lines[j + 1:i] if not l.strip().startswith("#")]
            blocks.append((i + 1, body))
        return blocks

    def test_every_litellm_launch_exports_the_chatgpt_instructions(self):
        blocks = self._litellm_launch_blocks()
        # stack + cloud + route + schematron + the cmd_reload fallback.
        self.assertEqual(len(blocks), 5)
        for lineno, preceding in blocks:
            self.assertTrue(
                any("_ferry_export_chatgpt_instructions" in l for l in preceding),
                "the `nohup litellm` launch at ferry:%d is not preceded by "
                "_ferry_export_chatgpt_instructions inside its own block" % lineno)

    def test_chatgpt_instructions_helper_mirrors_the_python_resolver(self):
        body = re.search(
            r"_ferry_export_chatgpt_instructions\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(
            body, "_ferry_export_chatgpt_instructions is missing from ferry")
        text = body.group(1)
        self.assertIn("FERRY_CHATGPT_INSTRUCTIONS", text)      # operator override
        self.assertIn("CHATGPT_DEFAULT_INSTRUCTIONS", text)    # what litellm reads
        self.assertIn('"off"', text)                           # the opt-out sentinel
        self.assertIn("$HOME/.config/ferry/chatgpt-instructions.txt", text)
        self.assertIn("$APP_DIR/front/chatgpt-instructions.txt", text)
        # An empty export is not an override (litellm: `getenv(...) or DEFAULT`),
        # so a blank candidate must be skipped rather than exported.
        self.assertIn("[[:space:]]", text)

    def test_launch_front_does_not_preexport_the_instructions(self):
        # ferry_front.py resolves and LABELS its own source; a shell pre-export
        # would make every launch log claim "operator env" instead.
        body = re.search(r"_ferry_launch_front\(\) \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(body, "_ferry_launch_front is missing from ferry")
        self.assertNotIn("_ferry_export_chatgpt_instructions", body.group(1))

    def test_the_zsh_mirror_behaves_like_the_python_resolver(self):
        """Run the mirror, don't just read it.

        Every assertion above is a substring scan over the generated `ferry`,
        and text cannot see behaviour: dropping the `export` keyword, or
        swapping the user file and the shipped file in the candidate list, keeps
        this whole class green while the `nohup litellm` children stop
        inheriting the override and litellm serves its Codex prompt again.
        lib/ferry-chatgpt-instructions.test.zsh sources the function out of the
        BUILT ferry and asserts what a CHILD PROCESS sees on every branch.
        """
        script = os.path.join(REPO, "lib", "ferry-chatgpt-instructions.test.zsh")
        self.assertTrue(os.path.isfile(script), "missing harness: %s" % script)
        proc = subprocess.run(["zsh", script], capture_output=True, text=True)
        self.assertEqual(
            proc.returncode, 0,
            "the zsh mirror harness failed:\n%s\n%s" % (proc.stdout, proc.stderr))
        self.assertNotIn("FAIL", proc.stdout)
        self.assertIn("ZSH CHATGPT-INSTRUCTIONS MIRROR: all checks passed",
                      proc.stdout)
        # A harness that quietly stopped asserting would also exit 0, so pin the
        # branch count: a/a2 x3+1, b/b2/b3, c/c2/c3, d/d2/d3, e/e2, f.
        self.assertGreaterEqual(proc.stdout.count("PASS "), 16, proc.stdout)


class TestRouteTemplateMediumLane(unittest.TestCase):
    """The shipped route template keeps the substantive worker wired safely."""

    @classmethod
    def setUpClass(cls):
        import yaml

        path = os.path.join(REPO, "litellm-route-example.yaml")
        with open(path) as handle:
            cls.template_text = handle.read()
        cls.cfg = yaml.safe_load(cls.template_text)
        cls.deployments = {m["model_name"]: m for m in cls.cfg["model_list"]}

    def test_model_info_ids_are_present_and_unique(self):
        ids = [m.get("model_info", {}).get("id") for m in self.cfg["model_list"]]
        self.assertNotIn(None, ids, "every deployment needs a stable model_info.id")
        self.assertEqual(len(ids), len(set(ids)), "template model_info.id values must be unique")

    def test_medium_primary_and_hop_have_the_expected_visibility_and_routing(self):
        primary = self.deployments["domestic.medium"]
        self.assertEqual(
            primary["litellm_params"],
            {"model": "chatgpt/responses/gpt-5.6-terra",
             "api_key": "chatgpt-oauth", "reasoning_effort": "xhigh",
             "timeout": 600},
        )
        self.assertIs(primary["model_info"].get("public"), True)

        hop = self.deployments["domestic.medium-terra"]
        self.assertNotIn("public", hop["model_info"], "fallback hops must stay private")
        self.assertEqual(hop["litellm_params"]["model"], "openrouter/openai/gpt-5.6-terra")
        self.assertEqual(hop["litellm_params"]["timeout"], 600)
        self.assertEqual(hop["litellm_params"]["stream_timeout"], 60)
        self.assertEqual(
            hop["litellm_params"]["extra_body"],
            {"provider": {"sort": "throughput"}, "reasoning": {"effort": "xhigh"}},
        )

        fallbacks = {
            name: hops for entry in self.cfg["router_settings"]["fallbacks"]
            for name, hops in entry.items()
        }
        self.assertEqual(fallbacks["domestic.medium"], ["domestic.medium-terra"])

    def test_public_super_flash_is_gemini_only_without_a_model_fallback(self):
        """Compaction/title/summary stay on Gemini when the lane has an error."""
        primary = self.deployments["domestic.super-flash"]
        self.assertEqual(
            primary["litellm_params"],
            {
                "model": "openrouter/~google/gemini-flash-latest",
                "api_key": "os.environ/OPENROUTER_API_KEY",
                "timeout": 600,
                "stream_timeout": 60,
                "extra_body": {
                    "provider": {"sort": "throughput"},
                    "reasoning": {"effort": "minimal"},
                },
            },
        )
        self.assertIs(primary["model_info"].get("public"), True)

        fallbacks = {
            name: hops for entry in self.cfg["router_settings"]["fallbacks"]
            for name, hops in entry.items()
        }
        self.assertEqual(fallbacks["domestic.super-flash"], [])
        self.assertNotIn("domestic.super-flash-luna", self.deployments)
        self.assertNotIn("domestic.super-flash-luna", self.template_text)
        self.assertNotIn("international.super-flash", self.deployments)


class TestSchematronDoor(unittest.TestCase):
    """v1.35.0: `ferry up --schematron` serves ONLY the extraction lane on its
    own port, so a scraper workload runs beside the main stack. The invariants
    that keep it a true companion door, not a second main door:

      * its own conventional port, env-overridable, that no other ferry
        service claims;
      * it must NOT recycle the main door's port, and must refuse (not kill -9)
        a foreign holder of its own port;
      * the served config is the route config FILTERED to the schematron
        deployment — never the whole file, never a hand-sliced text file.
    """

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as fh:
            cls.src = fh.read()

    def test_port_default_and_env_override(self):
        self.assertIn('SCHEMATRON_PORT="${FERRY_SCHEMATRON_PORT:-8094}"', self.src)
        # 8094 must be claimed by nothing else: the only other mentions of the
        # literal are the schematron declaration's own comments.
        for line in self.src.splitlines():
            if "8094" in line and "SCHEMATRON_PORT" not in line:
                self.assertIn("#", line.strip()[:1],
                              "8094 is claimed outside the SCHEMATRON_PORT block: %r" % line)

    def test_relay_reserves_the_schematron_port(self):
        # A client exposure must never shadow the extraction door.
        self.assertIn("$SCHEMATRON_PORT,$relay_port", self.src)

    def test_flag_parses_to_a_mode_with_its_own_default_port(self):
        # The case body opens with a rationale comment, so assert the pieces
        # rather than a regex across the comment block.
        self.assertIn("--schematron)", self.src)
        self.assertIn('LAUNCH_MODE="schematron"', self.src)
        self.assertIn('(( port_given )) || target_port="$SCHEMATRON_PORT"', self.src)
        # -p before OR after --schematron must both win over the default.
        self.assertIn('target_port="$2"\n          port_given=1', self.src)

    def test_schematron_refuses_a_foreign_port_holder_instead_of_reaping(self):
        # The main-door modes _ferry_free_port (kill -9) their target; the
        # schematron branch must take the other path — refuse with a message.
        m = re.search(
            r'if \[\[ "\$LAUNCH_MODE" == "schematron" \]\]; then(.*?)\n  else\n'
            r'\s*_ferry_free_port "\$target_port"',
            self.src, re.S)
        self.assertIsNotNone(m, "schematron mode must not share the free_port reap")
        self.assertIn("lsof", m.group(1))
        self.assertIn("refuses", m.group(1))
        self.assertIn("exit 1", m.group(1))

    def test_the_filtered_config_is_regenerated_deterministically(self):
        self.assertIn(
            'schematron_config="$HOME/.config/ferry/litellm-schematron.yaml"',
            self.src)

    def test_wait_uses_the_public_liveliness_route(self):
        self.assertIn(
            '_ferry_wait_http "http://127.0.0.1:$target_port/health/liveliness"'
            ' "schematron" 120 readiness', self.src)

    def test_log_is_port_accurate_and_reset_first(self):
        self.assertIn('schematron_log="$LOG_DIR/schematron-$target_port.log"', self.src)
        self.assertIn('_ferry_reset_log "$schematron_log"', self.src)
        self.assertIn('>> "$schematron_log" 2>&1 & disown', self.src)
        self.assertEqual(self.src.count('host 0.0.0.0 > "$schematron_log"'), 0)

    def test_banner_names_the_lane_the_endpoint_and_the_cdp_hook(self):
        self.assertIn("FERRY SCHEMATRON — extraction lane", self.src)
        self.assertIn("CDP_EXTRACT_BASE_URL=http://127.0.0.1:$target_port/v1", self.src)

    def test_down_takes_a_port_to_stop_one_door_only(self):
        m = re.search(r"cmd_down\(\) \{(.*?)\n  while \[\[ \$# -gt 0 \]\]", self.src, re.S)
        self.assertIsNotNone(m, "cmd_down no longer parses --port")
        self.assertIn('local stop_port=""', m.group(1))
        self.assertIn('stop_port="$2"', self.src)
        self.assertIn('_ferry_stop_litellm "$stop_port"', self.src)

    def test_the_dispatcher_forwards_flags_to_cmd_down(self):
        # `ferry down --port 8094` once hit a dispatch line that dropped "$@",
        # so the scoped stop silently ran as a FULL down and took :8090 with
        # it. Text assertions inside cmd_down cannot see that; this one pins
        # the dispatcher itself.
        self.assertRegex(self.src, r"down\)\s+cmd_down \"\$@\" ;;")


class TestSchematronFilter(unittest.TestCase):
    """Run the REAL shipped extractor against the REAL shipped template.

    Textual assertions over cmd_up cannot see whether the filter keeps the
    lane servable: these execute the embedded SCHEMFILTER_EOF python (extracted
    from the built `ferry`) and load its output with PyYAML, asserting the
    exact shape the schematron door serves.
    """

    @classmethod
    def setUpClass(cls):
        import yaml

        with open(FERRY) as fh:
            cls.src = fh.read()
        m = re.search(r"<<'SCHEMFILTER_EOF'\n(.*?)\nSCHEMFILTER_EOF", cls.src, re.S)
        assert m is not None, "the schematron filter heredoc is missing from ferry"
        cls.script = m.group(1)

        cls.dst = tempfile.mkdtemp() + "/litellm-schematron.yaml"
        cls.template = os.path.join(REPO, "litellm-route-example.yaml")
        cls.proc = subprocess.run(
            ["python3", "-c", cls.script, cls.template, cls.dst, "schematron"],
            capture_output=True, text=True, timeout=60)
        assert cls.proc.returncode == 0, cls.proc.stderr
        with open(cls.dst) as fh:
            cls.filtered = yaml.safe_load(fh)

    def test_stdout_is_the_upstream_model_for_the_banner(self):
        # v1.36.0: the primary is the LOCAL MLX backend, so the banner's
        # upstream is the HuggingFace id mlx_vlm preloaded, not an OpenRouter
        # model string. The filter is unchanged — it prints deployments[0] —
        # but what deployments[0] IS has moved on-machine.
        self.assertEqual(self.proc.stdout.strip(),
                         "openai/pchamart/schematron8B-mlx-8bit")

    def test_model_list_is_exactly_the_schematron_deployment(self):
        self.assertEqual(len(self.filtered["model_list"]), 1)
        dep = self.filtered["model_list"][0]
        self.assertEqual(dep["model_name"], "schematron")
        self.assertEqual(dep["litellm_params"]["model"],
                         "openai/pchamart/schematron8B-mlx-8bit")
        self.assertEqual(dep["litellm_params"]["api_base"],
                         "http://127.0.0.1:8100/v1")
        self.assertEqual(dep["litellm_params"]["temperature"], 0)
        self.assertIs(dep["model_info"]["public"], True)

    def test_the_cloud_sibling_does_not_ride_along(self):
        """`schematron-cloud` is a DIFFERENT lane, and this door serves one.

        The filter matches model_name EXACTLY, so the sibling is excluded — but
        a substring-matching regression would quietly make the cloud extractor
        callable through a door whose whole point is that it is local.
        """
        names = [m["model_name"] for m in self.filtered["model_list"]]
        self.assertNotIn("schematron-cloud", names)
        self.assertNotIn("openrouter/", open(self.dst).read())

    def test_no_other_lane_or_hop_leaks_into_the_door(self):
        text = open(self.dst).read()
        for foreign in ("domestic.heavy", "local-orch", "local-sub",
                        "super-flash", "gemini", "chatgpt/"):
            self.assertNotIn(foreign, text,
                             "%r leaked into the filtered config" % foreign)

    def test_master_key_survives_the_filter(self):
        # Without it this door would be the one unauthenticated listener on
        # 0.0.0.0 while the main door stays keyed.
        self.assertEqual(self.filtered["general_settings"]["master_key"],
                         "os.environ/LITELLM_MASTER_KEY")

    def test_litellm_settings_travel_untouched(self):
        for key in ("drop_params", "num_retries", "request_timeout", "callbacks"):
            self.assertIn(key, self.filtered["litellm_settings"])
        self.assertIs(self.filtered["litellm_settings"]["drop_params"], True)

    def test_fallbacks_trim_to_the_schematron_entry_only(self):
        self.assertEqual(self.filtered["router_settings"]["fallbacks"],
                         [{"schematron": []}])

    def test_a_config_without_the_lane_errors_rather_than_serving_empty(self):
        empty = tempfile.mkdtemp() + "/no-lane.yaml"
        with open(empty, "w") as fh:
            fh.write("model_list:\n  - model_name: flash\n"
                     "    litellm_params: {model: openrouter/x, "
                     "api_key: os.environ/K}\n")
        dst2 = empty + ".out"
        r = subprocess.run(
            ["python3", "-c", self.script, empty, dst2, "schematron"],
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no 'schematron' deployment", r.stderr)
        self.assertFalse(os.path.exists(dst2), "a failed filter must not write a config")


class TestLocalSchematronLane(unittest.TestCase):
    """v1.36.0: the `schematron` lane runs ON THE HOST GPU as a third MLX lane.

    Everything here reads the GENERATED `ferry`, because that single file is
    what actually ships — an edit to lib/ferry-*.zsh that is never rebuilt is
    exactly the regression these assertions exist to catch.

    The invariants:

      * the lane has its own model, log, governor and port, and the port is
        claimed by nothing else;
      * plain `ferry up` launches THREE mlx lanes, not two, and waits on all
        three;
      * `ferry up --schematron` now has a backend to start, and REUSES a warm
        one rather than reaping a lane the main stack may be serving through;
      * `ferry down` (both forms) and `ferry status` know about the new port.
    """

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as fh:
            cls.src = fh.read()

    # ---- constants -------------------------------------------------------
    def test_lane_constants_exist(self):
        self.assertIn('LOCAL_MODEL_SCHEMATRON="pchamart/schematron8B-mlx-8bit"',
                      self.src)
        self.assertIn('LOCAL_DRAFT_SCHEMATRON=""', self.src)
        self.assertIn('LOCAL_SCHEMATRON_LOG="$LOG_DIR/local-schematron-'
                      '$LOCAL_SCHEMATRON_PORT.log"', self.src)

    def test_kv_governor_is_unquantized_at_full_context(self):
        """bf16 KV is a deliberate choice, not an omission.

        8 kv heads x 128 head dim x 32 layers x 2 x 2 bytes = 131 KB/token, so
        the full 128k window is affordable; quantizing would cost fidelity on a
        lane whose job is verbatim copying. An empty KV_BITS drops the flag
        entirely (see _ferry_launch_mlx), which is the point.
        """
        self.assertIn('LOCAL_SCHEMATRON_KV_BITS=""', self.src)
        self.assertIn('LOCAL_SCHEMATRON_MAX_KV="${LOCAL_SCHEMATRON_MAX_KV:-131072}"',
                      self.src)
        # Deep prefill, shallow decode: concurrency grows KV faster than
        # throughput, so this lane admits fewer sequences than the others.
        self.assertIn('LOCAL_SCHEMATRON_MAX_SEQS="${LOCAL_SCHEMATRON_MAX_SEQS:-2}"',
                      self.src)
        self.assertIn('LOCAL_SCHEMATRON_APC_BLOCKS="${LOCAL_SCHEMATRON_APC_BLOCKS:'
                      '-$LOCAL_APC_BLOCKS}"', self.src)

    def test_port_default_and_env_override(self):
        self.assertIn(
            'LOCAL_SCHEMATRON_PORT="${FERRY_LOCAL_SCHEMATRON_PORT:-8100}"', self.src)
        # 8100 must be claimed by nothing else in the shipped script: the only
        # other mentions of the literal are comments.
        for line in self.src.splitlines():
            if "8100" in line and "LOCAL_SCHEMATRON_PORT" not in line:
                self.assertEqual(line.strip()[:1], "#",
                                 "8100 is claimed outside the lane's own "
                                 "declaration: %r" % line)

    def test_relay_reserves_the_lane_port(self):
        # A client exposure must never shadow an internal inference backend.
        # Asserted in its grouping with the other two MLX lane ports, which is
        # what it is: internal plumbing, not a door.
        self.assertIn("$LOCAL_ORCH_PORT,$LOCAL_SUB_PORT,$LOCAL_SCHEMATRON_PORT",
                      self.src)

    # ---- the stack -------------------------------------------------------
    def _launch_mlx_labels(self, block):
        return re.findall(r'_ferry_launch_mlx "([\w-]+)"', block)

    def _stack_block(self):
        m = re.search(r'if \[\[ "\$LAUNCH_MODE" == "stack" \]\]; then(.*?)'
                      r'\n  elif \[\[ "\$LAUNCH_MODE" == "local-orch"',
                      self.src, re.S)
        self.assertIsNotNone(m, "the stack branch of cmd_up is missing")
        return m.group(1)

    def test_stack_launches_three_mlx_lanes(self):
        labels = self._launch_mlx_labels(self._stack_block())
        self.assertEqual(labels, ["local-orch", "local-sub", "local-schematron"],
                         "the stack must launch all three MLX lanes, in order")

    def test_stack_passes_the_lane_its_own_port_log_and_governor(self):
        block = self._stack_block()
        m = re.search(r'_ferry_launch_mlx "local-schematron" (.*?)\n\n', block, re.S)
        self.assertIsNotNone(m, "the local-schematron launch line is missing")
        call = m.group(1)
        for arg in ('"$LOCAL_MODEL_SCHEMATRON"', '"$LOCAL_DRAFT_SCHEMATRON"',
                    '"$LOCAL_SCHEMATRON_PORT"', '"$LOCAL_SCHEMATRON_LOG"',
                    '"$LOCAL_SCHEMATRON_KV_BITS"', '"$LOCAL_SCHEMATRON_MAX_KV"',
                    '"$LOCAL_SCHEMATRON_MAX_SEQS"', '"$LOCAL_SCHEMATRON_APC_BLOCKS"'):
            self.assertIn(arg, call,
                          "the stack launch drops %s — a lane launched with "
                          "another lane's governor is the silent failure" % arg)

    def test_stack_frees_the_port_before_launching(self):
        self.assertIn('_ferry_free_port "$LOCAL_SCHEMATRON_PORT"',
                      self._stack_block())

    def test_stack_waits_for_the_lane_to_be_warm(self):
        """A 200 on /v1/models IS the weights-resident signal for an MLX lane.

        900s, like the other two: a cold 8.5GB load out of the HF cache
        legitimately takes tens of seconds, and more when three lanes stream
        concurrently.
        """
        block = self._stack_block()
        self.assertIn(
            '_ferry_wait_http "http://127.0.0.1:$LOCAL_SCHEMATRON_PORT/v1/models"',
            block)
        m = re.search(r'\$LOCAL_SCHEMATRON_PORT/v1/models" "local-schematron" (\d+)',
                      block)
        self.assertIsNotNone(m, "the lane's readiness wait has no timeout")
        self.assertEqual(m.group(1), "900")

    # ---- the --schematron door ------------------------------------------
    def _door_block(self):
        m = re.search(r'elif \[\[ "\$LAUNCH_MODE" == "schematron" \]\]; then(.*?)'
                      r'\n  fi\n\}', self.src, re.S)
        self.assertIsNotNone(m, "the schematron door branch is missing")
        return m.group(1)

    def test_the_door_launches_the_lane_behind_its_litellm_front(self):
        block = self._door_block()
        self.assertIn('_ferry_launch_mlx "local-schematron"', block,
                      "the door fronts a LOCAL backend now — it must start one")
        self.assertIn('"$LOCAL_SCHEMATRON_PORT" "$LOCAL_SCHEMATRON_LOG"', block)

    def test_the_door_reuses_a_warm_lane_instead_of_reaping_it(self):
        """The main stack wires the same deployment to the same port.

        So a door that killed :$LOCAL_SCHEMATRON_PORT to claim it would take
        extraction out from under a running stack. It must probe first and
        launch only on a cold port — and it must never _ferry_free_port it.
        """
        block = self._door_block()
        self.assertIn("$LOCAL_SCHEMATRON_PORT/v1/models", block)
        self.assertIn("schem_lane_warm=1", block)
        self.assertNotIn('_ferry_free_port "$LOCAL_SCHEMATRON_PORT"', block,
                         "the companion door must never reap the shared lane")

    def test_the_door_waits_for_the_lane(self):
        block = self._door_block()
        m = re.search(r'\$LOCAL_SCHEMATRON_PORT/v1/models" "local-schematron" (\d+)',
                      block)
        self.assertIsNotNone(m, "the door does not wait for its own backend")
        self.assertEqual(m.group(1), "900")

    def test_the_door_still_refuses_a_foreign_holder_of_its_litellm_port(self):
        """v1.35.0 behaviour, unchanged: the DOOR's port is refused, not reaped.

        Adding a backend must not have quietly turned the companion door into
        a main door that kill -9s whatever holds :8094.
        """
        m = re.search(
            r'if \[\[ "\$LAUNCH_MODE" == "schematron" \]\]; then(.*?)\n  else\n',
            self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("refuses to", m.group(1))
        # Comments must be stripped first: this block's whole rationale comment
        # NAMES _ferry_free_port as the thing it is declining to do, so a raw
        # substring scan can never fail and would prove nothing.
        code = "\n".join(l for l in m.group(1).splitlines()
                         if not l.strip().startswith("#"))
        self.assertNotIn("_ferry_free_port", code)

    def test_the_door_is_gated_on_apple_silicon_now_that_it_is_local(self):
        m = re.search(r'if \[\[ "\$LAUNCH_MODE" == "stack" \|\| '
                      r'"\$LAUNCH_MODE" == local-\* (.*?)\]\]; then', self.src)
        self.assertIsNotNone(m, "the GPU prerequisite guard is missing")
        self.assertIn('"$LAUNCH_MODE" == "schematron"', m.group(1),
                      "the door's backend is MLX, so it needs the MLX guard")

    # ---- the solo mode ---------------------------------------------------
    def test_solo_flag_parses_to_its_own_mode(self):
        self.assertIn("--local-schematron)", self.src)
        self.assertIn('LAUNCH_MODE="local-schematron"', self.src)

    def test_solo_mode_serves_the_lane_on_the_target_port(self):
        m = re.search(r'elif \[\[ "\$LAUNCH_MODE" == "local-schematron" \]\]; '
                      r'then(.*?)\n  elif ', self.src, re.S)
        self.assertIsNotNone(m, "the --local-schematron branch is missing")
        block = m.group(1)
        self.assertIn('_ferry_launch_mlx "local-schematron" '
                      '"$LOCAL_MODEL_SCHEMATRON"', block)
        self.assertIn('"$target_port" "$LOCAL_LOG"', block)
        self.assertIn('"http://127.0.0.1:$target_port/v1/models" '
                      '"local-schematron" 900', block)

    def test_the_interactive_catalog_offers_the_lane(self):
        self.assertIn('options.append(("local-schematron"', self.src)
        self.assertIn('"$chosen_model" == "local-schematron"', self.src)

    # ---- down ------------------------------------------------------------
    def test_down_frees_the_lane_port(self):
        self.assertIn('for _p in "$LOCAL_ORCH_PORT" "$LOCAL_SUB_PORT" '
                      '"$LOCAL_SCHEMATRON_PORT"; do', self.src)

    def test_down_port_stops_the_backend_only_when_the_stack_is_gone(self):
        """`ferry down --port 8094` owns the lane only if nothing else does.

        With the main stack up, :$PORT is serving `schematron` through the very
        same backend, so the companion door is the borrower and must leave it
        alone. With no stack, the door owns it and must not strand ~8.5GB of
        weights.
        """
        m = re.search(r'if \[\[ -n "\$stop_port" \]\]; then(.*?)\n    return 0',
                      self.src, re.S)
        self.assertIsNotNone(m, "the `down --port` branch is missing")
        block = m.group(1)
        self.assertIn('"$stop_port" == "$SCHEMATRON_PORT"', block)
        # The stack check must come FIRST — the guard is what makes it safe.
        guard = block.index('lsof -nP -iTCP:"$PORT" -sTCP:LISTEN')
        reap = block.index('_ferry_free_port "$LOCAL_SCHEMATRON_PORT"')
        self.assertLess(guard, reap,
                        "the lane is reaped without first checking for a "
                        "running main stack")

    # ---- status ----------------------------------------------------------
    def test_status_lists_and_labels_the_lane_port(self):
        self.assertIn('for p in "$PORT" "$LOCAL_ORCH_PORT" "$LOCAL_SUB_PORT" '
                      '"$LOCAL_SCHEMATRON_PORT" "$SHARE_PORT"; do', self.src)
        self.assertIn('_label="local-schematron lane (internal)"', self.src)

    def test_status_treats_it_as_an_mlx_backend_not_a_front_door(self):
        """Both MLX-only branches must include it.

        One reports phys_footprint (RSS is blind to wired GPU memory); the
        other reads --model from argv instead of printing /v1/models, which on
        an MLX lane lists the whole HF cache rather than what is loaded. Miss
        either and the new lane is rendered as if it were the endpoint.
        """
        cond = ('[[ "$p" == "$LOCAL_ORCH_PORT" || "$p" == "$LOCAL_SUB_PORT" '
                '|| "$p" == "$LOCAL_SCHEMATRON_PORT" ]]')
        self.assertEqual(self.src.count(cond), 2)


class TestRouteExampleSchematronSplit(unittest.TestCase):
    """The shipped template pins the local/cloud split, loaded as real YAML.

    A textual assertion cannot see whether the two deployments are actually
    two lanes, nor whether a fallback quietly bridges them — and a bridge is
    precisely the failure this design exists to prevent.
    """

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.path = os.path.join(REPO, "litellm-route-example.yaml")
        with open(cls.path) as fh:
            cls.cfg = yaml.safe_load(fh)
        cls.by_name = {}
        for dep in cls.cfg["model_list"]:
            cls.by_name.setdefault(dep["model_name"], []).append(dep)

    def test_schematron_is_the_local_lane_on_the_loopback_port(self):
        deps = self.by_name["schematron"]
        self.assertEqual(len(deps), 1, "the local lane takes no fallback hops")
        params = deps[0]["litellm_params"]
        self.assertEqual(params["model"], "openai/pchamart/schematron8B-mlx-8bit")
        # The backend is ferry's own MLX lane: loopback only, never the LAN.
        self.assertEqual(params["api_base"], "http://127.0.0.1:8100/v1")
        self.assertEqual(params["api_key"], "local")
        self.assertEqual(params["temperature"], 0)
        self.assertEqual(params["timeout"], 600)
        self.assertIs(deps[0]["model_info"]["public"], True)
        self.assertEqual(deps[0]["model_info"]["id"], "local-schematron-mlx")

    def test_the_api_base_port_matches_the_lane_ferry_actually_launches(self):
        """The config can only FRONT a backend; it cannot conjure one.

        If these two numbers drift, every extraction call 500s against a port
        nobody serves — and the config looks perfectly correct while it does.
        """
        with open(FERRY) as fh:
            ferry_src = fh.read()
        m = re.search(r'LOCAL_SCHEMATRON_PORT="\$\{FERRY_LOCAL_SCHEMATRON_PORT:'
                      r'-(\d+)\}"', ferry_src)
        self.assertIsNotNone(m, "ferry declares no LOCAL_SCHEMATRON_PORT")
        self.assertEqual(
            self.by_name["schematron"][0]["litellm_params"]["api_base"],
            "http://127.0.0.1:%s/v1" % m.group(1))

    def test_the_model_string_matches_the_one_mlx_preloads(self):
        """The string after `openai/` is sent to the backend VERBATIM.

        It must be the exact HuggingFace id mlx_vlm loaded, or the call misses
        the warm model and triggers a fresh load — or a download.
        """
        with open(FERRY) as fh:
            ferry_src = fh.read()
        m = re.search(r'LOCAL_MODEL_SCHEMATRON="([^"]+)"', ferry_src)
        self.assertIsNotNone(m, "ferry declares no LOCAL_MODEL_SCHEMATRON")
        self.assertEqual(
            self.by_name["schematron"][0]["litellm_params"]["model"],
            "openai/%s" % m.group(1))

    def test_schematron_cloud_keeps_the_openrouter_deployment(self):
        deps = self.by_name["schematron-cloud"]
        self.assertEqual(len(deps), 1)
        params = deps[0]["litellm_params"]
        self.assertEqual(params["model"],
                         "openrouter/inference-net/schematron-v2-turbo")
        self.assertEqual(params["temperature"], 0)
        self.assertNotIn("api_base", params, "the cloud lane is not loopback")
        self.assertIs(deps[0]["model_info"]["public"], True,
                      "it is a lane you ask for by name, not a hidden hop")
        self.assertEqual(deps[0]["model_info"]["id"], "or-schematron-v2-turbo")

    def test_nothing_falls_back_from_local_to_cloud(self):
        """The load-bearing assertion of this whole release.

        Two reasons it must hold: a local lane that silently ships the page
        off-box defeats the point of asking for it, and the two models extract
        differently — so a schema-pinned consumer must never be swapped from
        one onto the other mid-flight.
        """
        fallbacks = self.cfg["router_settings"]["fallbacks"]
        entries = {k: v for e in fallbacks for k, v in e.items()}
        self.assertIn("schematron", entries)
        self.assertEqual(entries["schematron"], [],
                         "`schematron` must have NO fallback hops at all")
        self.assertIn("schematron-cloud", entries)
        self.assertEqual(entries["schematron-cloud"], [])
        # Belt and braces: no OTHER lane may route to the cloud extractor
        # either, which is how a bridge would sneak back in.
        for lane, hops in entries.items():
            self.assertNotIn("schematron-cloud", hops or [],
                             "%r falls back to the cloud extractor" % lane)
            if lane != "schematron":
                self.assertNotIn("schematron", hops or [])


class TestStatusTestCommand(unittest.TestCase):
    """The curl line `ferry status` prints must work when pasted."""

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as f:
            cls.src = f.read()

    def test_suggested_max_tokens_clears_the_reasoning_floor(self):
        # Every lane on this endpoint is a REASONING model, and reasoning tokens
        # come out of the same budget as the answer. At max_tokens 10 the whole
        # allowance is spent thinking: the reply is finish_reason=length with
        # content=null, so the one command status hands you to prove the stack is
        # alive reads as a dead lane. Measured live on the orch lane 2026-08-26
        # (reasoning_tokens 7, text_tokens 3, content None).
        m = re.search(r'Test with: curl.*?max_tokens\\":(\d+)', self.src)
        self.assertIsNotNone(m, "no suggested test command found in cmd_status")
        self.assertGreaterEqual(
            int(m.group(1)), 64,
            "suggested max_tokens is below the reasoning floor; the command it "
            "prints returns content=null on a healthy lane")


class TestMasterKeyProbes(unittest.TestCase):
    """v1.22.0: the front door can sit behind general_settings.master_key.

    Every probe that answers "is it up" must survive a 401, and every probe
    that reads catalogue content must present LITELLM_MASTER_KEY when the host
    has one — while a keyless LAN install (no template edit) probes exactly as
    it did before, so the bearer is conditional everywhere.
    """

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as f:
            cls.src = f.read()

    def wait_http_body(self):
        m = re.search(r"_ferry_wait_http\(\) \{.*?\n\}", self.src, re.S)
        self.assertIsNotNone(m, "_ferry_wait_http is missing from ferry")
        return m.group(0)

    def run_wait_http(self, mode, server_status, require_bearer=None,
                      env_key=None, timeout="3"):
        """Execute the REAL extracted function against a throwaway HTTP server."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        hit = {"auth": None}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                hit["auth"] = self.headers.get("Authorization")
                ok = require_bearer is None or hit["auth"] == f"Bearer {require_bearer}"
                self.send_response(server_status if ok else 401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}/probe"

        with tempfile.NamedTemporaryFile("w", suffix=".zsh", delete=False) as f:
            f.write("set -u\n" + self.wait_http_body() + "\n"
                    f"_ferry_wait_http '{url}' probe {timeout} {mode}\n"
                    "echo RC=$?\n")
            path = f.name
        env = dict(os.environ)
        env.pop("LITELLM_MASTER_KEY", None)   # hermetic: author shells may export one
        if env_key is not None:
            env["LITELLM_MASTER_KEY"] = env_key
        try:
            r = subprocess.run(["zsh", path], capture_output=True, text=True,
                               env=env, timeout=60)
        finally:
            os.unlink(path)
        self.assertIn("RC=", r.stdout, r.stderr)
        return int(r.stdout.strip().rsplit("RC=", 1)[1]), hit

    def test_a_200_is_ready_in_both_modes(self):
        rc, _ = self.run_wait_http("content", 200)
        self.assertEqual(rc, 0)
        rc, _ = self.run_wait_http("readiness", 200)
        self.assertEqual(rc, 0)

    def test_a_401_is_ready_only_in_readiness_mode(self):
        # THE v1.22.0 case: a master_key front door answers 401; readiness must
        # call that up. The content probe must still refuse it — a 401 on an
        # MLX lane's /v1/models would NOT mean warm.
        rc, _ = self.run_wait_http("readiness", 401)
        self.assertEqual(rc, 0)
        rc, _ = self.run_wait_http("content", 401)
        self.assertEqual(rc, 1)

    def test_the_bearer_is_sent_when_the_key_is_set(self):
        rc, hit = self.run_wait_http("readiness", 200, require_bearer="sk-test",
                                     env_key="sk-test")
        self.assertEqual(rc, 0)
        self.assertEqual(hit["auth"], "Bearer sk-test",
                         "LITELLM_MASTER_KEY was not presented on the probe")

    def test_keyless_probes_send_no_auth_header(self):
        # LAN installs without the template edit: no var, no header — byte for
        # byte the probes ferry sent before v1.22.0.
        rc, hit = self.run_wait_http("content", 200)
        self.assertEqual(rc, 0)
        self.assertIsNone(hit["auth"], "a keyless install must not send an auth header")

    def test_readiness_mode_accepts_401_even_with_a_wrong_key(self):
        # Readiness asks "is the process up"; a rejected key still proves it is.
        rc, _ = self.run_wait_http("readiness", 200, require_bearer="sk-right",
                                   env_key="sk-wrong")
        self.assertEqual(rc, 0)

    # --- structural: the call sites and their modes ---------------------------
    def test_front_wait_uses_the_public_liveliness_route_in_readiness_mode(self):
        self.assertRegex(
            self.src,
            r'_ferry_wait_http "http://127\.0\.0\.1:\$target_port/health/liveliness"'
            r' +"front" +120 readiness \|\| true')

    def test_mlx_lane_waits_keep_the_catalogue_probe(self):
        # The MLX backends are loopback-only and never authenticated, and their
        # /v1/models 200 IS the weights-resident signal (the port does not
        # accept connections until the weights are resident). Switching them to
        # a litellm-only route would report every cold load NOT READY forever.
        self.assertIn('_ferry_wait_http "http://127.0.0.1:$LOCAL_ORCH_PORT/v1/models"',
                      self.src)
        self.assertIn('_ferry_wait_http "http://127.0.0.1:$LOCAL_SUB_PORT/v1/models"',
                      self.src)
        for lane in ("$LOCAL_ORCH_PORT", "$LOCAL_SUB_PORT"):
            for m in re.finditer(r'_ferry_wait_http "http://127\.0\.0\.1:' +
                                 re.escape(lane) + r'[^"]*"', self.src):
                line = self.src[m.start():self.src.find("\n", m.start())]
                self.assertNotIn("readiness", line,
                                 "MLX lane probes must stay strict content probes")

    def test_wait_http_bearer_is_conditional(self):
        body = self.wait_http_body()
        self.assertIn('[[ -n "${LITELLM_MASTER_KEY:-}" ]] && hdr=', body)

    def test_catalogue_curls_send_the_bearer_only_when_set(self):
        # The stack-up banner, the client status listing, and the host status
        # listing all read /v1/models CONTENT, so each presents the bearer via
        # a guarded array — never an unconditional header.
        for guard in ("banner_auth=()", "status_auth=()", "models_auth=()"):
            self.assertRegex(
                self.src,
                re.escape(guard) + r"\n\s*\[\[ -n \"\$\{LITELLM_MASTER_KEY:-\}\" \]\] && " +
                re.escape(guard[:-3]) + r'=\(-H "Authorization: Bearer \$LITELLM_MASTER_KEY"\)')

    def test_client_connectivity_counts_401_as_online(self):
        # A keyless client probing a keyed host must read ONLINE — the probe is
        # readiness, not catalogue content.
        segment = self.src[self.src.index(">>> Probing network connectivity"):
                           self.src.index(">>> Connection Health")]
        self.assertIn('== "401"', segment)

    def test_printed_commands_reference_the_variable_never_the_value(self):
        # The pasted-curl hints must resolve the key in the PASTING shell
        # (\$LITELLM_MASTER_KEY stays literal in the output) — echoing the
        # value would leak a front-door credential into scrollback.
        self.assertIn(r'Bearer \$LITELLM_MASTER_KEY', self.src)
        for m in re.finditer(r'echo[^\n]*Bearer \\\$LITELLM_MASTER_KEY[^\n]*', self.src):
            line = m.group(0)
            self.assertNotIn("sk-", line.replace("\\$LITELLM_MASTER_KEY", ""),
                             "a printed command embeds a literal key value")


class TestWarnMissingKeys(unittest.TestCase):
    """2026-09-04: _ferry_warn_missing_keys is DERIVED from the route config in
    use ($FERRY_ROUTE_CONFIG), not hardcoded to GLM_API_KEY/GEMINI_API_KEY (both
    retired lanes). It must warn only about vars the config actually references
    via `api_key: os.environ/VAR`, naming the model_names that reference each.
    """

    @classmethod
    def setUpClass(cls):
        with open(FERRY) as f:
            cls.src = f.read()

    def warn_missing_keys_body(self):
        m = re.search(r"_ferry_warn_missing_keys\(\) \{.*?\n\}", self.src, re.S)
        self.assertIsNotNone(m, "_ferry_warn_missing_keys is missing from ferry")
        return m.group(0)

    def run_warn_missing_keys(self, cfg_path, env_overrides):
        """Execute the REAL extracted function against a throwaway route config."""
        with tempfile.NamedTemporaryFile("w", suffix=".zsh", delete=False) as f:
            f.write("set -u\n" + self.warn_missing_keys_body() + "\n"
                    f"FERRY_ROUTE_CONFIG={cfg_path!r}\n"
                    "_ferry_warn_missing_keys\n")
            path = f.name
        env = dict(os.environ)
        for k in ("OPENROUTER_API_KEY", "OTHER_KEY", "LIVE_KEY", "DEAD_KEY"):
            env.pop(k, None)   # hermetic: author shells may export any of these
        env.update(env_overrides)
        try:
            r = subprocess.run(["zsh", path], capture_output=True, text=True,
                               env=env, timeout=30)
        finally:
            os.unlink(path)
        return r.stdout

    def test_warns_about_the_unset_var_and_its_lanes_only(self):
        # OPENROUTER_API_KEY is set (referenced by `flash`); OTHER_KEY is not
        # (referenced by `other-lane`). Only OTHER_KEY, with its own lane name,
        # should show up — never OPENROUTER_API_KEY or a stale hardcoded name.
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                "model_list:\n"
                "  - model_name: flash\n"
                "    litellm_params:\n"
                "      model: openrouter/google/gemini-3.8-flash\n"
                "      api_key: os.environ/OPENROUTER_API_KEY\n"
                "  - model_name: other-lane\n"
                "    litellm_params:\n"
                "      model: someprovider/some-model\n"
                "      api_key: os.environ/OTHER_KEY\n")
            cfg_path = f.name
        try:
            out = self.run_warn_missing_keys(cfg_path, {"OPENROUTER_API_KEY": "sk-set"})
        finally:
            os.unlink(cfg_path)
        self.assertIn("OTHER_KEY", out)
        self.assertIn("other-lane", out)
        self.assertNotIn("OPENROUTER_API_KEY", out)
        self.assertNotIn("GLM_API_KEY", out)
        self.assertNotIn("GEMINI_API_KEY", out)

    def test_two_lanes_sharing_one_var_are_both_named(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                "model_list:\n"
                "  - model_name: flash\n"
                "    litellm_params:\n"
                "      api_key: os.environ/OTHER_KEY\n"
                "  - model_name: super-flash\n"
                "    litellm_params:\n"
                "      api_key: os.environ/OTHER_KEY\n")
            cfg_path = f.name
        try:
            out = self.run_warn_missing_keys(cfg_path, {})
        finally:
            os.unlink(cfg_path)
        self.assertIn("OTHER_KEY", out)
        self.assertIn("flash", out)
        self.assertIn("super-flash", out)

    def test_prints_nothing_when_the_config_cannot_be_read(self):
        out = self.run_warn_missing_keys("/tmp/ferry-test-no-such-config.yaml", {})
        self.assertEqual(out, "")

    def test_a_commented_out_example_block_is_ignored(self):
        # Operators keep template leftovers around, e.g. a commented
        # `# - model_name: heavy-fallback` block referencing a var no live lane
        # needs. The grep pass must not pick up commented `model_name:`/
        # `api_key:` lines — that would both warn about a dead var and (worse)
        # mislabel the next REAL var's lanes with the commented lane's name.
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                "model_list:\n"
                "  - model_name: flash\n"
                "    litellm_params:\n"
                "      model: openrouter/google/gemini-3.8-flash\n"
                "      api_key: os.environ/LIVE_KEY\n"
                "  # - model_name: heavy-fallback\n"
                "  #   litellm_params:\n"
                "  #     model: fireworks_ai/whatever\n"
                "  #     api_key: os.environ/DEAD_KEY\n")
            cfg_path = f.name
        try:
            out = self.run_warn_missing_keys(cfg_path, {})   # both unset
        finally:
            os.unlink(cfg_path)
        self.assertIn("LIVE_KEY", out)
        self.assertIn("flash", out)
        self.assertNotIn("DEAD_KEY", out)
        self.assertNotIn("heavy-fallback", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
