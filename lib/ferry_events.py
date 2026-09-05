"""Per-request events for the ferry relay.

WHY THIS EXISTS

The proxy access log cannot say which model served a request, and no amount of
parsing will make it. Measured 2026-08-29 over 13182 real records: 13076 of them
describe a request and NOT ONE names a model, while all 39 model-naming lines are
startup mentions. uvicorn's access line carries ip, method, path and status — the
model is simply not in it.

litellm does know, and says so in its RESPONSE HEADERS: the lane the client asked
for, the deployment that actually served, how many fallback hops burned, and the
error from each one. This module turns that header list into one record.

Verified against litellm 1.97.0. `get_custom_headers`
(litellm/proxy/common_request_processing.py:1016) emits call-id (:1061),
model-id (:1062), model-name (:1063), model-api-base (:1065) and
response-duration-ms (:1082); every call site splats `**additional_headers`
(:1158 non-streaming, :1868 streaming, :2046, :2131), which is where the router
puts attempted-fallbacks and fallback-errors
(litellm/router_utils/add_retry_fallback_headers.py:241, and the
FallbackErrorInfo TypedDict at :7-11). The streaming branch at :1852-1868 is the
same call, so these land on SSE responses too — at http.response.start, before
the first token.

WHAT IT DOES NOT DO

It never reads a RESPONSE body. The record is built from the header list and
nothing else, which is what lets the tap sit on the streaming path without
buffering. Token counts are therefore absent — the headers do not carry them,
and they stay the metrics pipeline's job. The one body-derived number,
`resp_bytes`, is counted by the tap itself — length only, never content — and
attached to the record before it is written; this module's default for it is 0.

It DOES touch the REQUEST body, since 2026-09-04, and only there:
`comply_tool_schemas` rewrites `tools[].function.parameters` in place, only for
the shapes named in SCHEMA_RULES, only via that rule's registered fix. Messages,
model, and every other key are untouched, and a request with nothing to fix is
left byte-identical. The record key set is unchanged: the findings still land
under `schema_warnings`, now with a `fixed` flag per entry that was patched.

Standard library only: ferry-dash runs under any python3, and everything it
reaches imports the same way.
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import threading

# The record's key set is a contract. lib/ferry-events.test.py asserts it
# exactly, and the exporter and live view read it by name — so adding a key here
# is an allocation, not a free change.
_EMPTY = {
    "t": "", "call_id": "", "lane": "unknown", "deployment": "", "model": "",
    "provider": "", "api_base": "", "status": 0, "fallbacks": 0, "retries": 0,
    "hop_errors": [], "duration_ms": None, "overhead_ms": None, "cost": None,
    "resp_bytes": 0, "client_ip": "", "path": "", "schema_warnings": [],
}

# Request-side tool-schema rules: shapes a provider is KNOWN to reject in a way
# that never comes back as an error. The record names the PAYLOAD, so a lane
# that hangs for one client is traceable to that client's tools instead of
# being booked against the deployment. One rule so far, verified 2026-09-04:
# Gemini function declarations reject an array property with no `items`, and
# through OpenRouter the request returned no response headers at all — litellm
# waited out the deployment timeout on every call and the fallback hop found
# the client already gone (499 on every flash call from one client, for hours,
# while curl and every other client sailed through).
SCHEMA_RULES = {
    "array_without_items": (
        "an array property with no `items` schema; Gemini function "
        "declarations reject it, and through OpenRouter the request hangs "
        "with no response headers until the deployment timeout"),
}


def _fix_array_without_items(schema):
    """`items: {}` — the one-line change that ended the 2026-09-04 outage.

    Verified live that day: the identical request with `items: {}` is served
    by Gemini directly with zero fallbacks, and without it falls back. An
    empty schema is valid JSON Schema meaning "any item", so it is inert for
    every provider that never had the problem.
    """
    schema["items"] = {}


# rule name -> callable(schema_dict) that repairs the offending node IN PLACE.
# A rule may be detect-only: an entry in SCHEMA_RULES with no entry here is
# reported and left alone (the finding then carries no `fixed` flag).
SCHEMA_FIXES = {
    "array_without_items": _fix_array_without_items,
}
SCHEMA_WARNINGS_LIMIT = 20
_SCHEMA_DEPTH = 32


def tool_schema_warnings(doc, limit=SCHEMA_WARNINGS_LIMIT):
    """Findings for the tools in one parsed chat body, `[]` when clean.

    Walks every `tools[].function.parameters` schema — the OpenAI shape every
    client here sends — and returns one dict per hit:
    `{"tool": <function name>, "path": <property path>, "rule": <SCHEMA_RULES key>}`.
    Total and best-effort: malformed input yields what was found so far, never
    an exception, and the list is capped so one pathological request cannot
    inflate its own record.

    Observation only — `doc` is not touched. `comply_tool_schemas` is the
    same walk with the registry's fixes applied.
    """
    return _scan_tools(doc, limit, False)


def comply_tool_schemas(doc, limit=SCHEMA_WARNINGS_LIMIT):
    """Patch `doc`'s tool schemas to the known rules; return what was found.

    Same traversal and same finding shape as `tool_schema_warnings`, plus
    `"fixed": True` on every entry whose rule had a fix in `SCHEMA_FIXES`.
    `doc` is mutated IN PLACE; a document with nothing to fix returns `[]`
    and is left byte-identical. Total and best-effort in the same way: never
    raises, capped by `limit`, bounded by `_SCHEMA_DEPTH`, and malformed
    input yields whatever was found before the malformed part.
    """
    return _scan_tools(doc, limit, True)


def _scan_tools(doc, limit, fix):
    out = []
    try:
        tools = doc.get("tools") if isinstance(doc, dict) else None
        if not isinstance(tools, list):
            return out
        for tool in tools:
            if len(out) >= limit:
                break
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            _walk_schema(fn.get("parameters"),
                         name if isinstance(name, str) else "",
                         "", out, limit, 0, fix)
    except Exception:
        pass
    return out[:limit]


def _walk_schema(schema, tool, path, out, limit, depth, fix=False):
    if (not isinstance(schema, dict) or depth > _SCHEMA_DEPTH
            or len(out) >= limit):
        return
    kind = schema.get("type")
    is_array = kind == "array" or (isinstance(kind, list) and "array" in kind)
    if is_array and "items" not in schema:
        _hit(schema, "array_without_items", tool, path, out, fix)
    props = schema.get("properties")
    if isinstance(props, dict):
        for key, sub in props.items():
            _walk_schema(sub, tool, "%s.%s" % (path, key) if path else str(key),
                         out, limit, depth + 1, fix)
    items = schema.get("items")
    if isinstance(items, dict):
        _walk_schema(items, tool, path + "[]", out, limit, depth + 1, fix)
    elif isinstance(items, list):
        for index, sub in enumerate(items):
            _walk_schema(sub, tool, "%s[%d]" % (path, index), out, limit,
                         depth + 1, fix)
    for key in ("anyOf", "oneOf", "allOf"):
        alts = schema.get(key)
        if isinstance(alts, list):
            for index, sub in enumerate(alts):
                _walk_schema(sub, tool, "%s<%s %d>" % (path, key, index),
                             out, limit, depth + 1, fix)
    extra = schema.get("additionalProperties")
    if isinstance(extra, dict):
        _walk_schema(extra, tool, path + ".*" if path else "*", out, limit,
                     depth + 1, fix)


def _hit(schema, rule, tool, path, out, fix):
    """Record one finding, and repair the node when the caller asked for it.

    A fix that raises costs the repair, not the finding: the entry is still
    reported, just without `fixed`, so the record never claims a patch that
    did not land.
    """
    found = {"tool": tool, "path": path or "(root)", "rule": rule}
    if fix:
        repair = SCHEMA_FIXES.get(rule)
        if repair is not None:
            try:
                repair(schema)
                found["fixed"] = True
            except Exception:
                pass
    out.append(found)

# `openai/` and friends are litellm API-DIALECT markers, not providers. Any
# OpenAI-compatible endpoint is addressed as `openai/<model>` with an explicit
# api_base — the local GPU lanes included. Taking the prefix at face value labels
# a loopback inference server "openai", which is exactly what the naive rule did
# to real captured headers during the plan pre-flight.
_DIALECTS = {"openai", "openai_like", "custom_openai", "hosted_vllm",
             "text-completion-openai"}
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]")


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


def _host(api_base):
    if not api_base:
        return ""
    return api_base.split("://", 1)[-1].split("/", 1)[0]


def provider_for(model, api_base):
    """Who actually served this, as a label.

    A loopback api_base is "local" whatever the dialect says. Otherwise the model
    prefix names the provider, EXCEPT when it is a dialect marker paired with an
    explicit api_base: native use of a provider sets no api_base, so that pairing
    means "some other endpoint speaking that dialect" and the host is the truth.
    """
    host = _host(api_base)
    if host.split(":", 1)[0] in _LOCAL_HOSTS:
        return "local"
    prefix = model.split("/", 1)[0] if model and "/" in model else ""
    if not prefix:
        return host
    if prefix not in _DIALECTS or not api_base:
        return prefix
    # A dialect prefix WITH an api_base is ambiguous: it is either native use of
    # that provider (litellm fills in the provider's own host) or some other
    # endpoint speaking the dialect. The host settles it — if the host is the
    # provider's own domain, the prefix was honest. Caught by a live run,
    # 2026-08-30: genuine OpenAI traffic was being labelled "api.openai.com".
    return prefix if prefix in host else (host or prefix)


def record_from_headers(headers, client_ip, path, status, now=None):
    """Build one event from an ASGI `http.response.start` header list.

    Total and best-effort: a missing header yields the documented default and a
    malformed one yields the default rather than raising. A response carrying no
    attribution headers at all still produces a record with lane "unknown" —
    dropping it would lose exactly the failures worth seeing.
    """
    rec = dict(_EMPTY)
    rec["hop_errors"] = []
    rec["schema_warnings"] = []
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
        rec["provider"] = provider_for(rec["model"], rec["api_base"])
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


def default_path(port=8090):
    """Alongside the proxy log, per lib/ferry-core.zsh LOG_DIR.

    Deliberately `.ndjson`, not `.log`: ferry-dash's find_log() discovers
    `cloud-proxy-<port>.log`, and a `.log` suffix here risks the shipper tailing
    the event stream back into itself.
    """
    base = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(base.rstrip("/"), "ferry-logs", "ferry-events.ndjson")


class EventLog:
    """A bounded, fire-and-forget NDJSON appender.

    `offer` runs on the response path, so it does no I/O at all: it puts on a
    bounded queue and returns. One daemon thread performs every write. A full
    queue DROPS and counts, because a request must never wait on a disk.

    Known trade: a process that exits without close() loses whatever is still
    queued. Losing the last few events at shutdown is the right price for never
    blocking a response, and `dropped` makes any steady-state loss visible.
    """

    def __init__(self, path, max_bytes=67108864, max_queue=2048):
        self.path = path
        self.max_bytes = max_bytes
        self.dropped = 0
        self._announced = 0
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
        """Non-blocking. Returns True if queued, False if dropped or disabled."""
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
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
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
                self._stop.wait(0.01)
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
                # `dropped` lives in this process's memory and every reader is a
                # different process, so the stream is the only channel. The
                # notice is a DIFFERENT SHAPE from a request record — it carries
                # `notice`, never the record contract's keys — so a consumer
                # discriminates on one key instead of guessing from what is
                # missing. Emitted once per NEW drop: re-announcing the running
                # total on every record would bury the stream the moment the
                # queue overflows once.
                if self.dropped > self._announced:
                    self._fh.write(json.dumps(
                        {"t": _utcnow(), "notice": "dropped", "n": self.dropped},
                        separators=(",", ":")) + "\n")
                    self._announced = self.dropped
                self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                self._fh.flush()
                self._rotate_if_needed()
            except Exception:
                self._shut()
            if self._q.empty():
                self._idle.set()
        self._idle.set()
