"""litellm's proxy app with the lane catalogue filtered.

WHY THIS EXISTS

`/v1/models` advertises every real `model_name` in the config, which on the
ferry stack means the fallback deployments as well as the four lanes. That is
not cosmetic. `router_settings.fallbacks` is keyed by model group: `orch` has an
entry, `orch-deepseek` does not. A client that picks a fallback hop out of a
model list gets a single provider with nothing behind it — the exact opposite of
what the `orch` lane exists to provide, and it fails only when that hop is down,
which is the case the chain was built for.

litellm has no config-only fix. `hidden` is honoured for `model_group_alias`
entries ONLY (litellm/router.py, both the model-group-info and model-list
paths); a deployment's `model_info` is never consulted for it, and
`litellm.public_model_groups` sets a flag on `/model_group/info` without
filtering `/v1/models`. Per-key model access would work but needs the DB-backed
virtual-key layer, and ferry serves one shared static key.

WHAT THIS DOES

Wraps litellm's own FastAPI app in pure ASGI middleware. A lane declares itself
with `model_info: {public: true}` in the route config (`model_info` is
`extra: allow`, so litellm accepts and ignores the key). Anything not so marked
is dropped from the catalogue.

The inference path is NOT proxied. For every request whose path is not the model
listing, `__call__` hands scope/receive/send straight to the wrapped app and
returns — no buffering, no response rewriting, nothing between the client and a
streaming token. This is deliberately NOT Starlette's BaseHTTPMiddleware, which
buffers and is a known way to break SSE.

FAIL-OPEN, ALWAYS. A hop visible in the catalogue is a routing wart. A front
door that refuses to answer is an outage. Every failure mode here — an
unreadable config, no lane marked public, a body that is not the JSON we expect
— returns the upstream response untouched.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys

MODEL_LIST_PATHS = frozenset({"/v1/models", "/models"})

# Only an actual inference call is an event. This is an ALLOWLIST on purpose: a
# denylist of health/metrics paths would have to keep pace with litellm's route
# table, and anything it missed becomes silent noise. Caught by a live run,
# 2026-08-30 — /health/liveliness was producing a lane:"unknown" event, and both
# ferry-dash and the exporter poll it every 5s, so the feed would have been
# roughly 17k junk records a day.
INFERENCE_PATH_PREFIXES = (
    "/v1/chat/completions", "/chat/completions",
    "/v1/completions", "/completions",
    "/v1/messages", "/messages",
    "/v1/responses", "/responses",
    "/v1/embeddings", "/embeddings",
)


def is_inference_path(path: str) -> bool:
    """Whether this path is a served model call worth an event."""
    if not path or path in MODEL_LIST_PATHS:
        return False
    return any(path.startswith(p) for p in INFERENCE_PATH_PREFIXES)

# ── the event tap ──────────────────────────────────────────────────────────
# litellm returns the whole per-request attribution record in its RESPONSE
# HEADERS, and nothing in ferry reads it. The proxy log cannot substitute:
# measured 2026-08-29 over 13182 real records, not one of the 13076 lines that
# describe a request names a model at all.
#
# So the tap wraps `send` on the hot path — which this module was written to
# avoid — and the property that must survive is no longer "no wrapper" but "the
# bytes are identical". `lib/ferry-front.test.py` asserts that equivalence
# against a tap-disabled control, and that test is the rollout gate.
#
# `receive` is never wrapped: the lane comes from a RESPONSE header, so no
# request body is ever read. The one body-derived field is `resp_bytes`, a
# length counted on the way past (never buffered, never rewritten), attached
# when the final body chunk forwards. Off unless FERRY_EVENTS says otherwise.
_TAP = None
_TAP_PATH = None


def _events_module():
    """Load lib/ferry_events.py by path — its sibling name has no dot form."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "lib", "ferry_events.py")
    spec = importlib.util.spec_from_loader(
        "ferry_events", importlib.machinery.SourceFileLoader("ferry_events", path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["ferry_events"] = module
    spec.loader.exec_module(module)
    return module


def tap_enabled() -> bool:
    return (os.environ.get("FERRY_EVENTS") or "").strip().lower() in (
        "1", "on", "true", "yes")


def reset_tap(path=None) -> None:
    """Test hook: drop any live tap so the next request builds a fresh one."""
    global _TAP, _TAP_PATH
    if _TAP is not None:
        try:
            _TAP.close()
        except Exception:
            pass
    _TAP = None
    _TAP_PATH = path


def tap_flush() -> None:
    if _TAP is not None:
        _TAP.flush()


def _tap():
    """The process-wide EventLog, built lazily so import touches no disk."""
    global _TAP
    if _TAP is None:
        try:
            events = _events_module()
            _TAP = events.EventLog(_TAP_PATH or events.default_path())
            _TAP.record_from_headers = events.record_from_headers
        except Exception:
            return None
    return _TAP


def _public_lane_names(config_path: str) -> frozenset[str]:
    """Read the lane names a config marks `model_info: {public: true}`.

    Returns an empty set on any problem, which the middleware treats as
    "filter nothing" — see the fail-open note in the module docstring.
    """
    if not config_path or not os.path.exists(config_path):
        return frozenset()
    try:
        import yaml

        with open(config_path) as handle:
            cfg = yaml.safe_load(handle) or {}
        names = set()
        for entry in cfg.get("model_list") or []:
            if not isinstance(entry, dict):
                continue
            info = entry.get("model_info") or {}
            if isinstance(info, dict) and info.get("public") is True:
                name = entry.get("model_name")
                if isinstance(name, str) and name:
                    names.add(name)
        return frozenset(names)
    except Exception:
        return frozenset()


def filter_catalogue(payload: bytes, public: frozenset[str]) -> bytes | None:
    """Drop non-public entries from an OpenAI model-list body.

    Returns None when the body should be passed through unchanged: not JSON,
    not the shape we expect, nothing marked public, or filtering would empty a
    non-empty catalogue (which would look like a dead proxy to every client).
    """
    if not public:
        return None
    try:
        doc = json.loads(payload)
    except Exception:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        return None

    kept = [
        item for item in doc["data"]
        if isinstance(item, dict) and item.get("id") in public
    ]
    if doc["data"] and not kept:
        # Every advertised name is unmarked: almost certainly a config that
        # predates the marker, not a deliberately empty catalogue.
        return None
    if len(kept) == len(doc["data"]):
        return None

    doc["data"] = kept
    return json.dumps(doc).encode()


class LaneCatalogueFilter:
    """ASGI middleware that filters the model listing and nothing else."""

    def __init__(self, app, public: frozenset[str]) -> None:
        self.app = app
        self.public = public

    async def __call__(self, scope, receive, send):
        # The hot path: anything that is not the model listing is handed over
        # untouched. With FERRY_EVENTS off — the default — there is no wrapper
        # around `send` at all, so a streamed completion is byte-for-byte what
        # litellm produced and the wrapped app receives the caller's own send by
        # identity. With the tap on a wrapper does exist, but it forwards every
        # message unmodified and only READS on the response path — the header
        # list on http.response.start, body LENGTHS on http.response.body —
        # and lib/ferry-front.test.py asserts the two are
        # message-for-message equal.
        if (
            not self.public
            or scope.get("type") != "http"
            or scope.get("path") not in MODEL_LIST_PATHS
        ):
            # Only inference paths are tapped. The catalogue is excluded
            # explicitly because when NO lane is marked public this branch also
            # handles /v1/models; health and metrics are excluded because they
            # are polled every few seconds and are not served model calls.
            if (
                scope.get("type") == "http"
                and is_inference_path(scope.get("path", ""))
                and tap_enabled()
            ):
                return await self.app(scope, receive, self._tapped(scope, send))
            return await self.app(scope, receive, send)

        chunks: list[bytes] = []
        start_message: dict | None = None

        async def capture(message):
            nonlocal start_message
            if message["type"] == "http.response.start":
                start_message = message
                return
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                await self._flush(send, start_message, b"".join(chunks))
                return
            await send(message)

        await self.app(scope, receive, capture)

    def _tapped(self, scope, send):
        """Wrap `send` to read attribution headers off http.response.start and
        count response body bytes off http.response.body.

        Every message is forwarded unmodified — no body is buffered or
        rewritten, no header is changed, no ordering is altered, and the only
        await is the forwarded send. The record is written when the FINAL body
        chunk passes so its `resp_bytes` count is complete; a response that
        never finishes costs its event record, which is the price of counting
        without buffering. Fail-open in every branch: a broken tap must never
        fail, delay, or alter a request.
        """
        rec = None
        nbytes = 0

        async def tapped(message):
            nonlocal rec, nbytes
            mtype = message.get("type")
            if mtype == "http.response.start":
                try:
                    tap = _tap()
                    if tap is not None:
                        client = scope.get("client") or ("", 0)
                        rec = tap.record_from_headers(
                            message.get("headers") or [],
                            client[0] if client else "",
                            scope.get("path", ""),
                            message.get("status", 0),
                        )
                except Exception:
                    pass
            elif mtype == "http.response.body":
                try:
                    nbytes += len(message.get("body", b""))
                except Exception:
                    pass
                if not message.get("more_body"):
                    try:
                        tap = _tap()
                        if tap is not None and rec is not None:
                            rec["resp_bytes"] = nbytes
                            tap.offer(rec)
                    except Exception:
                        pass
            return await send(message)

        return tapped

    async def _flush(self, send, start_message, body: bytes) -> None:
        filtered = filter_catalogue(body, self.public)
        out = body if filtered is None else filtered

        headers = []
        for key, value in (start_message or {}).get("headers", []):
            if key.lower() == b"content-length":
                continue
            headers.append((key, value))
        headers.append((b"content-length", str(len(out)).encode()))

        await send({
            "type": "http.response.start",
            "status": (start_message or {}).get("status", 200),
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": out, "more_body": False})


def should_wrap(public) -> bool:
    """Whether the middleware has any job at all.

    A non-empty public set means catalogue filtering. An enabled tap means
    events. With neither, plain litellm is handed back untouched.

    This exists as its own predicate because `build_app` used to return the raw
    app whenever no lane was marked public — which silently disabled the event
    tap on every config that does not use `model_info: {public: true}`. The unit
    tests could not catch it: they construct LaneCatalogueFilter directly and
    never call build_app, so the object worked while the app never installed it.
    Caught by a live run under the real loader, 2026-08-30.
    """
    return bool(public) or tap_enabled()


def build_app():
    """Import litellm's proxy app and wrap it. Used as the uvicorn app factory.

    litellm resolves its own config from CONFIG_FILE_PATH, so importing its app
    here is the same startup the `litellm` CLI performs — this module adds the
    wrapper and nothing else.
    """
    from litellm.proxy.proxy_server import app as litellm_app

    public = _public_lane_names(os.environ.get("CONFIG_FILE_PATH", ""))
    if not should_wrap(public):
        # Nothing to do — behave exactly like plain litellm.
        return litellm_app
    return LaneCatalogueFilter(litellm_app, public)


def _prepare_multiproc_metrics(port: int) -> None:
    """Point prometheus_client at a fresh multiprocess dir when workers > 1.

    litellm's /metrics serves a MultiProcessCollector when (and only when)
    PROMETHEUS_MULTIPROC_DIR is set (litellm/integrations/prometheus.py,
    _mount_metrics_endpoint, verified 1.97.0). Without it, N workers each
    answer a scrape with their OWN private counters — VictoriaMetrics would
    sample one worker at random and the litellm_* dashboards would
    undercount by ~1/N with counter values flapping between scrapes.

    The dir must be EMPTY at start: stale .db files from a previous run make
    dead series resurface. Cleared here, before uvicorn spawns workers.
    """
    import shutil
    import tempfile

    d = os.environ.get("PROMETHEUS_MULTIPROC_DIR") or os.path.join(
        tempfile.gettempdir(), f"ferry-prom-multiproc-{port}"
    )
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = d


def main(argv: list[str] | None = None) -> int:
    """Serve litellm's proxy with the catalogue filtered.

    Deliberately accepts the same three flags ferry passes to the `litellm`
    CLI, in the same shape, so `pgrep -f '--port N'` still finds this process
    and `ferry down` keeps working. `--workers` mirrors the `litellm` CLI's
    `--num_workers`: litellm's own benchmark guidance is workers = CPU count
    (2 -> 4 instances halved median latency, P95 630ms -> 150ms), but on a LAN
    host a small pool is plenty — the default stays 1 unless ferry passes more.

    Multi-worker state is per-process: cooldowns and usage-based-routing
    counters live in each worker, which for one host is an acceptable blur.
    The event tap's NDJSON appends stay line-atomic across writers; rotation
    is already best-effort.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="ferry_front.py")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)

    os.environ["CONFIG_FILE_PATH"] = args.config
    if args.workers > 1:
        _prepare_multiproc_metrics(args.port)

    import uvicorn

    uvicorn.run(
        "ferry_front:build_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=max(1, args.workers),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
