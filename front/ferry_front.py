"""litellm's proxy app with the lane catalogue filtered.

WHY THIS EXISTS

`/v1/models` advertises every real `model_name` in the config, which on the
ferry stack means the fallback deployments as well as the named lanes. That is
not cosmetic. `router_settings.fallbacks` is keyed by model group: `flash` has
an entry, `flash-luna` does not. A client that picks a fallback hop out of a
model list gets a single provider with nothing behind it — the exact opposite of
what the `flash` lane exists to provide, and it fails only when that hop is
down, which is the case the chain was built for.

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
import tempfile
import time

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


# ── lane-name confidentiality ────────────────────────────────────────────────
# litellm restamps the response body's `"model"` to the client-requested lane
# name (1.99.0: `_override_openai_response_model` / `_restamp_streaming_chunk_model`),
# so the body already honours the lane abstraction. The HEADERS do not: every
# response carries x-litellm-model-name (the real provider model, e.g.
# "chatgpt/responses/gpt-5.6-sol"), -model-api-base (the provider URL), -model-id, and
# -model-group. litellm emits them unconditionally in get_custom_headers with no
# config toggle, so hiding them is a wrapper job.
#
# We strip the four IDENTITY headers plus the whole llm_provider-* family, on
# inference paths only, and only for clients that are not on this host. The
# llm_provider-* headers are the upstream provider's own response headers
# forwarded with a prefix (litellm get_custom_headers): `llm_provider-server:
# cloudflare`, a set-cookie whose Domain names the vendor (`<vendor>.com`), the
# Cloudflare ray id + PoP — the same identity leak one layer down.
#
# What we deliberately KEEP, because consumers use it for optimization:
#   - prompt caching: lives in the BODY (usage.cached_tokens /
#     cache_read_input_tokens / prompt_tokens_details), not headers — unaffected.
#   - rate-limit / backoff hints: llm_provider-x-ratelimit-*,
#     llm_provider-retry-after, and any plain retry-after. A client that backs
#     off on 429s (opencode does) reads these; stripping them would turn a
#     clean backoff into hammering.
#   - x-litellm cost/timing headers (response-cost, duration-ms, …): no identity.
# The event tap reads the SAME headers off http.response.start before the
# strip, so ferry's observability (lib/ferry_events.py) keeps full attribution.
# The loopback exemption is for ferry-dash's probe_backends, which reads
# x-litellm-model-name over 127.0.0.1 to show which deployment answered.
STRIP_RESPONSE_HEADERS = frozenset({
    b"x-litellm-model-name",
    b"x-litellm-model-id",
    b"x-litellm-model-api-base",
    b"x-litellm-model-group",
})

# Within the llm_provider-* family, these substrings mark a header worth keeping:
# rate-limit windows and retry hints that drive client backoff.
_KEEP_PROVIDER_SUBSTRINGS = (b"ratelimit", b"rate-limit", b"retry-after")


def _strip_header(name: bytes) -> bool:
    """Whether a response header name should be hidden from an off-host client."""
    n = name.lower()
    if n in STRIP_RESPONSE_HEADERS:
        return True
    if n.startswith(b"llm_provider-") or n.startswith(b"llm-provider-"):
        return not any(s in n for s in _KEEP_PROVIDER_SUBSTRINGS)
    if n == b"retry-after":
        return False
    return False

_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def _is_loopback_client(scope) -> bool:
    client = scope.get("client")
    return bool(client) and client[0] in _LOOPBACK_CLIENTS


def _strip_headers(message):
    """Return the http.response.start message minus the identity headers."""
    headers = [(k, v) for k, v in message.get("headers", [])
               if not _strip_header(k)]
    out = dict(message)
    out["headers"] = headers
    return out


def strip_headers_enabled() -> bool:
    """On by default; FERRY_STRIP_HEADERS=0/off disables (a client that greps
    the headers for debugging can opt back in)."""
    return (os.environ.get("FERRY_STRIP_HEADERS") or "1").strip().lower() not in (
        "0", "off", "false", "no")

# ── fallback-chain hot-swap ────────────────────────────────────────────────
# Reordering a lane's fallbacks must not need `ferry reload`: restarting the
# front door drops every in-flight request, while a reorder is one list
# assignment on litellm's Router. litellm reads router.fallbacks per REQUEST
# (router.py: `kwargs.get("fallbacks", self.fallbacks)` inside the call path),
# so swapping the attribute is live for the very next request — no restart, no
# dropped connections, in-flight requests keep the old chain to completion.
#
# The write half is atomic: every lane in the request is validated against the
# CURRENT router state first, and only when all lanes pass does any assignment
# happen — a rejected reorder changes nothing. Validation is the same three
# rules litellm itself enforces at Router boot plus the ferry lane rule:
# every hop must be a model_name the router serves, no hop twice, a lane never
# its own fallback. Unknown hops are REFUSED, never skipped: litellm's boot
# validator raises on a bad chain, so silently accepting one here would drift
# the live state away from anything the config could produce.
#
# Control surface: GET /v1/ferry/chains reads the live chains, POST
# /v1/ferry/reorder writes them. Loopback-only (same rule as the dash's header
# exemption): these mutate routing, so the LAN never reaches them. litellm's
# own auth still applies first — litellm resolves auth before this middleware
# ever runs, so without the bearer the request is a 401 from litellm itself.
# Paths are /v1/ferry/* on purpose: is_inference_path has no /v1/ferry prefix,
# so the event tap never records a reorder as a served request.
REORDER_CHAINS_PATH = "/v1/ferry/chains"
REORDER_PATH = "/v1/ferry/reorder"


def _live_router():
    """The running proxy's Router, or None (tests, import without boot).

    Imported lazily: this module loads without litellm installed (the unit
    tests import it offline), and at import time the proxy has not built its
    router yet anyway. The global lives on litellm.proxy.proxy_server, which
    is where cmd_reload's process already keeps it."""
    try:
        from litellm.proxy import proxy_server
        return proxy_server.llm_router
    except Exception:
        return None


def parse_reorder_body(raw: bytes):
    """(chains, error) from a POST /v1/ferry/reorder body.

    Accepts the dash's unified order shape ({order: {lane: [primary, ...]}})
    and the bare chains shape ({chains: {lane: [...]}}). Anything else is a
    400 with the reason, never a guess: an order whose position 0 is not the
    lane itself would silently re-point the primary if coerced."""
    try:
        doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return None, "body is not JSON"
    if not isinstance(doc, dict):
        return None, "body must be a JSON object"
    if isinstance(doc.get("order"), dict):
        chains = {}
        for lane, seq in doc["order"].items():
            if not isinstance(seq, list):
                return None, "order[%r] must be a list" % (lane,)
            if not seq:
                return None, "order[%r] may not be empty" % (lane,)
            if seq[0] != lane:
                return None, (
                    "primary changes not yet supported: %r would become the "
                    "primary of lane %r" % (seq[0], lane))
            chains[lane] = list(seq[1:])
        return chains, None
    if isinstance(doc.get("chains"), dict):
        chains = doc["chains"]
        for lane, seq in chains.items():
            if not isinstance(seq, list):
                return None, "chains[%r] must be a list of model names" % (lane,)
            if any(not isinstance(h, str) for h in seq):
                return None, "chains[%r] must be a list of model names" % (lane,)
        return {lane: list(seq) for lane, seq in chains.items()}, None
    return None, "body needs an 'order' or 'chains' object"


def validate_reorder(chains, router_names) -> list:
    """Every reason to refuse a hot-swap, as human-readable strings.

    router_names is the set of model_names the live proxy serves — NOT the
    file's groups: the router is the thing that will execute the chain, so a
    hop it does not know would be skipped silently at 2am, exactly the failure
    validate_chains guards against for the file path."""
    errs = []
    for lane, seq in chains.items():
        if lane not in router_names:
            errs.append("lane %r is not served by the running proxy" % (lane,))
            continue
        if lane in seq:
            errs.append("lane %r lists itself as its own fallback" % (lane,))
        seen = set()
        for hop in seq:
            if hop not in router_names:
                errs.append("hop %r (in %s) is not served by the running proxy"
                            % (hop, lane))
            if hop in seen:
                errs.append("hop %r appears twice in %s" % (hop, lane))
            seen.add(hop)
    return errs


def chain_signature(router) -> list:
    """The router's live fallbacks as a sorted [[lane, hops]] list.

    Sorted so two workers serving the same chains produce the same body, and a
    list (not a dict) so the GET response has a stable key order to eyeball."""
    out = []
    for entry in getattr(router, "fallbacks", None) or []:
        if isinstance(entry, dict):
            for lane, hops in entry.items():
                out.append([lane, list(hops)])
    out.sort()
    return out


def service_reorder(router, chains):
    """Validate-then-assign on the live router. Returns (ok, errors).

    The all-or-nothing half: validation runs over EVERY lane before the first
    assignment, so a three-lane reorder with one bad hop leaves all three
    chains exactly as they were. Assignment keeps litellm's own shape —
    [{lane: hops}] — so anything downstream reading router.fallbacks sees what
    a boot from the same config would have built."""
    names = set(getattr(router, "model_group_alias", None) or {})
    try:
        groups = router.get_model_groups() if hasattr(router, "get_model_groups") else []
        names.update(g if isinstance(g, str) else g.get("model_name", "") for g in groups or [])
    except Exception:
        pass
    if not names:
        try:
            names.update(d.get("model_name", "") for d in
                         (getattr(router, "model_list", None) or [])
                         if isinstance(d, dict))
        except Exception:
            pass
    names.discard("")
    errs = validate_reorder(chains, names)
    if errs:
        return False, errs
    live = {lane: list(hops) for entry in (router.fallbacks or [])
            if isinstance(entry, dict) for lane, hops in entry.items()}
    live.update(chains)
    router.fallbacks = [{lane: hops} for lane, hops in live.items()]
    return True, []


# ── primary hot-swap ───────────────────────────────────────────────────────
# A reorder only moves the TAIL (positions 1..n); the primary (position 0) is
# a deployment's own model_name, and "promoting" a fallback used to mean
# ferry reload. It no longer does, and the operation is NOT a params-reseat
# but a BACKEND SWAP between the two names: the lane's deployment and the
# hop's deployment trade litellm_params (each under a FRESH id), names and
# chains untouched.
#
# The swap shape is what makes it safe. A reseat (hop params under the lane
# name, old primary deleted) would orphan the demoted backend: re-adding it
# under the lane's own name is a self-fallback, and inventing a new name for
# it breaks every chain referencing the old ones. A swap loses nothing: after
# promoting flash-luna to flash, flash's chain [flash-luna] still resolves —
# that hop now serves the demoted Gemini backend, so the effective order is
# Luna -> Gemini with zero chain edits. Cross-lane side effect is symmetric
# and honest: every OTHER chain naming the hop would get the demoted backend
# in that slot too — under the 2026-09-04 config no lane shares a hop (flash's
# is flash-luna, super-flash's is super-flash-luna, one apiece), so there is
# nothing for the effect to touch today, but the mechanism still applies the
# moment two chains name the same hop — and the dash shows model strings from
# the file, so after the file echo the new mapping is VISIBLE.
#
# litellm resolves the primary per REQUEST (_get_all_deployments reads
# model_name_to_deployment_indices -> model_list[idx] fresh on every call),
# and upsert/delete_deployment maintain every index they touch, so the swap
# is live for the very next request — in-flight requests finish on the old
# backend; only the already-picked attempt keeps it.
#
# THE ONE RULE, from the per-worker-state audit: every id-keyed cache
# (cooldowns, allowed-fails, usage counters, provider SDK clients) is keyed
# by model_info.id. BOTH backends move, so BOTH arrive under fresh ids —
# service_promote mints (or freshness-checks) two ids, one per name.
#
# Same shape as service_reorder: (ok, errors, ids), validate-everything-
# first, and the file writer MUST echo the swap into litellm.yaml's
# model_list — litellm re-reads the config on reconcile and would evict a
# memory-only swap, and the dash parses the file. The dash applies
# file-first-then-live with the SAME ids, so any failure converges via
# restart instead of diverging.
PROMOTE_CHAINS_PATH = "/v1/ferry/promote/preview"
PROMOTE_PATH = "/v1/ferry/promote"


def _deployment_dict(router, model_name):
    """The live deployment dict backing a lane, or None.

    get_deployment_by_model_group_name returns the FIRST deployment for the
    group — which is the primary by construction (one deployment per lane in
    this config; a pooled lane promotes its first member, same as serving)."""
    try:
        dep = router.get_deployment_by_model_group_name(model_name)
    except Exception:
        return None
    if dep is None:
        return None
    try:
        return dep.to_json(exclude_none=True) if hasattr(dep, "to_json") else dict(dep)
    except Exception:
        return None


def _mint_promote_id(name):
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "%s-promoted-%s" % (name, ts)


def _fresh_promote_id(candidate, taken):
    """Whether a caller-supplied promote id is safe to use.

    Fresh means: a non-empty string that is not any deployment id the live
    router currently serves. Anything else inherits id-keyed state (cooldowns,
    usage, provider clients) and is refused."""
    return (isinstance(candidate, str) and bool(candidate)
            and candidate not in taken)


def parse_promote_body(raw: bytes):
    """(lane, hop, ids, error) from a POST /v1/ferry/promote body.

    {lane, hop}: swap the backends behind the two names. The hop must already
    be in the lane's live CHAIN — promotion never invents a backend, it only
    re-seats one the chain already trusts. Optional {lane_id, hop_id}: the ids
    the swapped deployments will serve under — used ONLY by the dash's
    file-first apply, which mints them, writes them into litellm.yaml, then
    sends the same ones so file and router agree. A caller-supplied id that
    is already live is refused (it would inherit that deployment's cooldowns,
    usage, and provider client); omitted ids are minted server-side.
    Anything else is a 400/409, never a guess about which provider string
    the caller meant."""
    try:
        doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return None, None, None, "body is not JSON"
    if not isinstance(doc, dict):
        return None, None, None, "body must be a JSON object"
    lane, hop = doc.get("lane"), doc.get("hop")
    if not isinstance(lane, str) or not lane:
        return None, None, None, "body needs a 'lane' string"
    if not isinstance(hop, str) or not hop:
        return None, None, None, "body needs a 'hop' string"
    if lane == hop:
        return None, None, None, "lane %r is already its own primary" % (lane,)
    ids = {k: doc.get(k) for k in ("lane_id", "hop_id")
           if isinstance(doc.get(k), str) and doc.get(k)}
    return lane, hop, ids, None


def _live_ids(router) -> set:
    """Every model_info.id the live router currently serves."""
    out = set()
    try:
        for d in getattr(router, "model_list", None) or []:
            info = (d.get("model_info") if isinstance(d, dict)
                    else getattr(d, "model_info", None)) or {}
            ident = info.get("id") if isinstance(info, dict) else getattr(info, "id", None)
            if ident:
                out.add(ident)
    except Exception:
        pass
    return out


def validate_promote(router, lane, hop, ids=None) -> list:
    """Every reason to refuse a primary swap, as human-readable strings."""
    errs = []
    lane_dep = _deployment_dict(router, lane)
    if lane_dep is None:
        errs.append("lane %r is not served by the running proxy" % (lane,))
        return errs
    hop_dep = _deployment_dict(router, hop)
    if hop_dep is None:
        errs.append("hop %r is not served by the running proxy — promotion "
                    "only re-seats a backend the proxy already runs" % (hop,))
        return errs
    if ((lane_dep.get("model_info") or {}).get("id") ==
            (hop_dep.get("model_info") or {}).get("id")):
        errs.append("lane %r and hop %r share one deployment — nothing to swap"
                    % (lane, hop))
    chain = [h for entry in (getattr(router, "fallbacks", None) or [])
             if isinstance(entry, dict) for l, hs in entry.items()
             if l == lane for h in hs]
    if hop not in chain:
        errs.append("hop %r is not in %s's fallback chain — promote only "
                    "moves a trusted hop, never an unlisted backend" % (hop, lane))
    taken = _live_ids(router)
    for key in ("lane_id", "hop_id"):
        cand = (ids or {}).get(key)
        if cand is not None and not _fresh_promote_id(cand, taken):
            errs.append("%r %r is already served — a reused id inherits that "
                        "deployment's cooldowns, usage, and provider client"
                        % (key, cand))
    return errs


def service_promote(router, lane, hop, ids=None):
    """Swap the backends behind lane and hop. Returns (ok, errors, id_map).

    Upsert BOTH swapped deployments under fresh ids, THEN evict both old ids:
    neither lane is ever without a backend if an add throws. Chains are
    untouched — the demoted backend keeps serving under the HOP's name, so
    the lane's existing chain order stays meaningful with zero edits.
    id_map {lane: fresh_lane_id, hop: fresh_hop_id} is returned so the file
    writer echoes the SAME ids — the live router and the file must agree, or
    the dash misattributes hops and the next reconcile evicts the swap."""
    errs = validate_promote(router, lane, hop, ids)
    if errs:
        return False, errs, None
    lane_dep = _deployment_dict(router, lane)
    hop_dep = _deployment_dict(router, hop)
    old_lane_id = (lane_dep.get("model_info") or {}).get("id")
    old_hop_id = (hop_dep.get("model_info") or {}).get("id")
    if not old_lane_id or not old_hop_id:
        return False, ["both deployments need a model_info.id to evict"], None
    ids = ids or {}
    fresh_lane = ids.get("lane_id") or _mint_promote_id(lane)
    fresh_hop = ids.get("hop_id") or _mint_promote_id(hop)
    if fresh_lane == fresh_hop:
        return False, ["lane_id and hop_id must differ"], None
    lane_params = dict(hop_dep.get("litellm_params") or {})
    hop_params = dict(lane_dep.get("litellm_params") or {})
    lane_info = dict(lane_dep.get("model_info") or {})
    hop_info = dict(hop_dep.get("model_info") or {})
    lane_info["id"] = fresh_lane
    hop_info["id"] = fresh_hop
    try:
        # The Deployment import is local so this module still imports without
        # litellm installed (the offline unit tests).
        try:
            from litellm.types.router import Deployment
            mk = lambda name, params, info: Deployment(
                model_name=name, litellm_params=params, model_info=info)
        except Exception:
            mk = lambda name, params, info: type("NewDeployment", (), {
                "model_name": name, "litellm_params": params,
                "model_info": info})()
        router.upsert_deployment(deployment=mk(lane, lane_params, lane_info))
        router.upsert_deployment(deployment=mk(hop, hop_params, hop_info))
        router.delete_deployment(id=old_lane_id)
        router.delete_deployment(id=old_hop_id)
    except Exception as e:
        return False, ["primary swap failed: %s: %s" % (type(e).__name__, e)], None
    return True, [], {"lane": fresh_lane, "hop": fresh_hop}


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


# ── fleets ────────────────────────────────────────────────────────────────
# A fleet is a complete routing set — one deployment (plus its hops) per cloud
# lane — living in the SAME litellm.yaml as every other fleet, distinguished by
# a prefix on the model_name: `domestic.heavy`, `international.flash-or`. The
# set of fleets is DISCOVERED from those prefixes; there is no registry and no
# second file to drift. A client keeps sending the bare lane name and the
# resolver below rewrites it, so switching fleets needs no reload and no client
# restart.
#
# FAIL-OPEN, same doctrine as the catalogue filter: a config with no dotted
# names discovers no fleets, and with no fleets the resolver is a no-op that
# passes every model name through untouched.
CLOUD_LANES = ("heavy", "flash", "super-flash")
LOCAL_LANES = frozenset({"local-orch", "local-sub"})
LEGACY_HEAVY = frozenset({"orch", "orchestrator"})
FLEET_HEADER = b"x-ferry-fleet"
CLIENT_HEADER = b"x-ferry-client"
FLEET_PATH = "/v1/ferry/fleet"
FLEET_STATE_ENV = "FERRY_FLEETS"
HOST_IDENTITY = "host"


def fleet_state_path(config_path: str) -> str:
    """Where the per-client selections live: beside litellm.yaml by default.

    FERRY_FLEETS overrides it, which is how the unit tests and a second host
    instance keep their state out of the real ~/.config/ferry."""
    override = (os.environ.get(FLEET_STATE_ENV) or "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(config_path or ""), "fleets.json")


def discover_fleets(config_path: str) -> dict:
    """{fleet: {lane: primary model string}} read out of the routing file.

    Every `model_name` containing a "." is split at the FIRST "." into
    (fleet, lane); the value is `litellm_params.model` of the FIRST deployment
    carrying that name, which is the primary by construction (the same rule
    _deployment_dict relies on). Names without a "." — the shared local GPU
    lanes — are ignored. Any read or parse problem returns {}: no fleets means
    the resolver is a no-op, which is the pre-fleets behaviour."""
    if not config_path or not os.path.exists(config_path):
        return {}
    try:
        import yaml

        with open(config_path) as handle:
            cfg = yaml.safe_load(handle) or {}
        out: dict = {}
        for entry in cfg.get("model_list") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("model_name")
            if not isinstance(name, str) or "." not in name:
                continue
            fleet, lane = name.split(".", 1)
            if not fleet or not lane:
                continue
            params = entry.get("litellm_params") or {}
            model = params.get("model") if isinstance(params, dict) else ""
            lanes = out.setdefault(fleet, {})
            if lane not in lanes:
                lanes[lane] = model if isinstance(model, str) else ""
        return out
    except Exception:
        return {}


def fleet_gaps(fleets: dict) -> list:
    """Human-readable complaints about fleets missing a cloud lane.

    Logged to stderr at startup rather than raised: a typo in one lane of one
    fleet degrades that lane (its requests 400 with the same message) instead
    of taking the whole front door down."""
    out = []
    for fleet, lanes in (fleets or {}).items():
        for lane in CLOUD_LANES:
            if lane not in (lanes or {}):
                out.append("fleet %r has no lane %r" % (fleet, lane))
    return out


class FleetStateError(Exception):
    """The selection file exists and cannot be understood.

    Loud on purpose: falling through to the default would silently move every
    client's lane set, which is exactly the surprise this feature must not
    produce."""


class FleetState:
    """The per-client fleet selections, read from and written to one file.

    The front door runs four uvicorn workers, so process memory cannot be the
    truth. Each instance caches the parsed document and the file's
    st_mtime_ns, and re-reads whenever that moves — one os.stat per request,
    which is cheaper than the json parse it usually avoids.

    A MISSING file is not an error: it means "nobody has chosen anything yet",
    whose default is the first fleet in discovery order (which is file order,
    dicts having kept insertion order since 3.7). It is written on the first
    mutation.
    """

    def __init__(self, path: str, fleets: dict) -> None:
        self.path = path
        self.fleets = fleets or {}
        self._doc = None
        self._mtime = None

    def _first_fleet(self) -> str:
        for name in self.fleets:
            return name
        return ""

    def _empty(self) -> dict:
        return {"default": self._first_fleet(), "clients": {}}

    def load(self) -> dict:
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            self._doc = self._empty()
            self._mtime = None
            return self._doc
        except OSError as exc:
            raise FleetStateError("%s: %s" % (self.path, exc))
        if self._doc is not None and self._mtime == mtime:
            return self._doc
        try:
            with open(self.path) as handle:
                doc = json.load(handle)
        except Exception as exc:
            raise FleetStateError("%s: %s" % (self.path, exc))
        if not isinstance(doc, dict):
            raise FleetStateError("%s: not a JSON object" % (self.path,))
        default = doc.get("default")
        clients = doc.get("clients")
        self._doc = {
            "default": default if isinstance(default, str) and default
            else self._first_fleet(),
            "clients": {k: v for k, v in (clients or {}).items()
                        if isinstance(k, str) and isinstance(v, str)}
            if isinstance(clients, dict) else {},
        }
        self._mtime = mtime
        return self._doc

    def default(self) -> str:
        return self.load()["default"]

    def selection_for(self, identity: str):
        return self.load()["clients"].get(identity)

    def _write(self, doc: dict) -> dict:
        """tmp + os.replace, so a concurrent worker never reads a half file.

        The tmp name is UNIQUE PER WRITER, not `path + ".tmp"`: with four
        workers, a shared name lets writer B truncate A's already-fsynced tmp
        before A's os.replace, so A would rename B's bytes into place while A's
        cache still held A's document."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Same directory as the target, so os.replace stays same-filesystem.
        fd, tmp = tempfile.mkstemp(dir=directory or ".",
                                   prefix=os.path.basename(self.path) + ".")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(doc, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._doc = doc
        try:
            self._mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            self._mtime = None
        return doc

    def set_selection(self, identity: str, fleet) -> dict:
        doc = dict(self.load())
        clients = dict(doc["clients"])
        if fleet is None:
            clients.pop(identity, None)
        else:
            clients[identity] = fleet
        doc["clients"] = clients
        return self._write(doc)

    def set_default(self, fleet: str) -> dict:
        doc = dict(self.load())
        doc["clients"] = dict(doc["clients"])
        doc["default"] = fleet
        return self._write(doc)

    def document(self, identity: str) -> dict:
        doc = self.load()
        return {
            "you": identity,
            "fleet": doc["clients"].get(identity) or doc["default"],
            "default": doc["default"],
            "fleets": self.fleets,
            "clients": dict(doc["clients"]),
        }


class ResolveError(Exception):
    """A fleet or lane named by a request does not exist.

    Answered as HTTP 400 with the message verbatim. Nothing ever falls through
    to the default on a bad name: silently serving a different lineup than the
    one asked for is the one failure mode this feature must not have."""


def _header_text(headers: dict, name: bytes) -> str:
    value = (headers or {}).get(name)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return ""
    return value.strip() if isinstance(value, str) else ""


def caller_identity(scope, headers: dict) -> str:
    """Who is asking: X-Ferry-Client > 'host' on loopback > the peer IP.

    A headerless client reaching the host through `tailscale serve` arrives
    from loopback and is therefore 'host'. That is documented, not worked
    around: regenerated client configs carry the header."""
    named = _header_text(headers, CLIENT_HEADER)
    if named:
        return named
    scope = scope or {}
    if _is_loopback_client(scope):
        return HOST_IDENTITY
    client = scope.get("client")
    return str(client[0]) if client else ""


def resolve_model(model: str, header_fleet: str, identity: str,
                  state: "FleetState") -> str:
    """The bare lane name a client sent, rewritten to a real fleet lane.

    Precedence, first match wins (spec §4): an explicit fleet prefix, a local
    lane, the X-Ferry-Fleet header, the caller's sticky selection, the
    host-wide default. `orch`/`orchestrator` are folded into `heavy` first.
    Anything that is not a cloud lane after that fold passes through untouched
    so litellm answers it exactly as it does today."""
    if not isinstance(model, str) or not model:
        return model
    fleets = state.fleets
    if not fleets:
        # Fail-open: a config with no dotted names discovers no fleets, and
        # with no fleets there is nothing to resolve to. Without this, the
        # default falls back to the empty string and every cloud lane 400s.
        return model
    if "." in model and model.split(".", 1)[0] in fleets:
        return model
    if model in LOCAL_LANES:
        return model
    lane = "heavy" if model in LEGACY_HEAVY else model
    if lane not in CLOUD_LANES:
        return model
    fleet = (header_fleet or "").strip() or state.selection_for(identity) or state.default()
    if fleet not in fleets:
        raise ResolveError("unknown fleet %r; fleets: %s"
                           % (fleet, ", ".join(fleets)))
    if lane not in fleets[fleet]:
        raise ResolveError("fleet %r has no lane %r" % (fleet, lane))
    return "%s.%s" % (fleet, lane)


def rewrite_body_model(body: bytes, model: str) -> bytes:
    """The request body with its top-level `model` replaced.

    Compact separators because the body is regenerated anyway and every byte
    is re-sent upstream; the ASGI content-length is recomputed by the caller
    from what this returns.

    Caller contract: only call this when the body is a JSON OBJECT carrying a
    top-level string `model`, and wrap the call in `except Exception` — a
    non-JSON body raises, and an object without a `model` key silently GAINS
    one rather than being left alone."""
    doc = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    doc["model"] = model
    return json.dumps(doc, separators=(",", ":")).encode()


def synthesize_catalogue(payload: bytes, public: frozenset, fleets: dict,
                         fleet: str) -> "bytes | None":
    """filter_catalogue, plus a bare entry per public cloud lane of `fleet`.

    A client on `international` lists heavy/flash/super-flash (its own, bare)
    alongside every public fleet lane, so `ferry opencode`'s catalogue check,
    `ferry status` and host-reset's verifier keep matching bare names while a
    curious client can still see and pin a specific fleet lane.

    `fleets` is RESERVED and intentionally unused: what gets a bare entry is
    `public` ∩ CLOUD_LANES ∩ upstream-advertised, and since discovery and the
    upstream catalogue both derive from the same `model_list` they cannot
    diverge. Do not gate on it.

    Same fail-open contract as filter_catalogue: None means "send the upstream
    bytes untouched"."""
    if not public:
        return None
    try:
        doc = json.loads(payload)
    except Exception:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        return None
    kept = [item for item in doc["data"]
            if isinstance(item, dict) and item.get("id") in public]
    if doc["data"] and not kept:
        return None
    bare = []
    by_id = {item["id"]: item for item in kept}
    for lane in CLOUD_LANES:
        name = "%s.%s" % (fleet, lane)
        if name in public and name in by_id:
            entry = dict(by_id[name])
            entry["id"] = lane
            bare.append(entry)
    if not bare and len(kept) == len(doc["data"]):
        return None
    doc["data"] = bare + kept
    return json.dumps(doc).encode()


def _bearer_ok(headers: dict) -> bool:
    """Whether a control-plane request carries the configured master key.

    No key configured means no check — the same posture litellm itself takes,
    and the host's own loopback tooling relies on it."""
    key = (os.environ.get("LITELLM_MASTER_KEY") or "").strip()
    if not key:
        return True
    auth = _header_text(headers, b"authorization")
    parts = auth.split(None, 1)
    return len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == key


_FLEET_WARNED: dict = {}
FLEET_WARN_INTERVAL = 60.0


def _fleet_warn(err, stream=None, clock=None) -> bool:
    """One stderr line per distinct error per FLEET_WARN_INTERVAL seconds.

    The resolver fails OPEN on a broken fleets.json, so without this the
    degradation is invisible; with a per-request line it would be a flood on
    the hot path. Returns True when a line was written. Never raises."""
    try:
        key = (type(err).__name__, str(err))
        now = (clock or time.monotonic)()
        last = _FLEET_WARNED.get(key)
        if last is not None and now - last < FLEET_WARN_INTERVAL:
            return False
        _FLEET_WARNED[key] = now
        print("ferry-front: fleet resolution failed open: %s: %s" % key,
              file=stream or sys.stderr)
        return True
    except Exception:
        return False


def _header_map(scope) -> dict:
    """ASGI request headers as {lowercased name: value}, last value wins."""
    out = {}
    try:
        for key, value in scope.get("headers") or []:
            out[bytes(key).lower()] = bytes(value)
    except Exception:
        return {}
    return out


def _set_content_length(scope, length: int) -> None:
    """Replace content-length in a live ASGI scope after a body rewrite.

    litellm's stack reads the length from the scope, not from our replayed
    receive, so a rewrite that grows the body by the fleet prefix would be
    truncated to the original length without this.

    `transfer-encoding` goes too: the body has been buffered and is replayed as
    one non-chunked message, so leaving it beside a fresh content-length would
    put an invalid header pair in the scope.
    """
    try:
        headers = [(k, v) for k, v in (scope.get("headers") or [])
                   if bytes(k).lower() not in (b"content-length",
                                               b"transfer-encoding")]
        headers.append((b"content-length", str(length).encode()))
        scope["headers"] = headers
    except Exception:
        pass


class LaneCatalogueFilter:
    """ASGI middleware that filters the model listing and nothing else."""

    def __init__(self, app, public: frozenset[str], fleets=None, state=None) -> None:
        self.app = app
        self.public = public
        # `fleets` is the discovery map {fleet: {lane: provider model}}; `state`
        # is the FleetState over fleets.json. BOTH default to the pre-fleets
        # behaviour: with `state is None` not one byte of a request is read or
        # rewritten, which is what keeps every test written before this feature
        # passing unchanged.
        self.fleets = fleets or {}
        self.state = state

    async def __call__(self, scope, receive, send):
        # The control plane first: GET /v1/ferry/chains, POST /v1/ferry/reorder,
        # and POST /v1/ferry/promote are answered HERE, before litellm ever sees
        # the request — litellm has no such routes and would 404 them, and more
        # importantly its auth layer would bill them as unknown model calls.
        # Loopback-only: mutating routing from the LAN is a non-starter.
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if path == FLEET_PATH:
            # Unlike the reorder/promote routes this one is deliberately
            # LAN-reachable: a client has to be able to move ITSELF. The
            # blast radius is bounded by identity — a POST can only ever
            # write the caller's own entry — and `default: true`, the one
            # host-wide write, is still loopback-only.
            headers = _header_map(scope)
            if not _bearer_ok(headers):
                return await self._reply(
                    send, 401, {"errors": ["bearer required"]})
            if self.state is None:
                return await self._reply(
                    send, 503, {"errors": ["no fleets in this config"]})
            method = scope.get("method", "GET").upper()
            identity = caller_identity(scope, headers)
            if method == "GET":
                try:
                    return await self._reply(
                        send, 200, self.state.document(identity))
                except Exception as err:
                    return await self._reply(send, 503, {"errors": [str(err)]})
            if method != "POST":
                return await self._reply(
                    send, 405, {"errors": ["use GET or POST on %s" % FLEET_PATH]})
            raw = await self._read_body(receive, send)
            if raw is None:
                return
            try:
                doc = json.loads(raw)
            except Exception:
                doc = None
            bad = {"error": {"message": 'body needs a "fleet" key whose value '
                                        'is a fleet name or null',
                             "type": "ferry_fleet"}}
            if not isinstance(doc, dict) or "fleet" not in doc:
                return await self._reply(send, 400, bad)
            fleet = doc.get("fleet")
            if fleet is not None and not isinstance(fleet, str):
                return await self._reply(send, 400, bad)
            as_default = doc.get("default") is True
            if as_default and not _is_loopback_client(scope):
                return await self._reply(send, 403, {"error": {
                    "message": "the default is the host's to set",
                    "type": "ferry_fleet"}})
            if fleet is not None and fleet not in self.fleets:
                return await self._reply(send, 400, {"error": {
                    "message": "unknown fleet %r; fleets: %s" % (
                        fleet, ", ".join(self.fleets)),
                    "type": "ferry_fleet"}})
            if as_default and fleet is None:
                return await self._reply(send, 400, {"error": {
                    "message": "the default needs a fleet name, not null",
                    "type": "ferry_fleet"}})
            try:
                if as_default:
                    self.state.set_default(fleet)
                else:
                    self.state.set_selection(identity, fleet)
                return await self._reply(
                    send, 200, self.state.document(identity))
            except Exception as err:
                return await self._reply(send, 503, {"errors": [str(err)]})
        if path in (REORDER_CHAINS_PATH, REORDER_PATH,
                    PROMOTE_CHAINS_PATH, PROMOTE_PATH):
            if not _is_loopback_client(scope):
                return await self._reply(
                    send, 403, {"errors": ["ferry control plane is loopback-only"]})
            if path == REORDER_CHAINS_PATH:
                if scope.get("method", "GET").upper() != "GET":
                    return await self._reply(
                        send, 405, {"errors": ["use GET for chains"]})
                return await self._reply(
                    send, 200, {"chains": chain_signature(_live_router())})
            if path == PROMOTE_CHAINS_PATH:
                # Preview: what a promote WOULD do, without touching anything.
                # The dash shows this before the user confirms.
                if scope.get("method", "GET").upper() != "POST":
                    return await self._reply(
                        send, 405, {"errors": ["use POST for promote preview"]})
                body = await self._read_body(receive, send)
                if body is None:
                    return
                lane, hop, ids, err = parse_promote_body(body)
                if err is not None:
                    return await self._reply(send, 400, {"errors": [err]})
                router = _live_router()
                if router is None:
                    return await self._reply(
                        send, 503, {"errors": ["proxy router not ready"]})
                errs = validate_promote(router, lane, hop, ids)
                if errs:
                    return await self._reply(send, 409, {"errors": errs})
                lane_dep = _deployment_dict(router, lane)
                hop_dep = _deployment_dict(router, hop)
                return await self._reply(send, 200, {
                    "ok": True,
                    "lane": lane, "hop": hop,
                    "old_lane_model": (lane_dep.get("litellm_params") or {}).get("model"),
                    "old_hop_model": (hop_dep.get("litellm_params") or {}).get("model"),
                    "note": "Confirm to swap: the two backends trade names "
                            "under fresh ids; chains are untouched.",
                })
            if path in (REORDER_PATH, PROMOTE_PATH) and \
                    scope.get("method", "GET").upper() != "POST":
                return await self._reply(
                    send, 405, {"errors": ["use POST here"]})
            if path == PROMOTE_PATH:
                body = await self._read_body(receive, send)
                if body is None:
                    return
                lane, hop, ids, err = parse_promote_body(body)
                if err is not None:
                    return await self._reply(send, 400, {"errors": [err]})
                router = _live_router()
                if router is None:
                    return await self._reply(
                        send, 503, {"errors": ["proxy router not ready"]})
                ok, errs, id_map = service_promote(router, lane, hop, ids)
                if not ok:
                    return await self._reply(send, 409, {"errors": errs})
                return await self._reply(send, 200, {
                    "ok": True, "lane": lane, "hop": hop,
                    "ids": id_map,
                    "chains": chain_signature(router),
                    "note": "Live backends swapped (chains untouched); the "
                            "config file is unchanged — Apply in ferry-dash "
                            "writes the same swap (same ids %s) into "
                            "litellm.yaml so a restart keeps it." % (id_map,),
                })
            # REORDER_PATH from here on.
            body = await self._read_body(receive, send)
            if body is None:
                return
            chains, err = parse_reorder_body(body)
            if err is not None:
                return await self._reply(send, 400, {"errors": [err]})
            router = _live_router()
            if router is None:
                return await self._reply(
                    send, 503, {"errors": ["proxy router not ready"]})
            ok, errs = service_reorder(router, chains)
            if not ok:
                return await self._reply(send, 409, {"errors": errs})
            return await self._reply(send, 200, {
                "ok": True, "chains": chain_signature(router),
                "note": "Live chains updated; the config file is unchanged — "
                        "Apply in ferry-dash also writes litellm.yaml so a "
                        "restart keeps this order.",
            })
        # Fleet resolution. This runs BEFORE the hot-path handover so the tap
        # and the header strip below still see — and record — the REWRITTEN
        # request: metrics must group by `international.heavy`, not by `heavy`.
        # Only `receive` is replaced; `send` is untouched, so the streamed
        # response path gains no Python.
        if (self.state is not None and scope.get("type") == "http"
                and is_inference_path(path)):
            receive = await self._fleet_rewrite(scope, receive, send)
            if receive is None:
                return
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
            # Only inference paths are tapped and header-stripped. The catalogue
            # is excluded explicitly because when NO lane is marked public this
            # branch also handles /v1/models; health and metrics are excluded
            # because they are polled every few seconds and are not served model
            # calls. The strip is for lane-name confidentiality; it is skipped
            # for loopback clients (ferry-dash's control plane reads the headers).
            http = scope.get("type") == "http"
            infer = http and is_inference_path(scope.get("path", ""))
            strip = (
                infer
                and strip_headers_enabled()
                and not _is_loopback_client(scope)
            )
            if infer and tap_enabled():
                return await self.app(scope, receive, self._tapped(scope, send, strip))
            if strip:
                return await self.app(scope, receive, self._stripping(send))
            return await self.app(scope, receive, send)

        fleet = self._catalogue_fleet(scope)
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
                await self._flush(send, start_message, b"".join(chunks), fleet)
                return
            await send(message)

        await self.app(scope, receive, capture)

    async def _read_body(self, receive, send):
        """Read a full request body, or reply 400 and return None."""
        body = b""
        try:
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body"):
                    return body
        except Exception:
            await self._reply(
                send, 400, {"errors": ["could not read request body"]})
            return None

    async def _fleet_rewrite(self, scope, receive, send):
        """Resolve this request's fleet and return a one-shot replay `receive`.

        Returns None when a reply has already been sent (a 400 for an unknown
        fleet), in which case the caller must return immediately. The body is
        fully buffered here — chat bodies are not streamed uploads — and every
        failure mode short of an unreadable socket replays the ORIGINAL bytes.
        """
        body = await self._read_body(receive, send)
        if body is None:
            return None
        out = body
        try:
            doc = json.loads(body)
        except Exception:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("model"), str):
            headers = _header_map(scope)
            header_fleet = headers.get(FLEET_HEADER, b"").decode(
                "utf-8", "replace").strip()
            try:
                resolved = resolve_model(
                    doc["model"], header_fleet,
                    caller_identity(scope, headers), self.state)
            except ResolveError as err:
                await self._reply(send, 400, {"error": {
                    "message": str(err.args[0]), "type": "ferry_fleet"}})
                return None
            except Exception as err:
                # FleetStateError or anything else: fail open, exactly as an
                # unreadable litellm.yaml does for the catalogue — but say so
                # once per interval, or the degradation is invisible.
                _fleet_warn(err)
                resolved = None
            if resolved and resolved != doc["model"]:
                try:
                    out = rewrite_body_model(body, resolved)
                except Exception:
                    out = body
        if out != body:
            _set_content_length(scope, len(out))

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": out,
                        "more_body": False}
            return await receive()

        return replay

    def _catalogue_fleet(self, scope):
        """The fleet whose lanes this caller's bare names should mean.

        Same precedence as the inference path, minus the model-shaped rules:
        header > sticky > default. Returns None — meaning "just filter, as
        before" — for any unknown name or unreadable state, because a catalogue
        read must never be the thing that takes the front door down.
        """
        if self.state is None or not self.fleets:
            return None
        try:
            headers = _header_map(scope)
            fleet = headers.get(FLEET_HEADER, b"").decode(
                "utf-8", "replace").strip()
            if not fleet:
                fleet = self.state.selection_for(
                    caller_identity(scope, headers)) or self.state.default()
            return fleet if fleet in self.fleets else None
        except Exception as err:
            # Symmetric with _fleet_rewrite: failing open is fine, failing open
            # SILENTLY is not — a permanently broken fleets.json would degrade
            # every listing with no signal. _fleet_warn rate-limits to one line
            # per distinct error per interval, so the polled catalogue path
            # cannot turn this into a flood. An unknown fleet NAME does not come
            # through here: that is an expected fallback, not a degradation.
            _fleet_warn(err)
            return None

    async def _reply(self, send, status, doc):
        body = json.dumps(doc).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body,
                    "more_body": False})

    def _stripping(self, send):
        """Wrap `send` to drop the identity headers on http.response.start.

        Only the start message is touched, and only its header list — bodies
        forward by identity, so a streamed token is never funnelled through a
        rewrite. Fail-open: any error forwards the original message.
        """

        async def stripping(message):
            if message.get("type") == "http.response.start":
                try:
                    message = _strip_headers(message)
                except Exception:
                    pass
            return await send(message)

        return stripping

    def _tapped(self, scope, send, strip=False):
        """Wrap `send` to read attribution headers off http.response.start and
        count response body bytes off http.response.body.

        The record is built from the headers BEFORE the optional strip, so
        observability keeps full attribution even when the client sees fewer
        headers. Bodies are never buffered or rewritten and no ordering is
        altered; the only awaits are the forwarded sends. The record is written
        when the FINAL body chunk passes so its `resp_bytes` count is complete;
        a response that never finishes costs its event record, which is the
        price of counting without buffering. Fail-open in every branch: a
        broken tap must never fail, delay, or alter a request.
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
                if strip:
                    try:
                        message = _strip_headers(message)
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

    async def _flush(self, send, start_message, body: bytes, fleet=None) -> None:
        if fleet is None:
            filtered = filter_catalogue(body, self.public)
        else:
            filtered = synthesize_catalogue(body, self.public, self.fleets, fleet)
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
    events. Header stripping (on by default) means lane-name confidentiality.
    The reorder hot-swap always needs the middleware: POST /v1/ferry/reorder
    and GET /v1/ferry/chains are answered in LaneCatalogueFilter.__call__,
    before the request ever reaches litellm — so a config that needs none of
    the other three still wraps, or the dash loses its one no-restart write
    path. With none of the four, plain litellm is handed back untouched.

    This exists as its own predicate because `build_app` used to return the raw
    app whenever no lane was marked public — which silently disabled the event
    tap on every config that does not use `model_info: {public: true}`. The unit
    tests could not catch it: they construct LaneCatalogueFilter directly and
    never call build_app, so the object worked while the app never installed it.
    Caught by a live run under the real loader, 2026-08-30.
    """
    return bool(public) or tap_enabled() or strip_headers_enabled() or True


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
