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

It never reads a request or response body. The caller hands over the header list
and nothing else, which is what lets the tap sit on the streaming path without
buffering. Token counts are therefore absent — the headers do not carry them, and
they stay the metrics pipeline's job.

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
    "client_ip": "", "path": "",
}

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
    if prefix and not (prefix in _DIALECTS and api_base):
        return prefix
    return host or prefix


def record_from_headers(headers, client_ip, path, status, now=None):
    """Build one event from an ASGI `http.response.start` header list.

    Total and best-effort: a missing header yields the documented default and a
    malformed one yields the default rather than raising. A response carrying no
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
                self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                self._fh.flush()
                self._rotate_if_needed()
            except Exception:
                self._shut()
            if self._q.empty():
                self._idle.set()
        self._idle.set()
