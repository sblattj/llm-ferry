# Live Request Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** See, live and per request, which lane was asked for, which deployment served it, what the fallback chain did on the way, and which backends are exhausted.

**Architecture:** litellm already returns the whole attribution record in its response headers. A tap in the ASGI middleware that already sits in front of the proxy reads those headers at `http.response.start`, writes one NDJSON line per request, and two consumers read it: `ferry dash` for the live view, the exporter for labelled metrics. No request or response body is ever read.

**Tech Stack:** Python 3 standard library only (no venv, no pip — `ferry-dash` runs under bare `/usr/bin/python3` by contract). ASGI. Prometheus text format. Grafana OSS + VictoriaMetrics + VictoriaLogs.

**Spec:** `docs/superpowers/specs/2026-08-29-live-request-observability-design.md`

## Global Constraints

- **Standard library only** for `ferry-dash` and anything it imports. No PyYAML.
- **The tap must not read either body.** No `receive` wrapper. The `send` wrapper forwards every message unmodified and adds no `await` beyond the forwarded `send`.
- **Fail-open, always.** Every tap operation inside `try/except`; a broken tap disables itself and never fails, delays, or alters a request.
- **Off by default** behind `FERRY_EVENTS=on` until Task 3's fidelity test passes with its control failing.
- **Public repo — synthetic names only.** Vendor names, provider hosts, real model ids and plan terms do not go in code, comments, fixtures, or commit messages. Use `someprovider`, `some-model`, `example.invalid`. The classifier table is host config, never code.
- **Additive changes only to `parse_topology_text`.** The route editor validates writes against it. No key renamed or removed.
- **Commit trailer:** every commit ends with a `Claude-Session-Id: <uuid>` paragraph, last, nothing after it.

## Namespace inventory (done by the plan author — do not re-derive)

Run 2026-08-29 against `origin/main` @ `8af1fc6` (v1.15.0), after `git fetch`.

| Namespace | Existing allocations | Verdict |
|---|---|---|
| `FERRY_*` env vars | `FERRY_ALERT_WEBHOOK`, `FERRY_BIN`, `FERRY_BIN_PATH`, `FERRY_PORT`, `FERRY_RELAY_TOKEN`, `FERRY_ROUTE_CONFIG`, `FERRY_SHARE_PORT` | **`FERRY_EVENTS` is free** |
| `ferry-dash` routes | GET `/`, `/status`, `/probe`; POST `/api/routes/preview`, `/api/routes/apply` | **use `/api/events`** — the editor set the `/api/` convention. **No `/topology` route:** `get_status()` already returns `topology` |
| `ferry_*` metrics | 16, incl. `ferry_worker_pool_size`, `ferry_fallback_chain_length`, `ferry_deployment_info`, `ferry_backend_events_total` | all 6 new names free; note `ferry_events_total` sits next to the existing `ferry_backend_events_total` — different metric, do not conflate |
| Grafana dashboard uids | `ferry-overview`, `ferry-traffic`, `ferry-backends`, `ferry-models`, `ferry-logs` | **use `ferry-lanes`** (not `ferry-routes`, which would read as the dash's Routes *editor* panel) |
| Alert uids | `ferry-down`, `ferry-exporter-down`, `ferry-high-error-rate`, `ferry-ratelimit-storm`, `ferry-kimi-quota-exhausted`, `ferry-key-pool-shrank`, `ferry-health-check-latency-high`, `ferry-orchestrator-fallback`, `ferry-deployment-cooled-down` | 3 new names free |
| `LOG_DIR` filenames | `local-gpu-$PORT.log`, `cloud-proxy-$PORT.log`, `local-orch-*.log`, `local-sub-*.log`, `share-*.log` | **`ferry-events.ndjson` is free**, and its non-`.log` suffix keeps `find_log()` from ever tailing it |

## Symbol pre-flight (resolved mechanically against the tree)

| Symbol | Location | Signature | Note |
|---|---|---|---|
| `parse_topology_text` | `ferry-dash` | `(text)` | returns keys `error, fallbacks, groups, order, routing`; `groups[n]["count"] > 1` is a pool — verified by parsing a two-deployment fixture |
| `load_topology` | `ferry-dash` | `(path)` | mtime-cached wrapper |
| `find_log` | `ferry-dash` | `(port)` | |
| `get_status` | `ferry-dash` | `()` | already includes `topology` |
| `Activity` | `ferry-dash` | `(logpath)` | |
| `diff_chains` | `ferry-dash` | `(path, chains)` | route editor's writer |
| `LaneCatalogueFilter` | `front/ferry_front.py` | ASGI class | **this is the class name** — not `FerryFront` or similar |
| `MODEL_LIST_PATHS`, `filter_catalogue`, `build_app` | `front/ferry_front.py` | exist | `public_lanes` does **not** exist |

**Baselines, re-measured with the exact commands below.** `python3 observ/ferry-log-shipper.test.py` → **68 pass**. `python3 observ/ferry-metrics-exporter.test.py` → **13 pass**. `python3 lib/ferry-dashroutes.test.py` → **19 pass**.

## File structure

| File | Responsibility |
|---|---|
| `lib/ferry_events.py` | **new.** Build a record from response headers; the bounded async writer. No ASGI, no HTTP — pure and unit-testable. |
| `lib/ferry-events.test.py` | **new.** Tests for the above. |
| `front/ferry_front.py` | **modify.** The `send` wrapper on the hot path, behind `FERRY_EVENTS`. |
| `lib/ferry-front.test.py` | **modify.** Pass-through fidelity + tap behaviour. |
| `ferry-dash` | **modify, minimally.** Extend `parse_topology_text` additively; add the `/api/events` route and the live panel mount. |
| `lib/ferry_live.py` | **new.** The NDJSON tail, the SSE framing, the exhaustion state machine. |
| `lib/ferry-live.test.py` | **new.** |
| `observ/ferry-metrics-exporter` | **modify.** New labelled families from topology + events. |
| `observ/grafana/dashboards/ferry-lanes.json` | **new.** |
| `observ/grafana/provisioning/alerting/rules.yml` | **modify.** Three new rules. |

---

### Task 1: The event record

**Files:**
- Create: `lib/ferry_events.py`
- Test: `lib/ferry-events.test.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `record_from_headers(headers, client_ip, path, status, now=None) -> dict`. `headers` is an iterable of `(bytes, bytes)` pairs exactly as ASGI delivers them in `http.response.start`. Returns the dict documented below. Never raises.

Returned key set — **this is an allocation; later tasks and tests assert on it**:
`t, call_id, lane, deployment, model, provider, api_base, status, fallbacks, retries, hop_errors, duration_ms, overhead_ms, cost, client_ip, path`.

- [ ] **Step 1: Write the failing test**

```python
# lib/ferry-events.test.py
import importlib.machinery, importlib.util, os, sys, unittest

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
    return [(k.replace("_", "-").encode(), str(v).encode()) for k, v in kw.items()]

class TestRecord(unittest.TestCase):
    def test_lane_comes_from_model_group_not_model_name(self):
        # x-litellm-model-name is the UNDERLYING model string; the lane the
        # client asked for is x-litellm-model-group. Confusing them mislabels
        # every event with a provider path instead of a lane.
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
        # Naively trusting the prefix labels the on-box MLX server "openai".
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
        # the record loses the failure entirely; `unknown` keeps it visible.
        r = E.record_from_headers([], "10.0.0.9", "/v1/chat/completions", 500)
        self.assertEqual(r["lane"], "unknown")
        self.assertEqual(r["status"], 500)

    def test_malformed_fallback_errors_do_not_raise(self):
        r = E.record_from_headers(
            hdrs(**{"x_litellm_fallback_errors": "{not json"}),
            "", "/v1/chat/completions", 200)
        self.assertEqual(r["hop_errors"], [])

    def test_key_set_is_exactly_the_contract(self):
        r = E.record_from_headers([], "", "/v1/chat/completions", 200)
        self.assertEqual(set(r), {
            "t", "call_id", "lane", "deployment", "model", "provider",
            "api_base", "status", "fallbacks", "retries", "hop_errors",
            "duration_ms", "overhead_ms", "cost", "client_ip", "path"})

if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 lib/ferry-events.test.py`
Expected: FAIL — `No such file or directory: .../ferry_events.py`.

- [ ] **Step 3: Implement**

```python
# lib/ferry_events.py
"""Per-request events for the ferry relay.

litellm returns the whole attribution record in its RESPONSE HEADERS — the lane
asked for, the deployment that served, how many fallback hops burned, and the
error from each one. Nothing here reads a request or response body; the caller
hands over the header list from `http.response.start` and nothing else.

Verified against litellm 1.97.0: `get_custom_headers`
(litellm/proxy/common_request_processing.py:1016) emits call-id/model-id/
model-name/model-api-base/response-duration-ms, and every call site splats
`**additional_headers`, which is where the router puts attempted-fallbacks and
fallback-errors. The streaming branch (:1852-1868) does the same, so these are
present on SSE responses too.
"""
from __future__ import annotations

import datetime
import json

# The record's key set is a contract; `lib/ferry-events.test.py` asserts it
# exactly, and the exporter and live view read by name.
_EMPTY = {
    "t": "", "call_id": "", "lane": "unknown", "deployment": "", "model": "",
    "provider": "", "api_base": "", "status": 0, "fallbacks": 0, "retries": 0,
    "hop_errors": [], "duration_ms": None, "overhead_ms": None, "cost": None,
    "client_ip": "", "path": "",
}


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _num(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int(raw):
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


# `openai/` and friends are litellm API-DIALECT markers, not providers. Any
# OpenAI-compatible endpoint is addressed as `openai/<model>` with an explicit
# api_base — the local GPU lanes included. Taking the prefix at face value
# labels a loopback MLX server "openai", which is how this rule was caught:
# feeding the real captured headers of a local lane through the naive version
# returned provider="openai" for http://127.0.0.1:8093/v1.
_DIALECTS = {"openai", "openai_like", "custom_openai", "hosted_vllm",
             "text-completion-openai"}
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]")


def _host(api_base):
    if not api_base:
        return ""
    return api_base.split("://", 1)[-1].split("/", 1)[0]


def _provider(model, api_base):
    """Who actually served this, as a label.

    A loopback api_base is "local" regardless of dialect. Otherwise the model
    prefix names the provider, EXCEPT when it is a dialect marker paired with an
    explicit api_base — native use of a provider sets no api_base, so that pair
    means "some other endpoint speaking that dialect" and the host is the truth.
    """
    host = _host(api_base)
    if host.split(":", 1)[0] in _LOCAL_HOSTS:
        return "local"
    prefix = model.split("/", 1)[0] if model and "/" in model else ""
    if prefix and not (prefix in _DIALECTS and api_base):
        return prefix
    return host or prefix


def record_from_headers(headers, client_ip, path, status, now=None):
    """Build one event from an ASGI `http.response.start` header list.

    Total and best-effort: a missing header yields the documented default, a
    malformed one yields the default rather than raising. A response with no
    attribution headers at all still produces a record with lane "unknown" —
    dropping it would lose exactly the failures worth seeing.
    """
    rec = dict(_EMPTY)
    rec["hop_errors"] = []
    try:
        h = {}
        for k, v in headers or ():
            try:
                h[k.decode("latin-1").lower()] = v.decode("latin-1")
            except Exception:
                continue

        rec["t"] = now or _utcnow()
        rec["call_id"] = h.get("x-litellm-call-id", "")
        rec["lane"] = h.get("x-litellm-model-group") or "unknown"
        rec["deployment"] = h.get("x-litellm-model-id", "")
        rec["model"] = h.get("x-litellm-model-name", "")
        rec["api_base"] = h.get("x-litellm-model-api-base", "")
        rec["provider"] = _provider(rec["model"], rec["api_base"])
        rec["status"] = int(status or 0)
        rec["fallbacks"] = _int(h.get("x-litellm-attempted-fallbacks"))
        rec["retries"] = _int(h.get("x-litellm-attempted-retries"))
        rec["duration_ms"] = _num(h.get("x-litellm-response-duration-ms"))
        rec["overhead_ms"] = _num(h.get("x-litellm-overhead-duration-ms"))
        rec["cost"] = _num(h.get("x-litellm-response-cost"))
        rec["client_ip"] = client_ip or ""
        rec["path"] = path or ""

        raw = h.get("x-litellm-fallback-errors")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    rec["hop_errors"] = [p for p in parsed if isinstance(p, dict)]
            except Exception:
                pass
    except Exception:
        pass
    return rec
```

- [ ] **Step 4: Run the tests**

Run: `python3 lib/ferry-events.test.py`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add lib/ferry_events.py lib/ferry-events.test.py
git commit -F <message file ending in a Claude-Session-Id trailer>
```

---

### Task 2: The bounded async writer

**Files:**
- Modify: `lib/ferry_events.py`
- Test: `lib/ferry-events.test.py`

**Interfaces:**
- Consumes: `record_from_headers` from Task 1.
- Produces: `EventLog(path, max_bytes=67108864, max_queue=2048)` with `.offer(rec) -> bool` (never blocks, never raises, returns False when dropped), `.dropped` (int), `.flush()` (test-only, drains synchronously), `.close()`. Also `default_path(port=8090) -> str` returning `${TMPDIR:-/tmp}/ferry-logs/ferry-events.ndjson`.

- [ ] **Step 1: Write the failing test**

```python
# append to lib/ferry-events.test.py, before the __main__ block
import json as _json, tempfile, os as _os

class TestEventLog(unittest.TestCase):
    def _log(self, **kw):
        d = tempfile.mkdtemp()
        return E.EventLog(_os.path.join(d, "ferry-events.ndjson"), **kw)

    def test_offer_writes_one_json_line_per_record(self):
        log = self._log()
        log.offer({"lane": "flash"})
        log.offer({"lane": "heavy"})
        log.flush()
        lines = [l for l in open(log.path) if l.strip()]
        self.assertEqual([_json.loads(l)["lane"] for l in lines], ["flash", "heavy"])
        log.close()

    def test_a_full_queue_drops_instead_of_blocking(self):
        log = self._log(max_queue=2)
        log.pause()                      # writer parked, so the queue really fills
        accepted = [log.offer({"n": i}) for i in range(6)]
        self.assertEqual(accepted.count(True), 2)
        self.assertEqual(log.dropped, 4)
        log.close()

    def test_an_unwritable_path_disables_the_log_and_never_raises(self):
        log = E.EventLog("/proc/nonexistent/ferry-events.ndjson")
        self.assertIsNone(log.offer({"lane": "flash"}) and None)
        log.flush()
        self.assertFalse(log.healthy)
        log.close()

    def test_rotation_at_max_bytes_keeps_writing(self):
        log = self._log(max_bytes=200)
        for i in range(50):
            log.offer({"lane": "flash", "i": i, "pad": "x" * 40})
        log.flush()
        self.assertTrue(_os.path.exists(log.path))
        self.assertTrue(_os.path.exists(log.path + ".1"))
        log.close()

    def test_default_path_is_in_the_ferry_log_dir_and_is_not_a_dot_log(self):
        p = E.default_path()
        self.assertTrue(p.endswith("/ferry-logs/ferry-events.ndjson"))
        # find_log() globs for cloud-proxy-*.log; a .log suffix here would put
        # the event stream in the shipper's mouth.
        self.assertFalse(p.endswith(".log"))
```

- [ ] **Step 2: Run and watch it fail**

Run: `python3 lib/ferry-events.test.py`
Expected: FAIL — `module 'ferry_events' has no attribute 'EventLog'`.

- [ ] **Step 3: Implement**

```python
# append to lib/ferry_events.py
import os
import queue
import threading


def default_path(port=8090):
    """Alongside the proxy log, per lib/ferry-core.zsh LOG_DIR.

    Deliberately `.ndjson`, not `.log`: ferry-dash's find_log() discovers
    `cloud-proxy-<port>.log`, and a `.log` suffix here risks the shipper
    tailing the event stream back into itself.
    """
    base = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(base.rstrip("/"), "ferry-logs", "ferry-events.ndjson")


class EventLog:
    """A bounded, fire-and-forget NDJSON appender.

    `offer` is called on the response path, so it does no I/O: it puts on a
    bounded queue and returns. One daemon thread does every write. A full queue
    DROPS and counts, because a request must never wait on a disk.
    """

    def __init__(self, path, max_bytes=67108864, max_queue=2048):
        self.path = path
        self.max_bytes = max_bytes
        self.dropped = 0
        self.healthy = True
        self._q = queue.Queue(maxsize=max_queue)
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._fh = None
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def pause(self):
        """Test hook: park the writer so a bounded queue can actually fill."""
        self._paused.set()

    def offer(self, rec):
        if not self.healthy:
            return False
        try:
            self._idle.clear()
            self._q.put_nowait(rec)
            return True
        except queue.Full:
            self.dropped += 1
            return False
        except Exception:
            return False

    def flush(self, timeout=2.0):
        """Test-only: block until the queue has drained."""
        self._idle.wait(timeout)

    def close(self):
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        self._t.join(timeout=2.0)
        self._shut()

    # ── writer thread ──────────────────────────────────────────────────────
    def _open(self):
        if self._fh is not None:
            return True
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
            return True
        except Exception:
            self.healthy = False
            return False

    def _shut(self):
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        self._fh = None

    def _rotate_if_needed(self):
        try:
            if self._fh and self._fh.tell() >= self.max_bytes:
                self._shut()
                os.replace(self.path, self.path + ".1")
        except Exception:
            self._shut()

    def _run(self):
        while not self._stop.is_set():
            while self._paused.is_set() and not self._stop.is_set():
                threading.Event().wait(0.01)
            try:
                rec = self._q.get(timeout=0.1)
            except queue.Empty:
                if self._q.empty():
                    self._idle.set()
                continue
            if rec is None:
                break
            if not self._open():
                self._idle.set()
                continue
            try:
                self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                self._fh.flush()
                self._rotate_if_needed()
            except Exception:
                self._shut()
            if self._q.empty():
                self._idle.set()
        self._idle.set()
```

- [ ] **Step 4: Run the tests**

Run: `python3 lib/ferry-events.test.py`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

---

### Task 3: Wire the tap into the front door

**Files:**
- Modify: `front/ferry_front.py` (the `LaneCatalogueFilter.__call__` hot path)
- Modify: `lib/ferry-front.test.py`

**Interfaces:**
- Consumes: `EventLog`, `record_from_headers`, `default_path` from Tasks 1-2.
- Produces: nothing later tasks import. The observable output is the NDJSON file.

**Authority note — read before editing.** `front/ferry_front.py:112-114` currently documents *"No wrapper around `send`, so a streamed completion is byte-for-byte what litellm produced."* This task makes that comment false and **the comment is the thing to update, not the behaviour to preserve literally.** The property that must survive is byte-for-byte pass-through, not the absence of a wrapper. Update the comment in the same edit.

- [ ] **Step 1: Write the failing test**

```python
# append to lib/ferry-front.test.py (F is the loaded ferry_front module)
import asyncio, json, os, tempfile, unittest

class TestEventTap(unittest.TestCase):
    """The tap must be invisible to the byte stream. These assertions are the
    rollout gate: if any of them fails, the tap does not ship enabled."""

    def _run(self, app, scope, enabled=True, path=None):
        sent = []
        async def send(m): sent.append(m)
        async def receive(): return {"type": "http.request", "body": b""}
        os.environ["FERRY_EVENTS"] = "on" if enabled else "off"
        F.reset_tap(path)
        asyncio.get_event_loop().run_until_complete(app(scope, receive, send))
        return sent

    def _streaming_app(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream"),
                                    (b"x-litellm-model-group", b"flash"),
                                    (b"x-litellm-model-id", b"flash-alt-1"),
                                    (b"x-litellm-attempted-fallbacks", b"1")]})
            await send({"type": "http.response.body", "body": b"a", "more_body": True})
            await send({"type": "http.response.body", "body": b"bb", "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        return app

    def test_chunk_boundaries_and_more_body_survive_the_tap(self):
        scope = {"type": "http", "path": "/v1/chat/completions",
                 "client": ("10.0.0.9", 5000)}
        app = F.LaneCatalogueFilter(self._streaming_app(), frozenset({"flash"}))
        with_tap = self._run(app, scope, enabled=True)
        without = self._run(app, scope, enabled=False)
        self.assertEqual(with_tap, without)
        bodies = [(m.get("body"), m.get("more_body")) for m in with_tap
                  if m["type"] == "http.response.body"]
        self.assertEqual(bodies, [(b"a", True), (b"bb", True), (b"", None)])

    def test_headers_are_not_rewritten_on_the_hot_path(self):
        scope = {"type": "http", "path": "/v1/chat/completions", "client": ("1.2.3.4", 1)}
        app = F.LaneCatalogueFilter(self._streaming_app(), frozenset({"flash"}))
        start = [m for m in self._run(app, scope) if m["type"] == "http.response.start"][0]
        self.assertNotIn(b"content-length", dict(start["headers"]))

    def test_an_event_is_written_with_the_lane_and_deployment(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ferry-events.ndjson")
        scope = {"type": "http", "path": "/v1/chat/completions", "client": ("10.0.0.9", 1)}
        app = F.LaneCatalogueFilter(self._streaming_app(), frozenset({"flash"}))
        self._run(app, scope, enabled=True, path=p)
        F.tap_flush()
        rec = json.loads(open(p).readline())
        self.assertEqual(rec["lane"], "flash")
        self.assertEqual(rec["deployment"], "flash-alt-1")
        self.assertEqual(rec["fallbacks"], 1)

    def test_disabled_writes_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ferry-events.ndjson")
        scope = {"type": "http", "path": "/v1/chat/completions", "client": ("1.2.3.4", 1)}
        app = F.LaneCatalogueFilter(self._streaming_app(), frozenset({"flash"}))
        self._run(app, scope, enabled=False, path=p)
        self.assertFalse(os.path.exists(p))

    def test_a_raising_tap_does_not_break_the_response(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ferry-events.ndjson")
        scope = {"type": "http", "path": "/v1/chat/completions", "client": ("1.2.3.4", 1)}
        app = F.LaneCatalogueFilter(self._streaming_app(), frozenset({"flash"}))
        F.reset_tap(p)
        F._TAP.offer = lambda rec: (_ for _ in ()).throw(RuntimeError("boom"))
        os.environ["FERRY_EVENTS"] = "on"
        sent = []
        async def send(m): sent.append(m)
        async def receive(): return {"type": "http.request", "body": b""}
        asyncio.get_event_loop().run_until_complete(app(scope, receive, send))
        self.assertEqual([m["type"] for m in sent],
                         ["http.response.start", "http.response.body",
                          "http.response.body", "http.response.body"])
```

- [ ] **Step 2: Run and watch it fail**

Run: `python3 lib/ferry-front.test.py`
Expected: FAIL — `module 'ferry_front' has no attribute 'reset_tap'`.

- [ ] **Step 3: Implement**

Replace the hot-path early return in `LaneCatalogueFilter.__call__` and add the module-level tap. The `_TAP` is created lazily so importing the module never touches the filesystem.

```python
# near the top of front/ferry_front.py, after the existing imports
import importlib.machinery
import importlib.util
import os as _os
import sys as _sys

_TAP = None
_TAP_PATH = None


def _events_module():
    p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                      "lib", "ferry_events.py")
    spec = importlib.util.spec_from_loader(
        "ferry_events", importlib.machinery.SourceFileLoader("ferry_events", p))
    m = importlib.util.module_from_spec(spec)
    _sys.modules["ferry_events"] = m
    spec.loader.exec_module(m)
    return m


def tap_enabled():
    return (_os.environ.get("FERRY_EVENTS") or "").strip().lower() in ("1", "on", "true", "yes")


def reset_tap(path=None):
    """Test hook: drop any existing tap so the next request builds a fresh one."""
    global _TAP, _TAP_PATH
    if _TAP is not None:
        try:
            _TAP.close()
        except Exception:
            pass
    _TAP = None
    _TAP_PATH = path


def tap_flush():
    if _TAP is not None:
        _TAP.flush()


def _tap():
    global _TAP
    if _TAP is None:
        try:
            E = _events_module()
            _TAP = E.EventLog(_TAP_PATH or E.default_path())
            _TAP._record = E.record_from_headers
        except Exception:
            return None
    return _TAP
```

```python
    # front/ferry_front.py — replace the hot-path early return in __call__
    async def __call__(self, scope, receive, send):
        # The hot path. Everything that is not the model listing is handed over
        # with its bytes untouched. When FERRY_EVENTS is on, `send` IS wrapped —
        # but the wrapper forwards every message unmodified and only READS the
        # header list on http.response.start, so a streamed completion is still
        # byte-for-byte what litellm produced. `receive` is never wrapped: the
        # lane comes from the x-litellm-model-group RESPONSE header, so no
        # request body is ever read. lib/ferry-front.test.py asserts the
        # equivalence against a tap-disabled control.
        if (
            not self.public
            or scope.get("type") != "http"
            or scope.get("path") not in MODEL_LIST_PATHS
        ):
            if scope.get("type") == "http" and tap_enabled():
                return await self.app(scope, receive, self._tapped(scope, send))
            return await self.app(scope, receive, send)
        ...  # the catalogue path below is unchanged

    def _tapped(self, scope, send):
        """Wrap `send` to read attribution headers. Fail-open in every branch."""
        async def tapped(message):
            if message.get("type") == "http.response.start":
                try:
                    tap = _tap()
                    if tap is not None:
                        client = scope.get("client") or ("", 0)
                        tap.offer(tap._record(
                            message.get("headers") or [],
                            client[0] if client else "",
                            scope.get("path", ""),
                            message.get("status", 0)))
                except Exception:
                    pass
            return await send(message)
        return tapped
```

- [ ] **Step 4: Run the tests**

Run: `python3 lib/ferry-front.test.py`
Expected: PASS, existing tests plus 5 new.

- [ ] **Step 5: Run the control — the fidelity test MUST be able to fail**

Temporarily make `tapped` mutate the stream (e.g. `message["body"] = b"X"` on a body message), re-run, and confirm `test_chunk_boundaries_and_more_body_survive_the_tap` FAILS. Revert. A pass-through test that cannot fail is not a rollout gate.

- [ ] **Step 6: Live check under the real loader**

```bash
FERRY_EVENTS=on ferry up --route
curl -s -N http://127.0.0.1:8090/v1/chat/completions \
  -H 'Authorization: Bearer local' -H 'Content-Type: application/json' \
  -d '{"model":"local-sub","messages":[{"role":"user","content":"hi"}],"max_tokens":4,"stream":true}' \
  > /tmp/stream.out
tail -1 "${TMPDIR:-/tmp}/ferry-logs/ferry-events.ndjson"
```
Expected: the SSE stream arrives intact in `/tmp/stream.out` (chunks, not one blob), and the last event line names lane `local-sub` and deployment `local-sub-mlx`. Use a LOCAL lane so the check costs nothing.

- [ ] **Step 7: Commit**

---

### Task 4: Extend the topology parse additively

**Files:**
- Modify: `ferry-dash` (`parse_topology_text`)
- Test: `lib/ferry-dashroutes.test.py` is the **control** — it must stay at 19 pass. Add new assertions in `lib/ferry-live.test.py` (created in Task 5) rather than editing the editor's tests.

**Interfaces:**
- Produces: `parse_topology_text(text)` gains, per group, `ids: [str]` (the `model_info.id` of each deployment, in file order), `public: bool`, and `providers: [str]`. Existing keys `error, fallbacks, groups, order, routing` and `groups[n]["count"] / ["models"]` are unchanged.

**Authority note:** where the plan and the tree disagree, **the tree is authoritative** — the editor's writer validates against this structure. Add keys; rename nothing.

- [ ] **Step 1: Write the failing test** — see Task 5's test file; assert `ids`, `public`, `providers` on a two-deployment fixture, and assert `count`/`models`/`fallbacks` are unchanged.
- [ ] **Step 2: Run and watch it fail** (`KeyError: 'ids'`).
- [ ] **Step 3: Implement** — in the `section == "models"` branch, additionally match `^\s*id:\s*(\S+)`, `^\s*public:\s*(true|false)` and derive the provider from the already-captured `model:` value with the same `_provider` rule as `lib/ferry_events.py` (import it rather than re-implementing — one rule, one place).
- [ ] **Step 4: Run BOTH suites.** `python3 lib/ferry-dashroutes.test.py` → **19 pass, unchanged**. `python3 lib/ferry-live.test.py` → new assertions pass.
- [ ] **Step 5: Commit**

---

### Task 5: The event tail and the SSE endpoint

**Files:**
- Create: `lib/ferry_live.py`, `lib/ferry-live.test.py`
- Modify: `ferry-dash` (add `GET /api/events`)

**Interfaces:**
- Produces: `EventTail(path)` with `.read_new() -> list[dict]` (tails from EOF on first call; survives truncation and rotation by `(st_dev, st_ino)` plus a head fingerprint, the same ladder `observ/ferry-log-shipper`'s `Tailer` uses); `sse_frame(rec) -> bytes`.

- [ ] **Step 1: Write the failing test** — a tail that returns only lines appended after construction; a rotated file re-read from zero; a half-written line buffered until its newline arrives; `sse_frame` emitting `data: {json}\n\n`.
- [ ] **Step 2: Run and watch it fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** `python3 lib/ferry-live.test.py`
- [ ] **Step 5: Add the route** — in `ferry-dash`'s `do_GET`, before the `/status` branch: `elif self.path.startswith("/api/events"):` streaming `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`, polling `read_new()` on a short interval. Note `ThreadingHTTPServer` gives each SSE client its own thread; cap concurrent streams and drop the oldest so a forgotten browser tab cannot pin the dashboard.
- [ ] **Step 6: Commit**

---

### Task 6: The exhaustion state machine

**Files:**
- Modify: `lib/ferry_live.py`, `lib/ferry-live.test.py`

**Interfaces:**
- Produces: `ExhaustionState(rules)` with `.observe(rec)`, `.observe_metric(name, labels, value)`, `.snapshot() -> {deployment: {state, since, detail, code}}`. `rules` is loaded from host config (`~/.config/ferry/event-rules.json`), **never hardcoded** — that is what keeps vendor names and plan terms out of a public repo.

States: `healthy · rate_limited · quota_exhausted · auth_dead · unreachable · unknown`.

- [ ] **Step 1: Write the failing test** — with a synthetic rule file (`someprovider`, `some-model`): a 429 matching a quota rule becomes `quota_exhausted`; a plain 429 becomes `rate_limited`; a 401 becomes `auth_dead`; an unmatched error becomes `unknown`, **never** `healthy`; `quota_exhausted` survives a later `rate_limited` on a *different* deployment (the last-writer-wins defect); `quota_exhausted` clears only on a 2xx for that same deployment; `rate_limited` decays after its TTL; `detail` carries the provider's own message text verbatim.
- [ ] **Step 2: Run and watch it fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.**
- [ ] **Step 5: Ship a synthetic `event-rules.example.json`** in the repo with placeholder vendors, and document that the real one lives in `~/.config/ferry/`.
- [ ] **Step 6: Commit**

---

### Task 7: The live view

**Files:**
- Modify: `ferry-dash` (HTML/CSS/JS panel + mount)

- [ ] **Step 1** Render lanes from `/status`'s existing `topology` — public lanes expanded, the rest behind a toggle. Each lane: primary then ordered hops, a pooled hop (`count > 1`) as a fan-out.
- [ ] **Step 2** Subscribe to `/api/events`; on each event light the served edge green and, for `fallbacks: n`, the first `n` hop edges red with `hop_errors[i].code`.
- [ ] **Step 3** Per-deployment chip: state, req/min, p50, last error code, "since HH:MM" for a sticky state.
- [ ] **Step 4** The scrolling feed, newest first, capped at 200 rows so a long session cannot grow the DOM without bound.
- [ ] **Step 5** Degrade honestly: if `/api/events` 404s or the tap is off, say *"event tap is off — start ferry with FERRY_EVENTS=on"* rather than rendering an empty graph that looks like no traffic.
- [ ] **Step 6: Commit**

---

### Task 8: Labelled metrics from topology and events

**Files:**
- Modify: `observ/ferry-metrics-exporter`, `observ/ferry-metrics-exporter.test.py`

**Interfaces:**
- Produces: `ferry_lane_hop{lane,position,hop,deployment,model,provider,pool_size}`, `ferry_lane_chain_length{lane}`, `ferry_pool_size{hop}`, `ferry_deployment_state{deployment,provider,state}`, `ferry_deployment_state_since_seconds{deployment}`, `ferry_events_total{lane,deployment,provider,outcome}`, `ferry_fallback_edges_total{lane,from_deployment,to_deployment,code}`.
- `ferry_worker_pool_size` and `ferry_fallback_chain_length` (unlabelled) are **retained unchanged** — `ferry-backends.json` panels 1-2 and the alert rules read them.

- [ ] **Step 1: Write the failing test** — all 7 families emitted from a fixture topology + fixture events; the two legacy scalars still present with their old values; a family with zero samples omits its HELP/TYPE (the existing convention); a dropped-event counter is exported so an overflowing tap is visible.
- [ ] **Step 2: Run and watch it fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** `python3 observ/ferry-metrics-exporter.test.py` → 13 existing plus new, all pass.
- [ ] **Step 5: Commit**

---

### Task 9: The lanes dashboard and the alerts that name a cause

**Files:**
- Create: `observ/grafana/dashboards/ferry-lanes.json` (uid `ferry-lanes`)
- Modify: `observ/grafana/provisioning/alerting/rules.yml`

**Grafana traps that apply here** (from the house skill — do not rediscover): the threshold evaluator has **no `gte`/`lte`**, so any `>=` goes in PromQL as `>= bool X` with the node thresholded `gt 0`; every rule is a 3-node `query → reduce(last) → threshold` chain whose `condition` is `C`; "the series is absent" is `noDataState`, not an expression clause; a merged table's value columns arrive as `Value`, `Value 1`, `Value 2` and need an `organize` transform to be readable.

- [ ] **Step 1** Dashboard: lane topology table from `ferry_lane_hop`; fallback-edge table from `ferry_fallback_edges_total`; exhaustion timeline from `ferry_deployment_state`; per-lane request rate from `ferry_events_total`.
- [ ] **Step 2** Alert `ferry-deployment-quota-exhausted`: `max by (deployment) (ferry_deployment_state{state="quota_exhausted"}) >= bool 1`, threshold `gt 0`, `for: 5m`.
- [ ] **Step 3** Alert `ferry-lane-chain-exhausted` — **the outage condition that is currently unalertable**: every deployment in a lane's chain simultaneously non-healthy. Author it, then **fire it synthetically** and confirm the instance appears with the right `lane` label. An alert nobody has seen fire is not an alert.
- [ ] **Step 4** Alert `ferry-pool-member-lost`: a hop whose `ferry_pool_size` dropped below its own 24h max. Re-point `ferry-key-pool-shrank` at `ferry_pool_size{hop}` now that per-hop series exist, replacing the interim global-max fix.
- [ ] **Step 5** Bring the stack up (`observ/bringup.sh`), run `observ/verify.sh`, and confirm each new panel renders real data — every trap in this file's list renders *something* rather than erroring, so the verification is visual.
- [ ] **Step 6: Commit**

---

## Self-review

**Spec coverage.** Event record → T1. Tap → T3. Topology → T4. Live view → T5, T7. Exhaustion → T6. Metrics → T8. Dashboard + alerts → T9. Rollout gate → T3 steps 5-6. Testing section → distributed across each task's test step. The spec's three named hypotheses (error-path headers, `model-group` universality, `hop_errors` ordering) are covered by T1's `test_missing_headers_yield_unknown_lane_not_a_dropped_record` and T3's live check; **T3 step 6 should additionally be run against a deliberately failing lane** to settle hypothesis 1 before T7 draws red edges from `hop_errors` indices.

**Placeholder scan.** No TBD/TODO. Tasks 1-3 carry full code. Tasks 4-9 carry interfaces, exact metric/label/uid names, exact commands and exact expected counts; their bodies are edits to existing files whose shape is pinned by the Interfaces block and the symbol pre-flight table.

**Type consistency.** `record_from_headers(headers, client_ip, path, status, now=None)` is called with exactly that shape in T3. `EventLog.offer(rec) -> bool` is used for its return in T2's tests and ignored in T3, which is fine. `_provider` is defined once in `lib/ferry_events.py` and imported by T4 rather than duplicated. The record key set declared in T1 is the set T8 reads by name.

**Known gap, stated rather than hidden:** T2's `EventLog` writes from a thread while `front/ferry_front.py` runs under asyncio. That is deliberate — a queue put is non-blocking and the event loop is never touched — but it means a process that exits without `close()` can lose whatever is still queued. Losing the last few events on shutdown is the correct trade against ever blocking a response, and the dropped counter from T8 makes any steady-state loss visible.
