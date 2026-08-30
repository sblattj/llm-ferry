# Live request observability — design

**Date:** 2026-08-29
**Status:** approved, not yet implemented
**Scope:** per-request event capture, a live lane/hop/pool view in `ferry dash`,
per-deployment exhaustion state, and the metrics + alerting to match

## Problem

Three questions cannot be answered from the ferry stack today.

1. **What is moving through the relay right now, and where is it going?**
2. **Which providers are exhausted, and since when?**
3. **What do the fallback chains and pools actually look like?**

The stack is not short of instrumentation — VictoriaMetrics, VictoriaLogs, five
Grafana dashboards, nine alert rules, a metrics exporter and a log shipper all
run today. It is short of *one record per request*. Everything above is either
an aggregate counter sampled every 15s or a regex guess over log prose.

### Evidence

**No per-request attribution exists.** The log shipper infers the `model` field
by matching regexes against arbitrary log text (`observ/ferry-log-shipper`,
`MODEL_PATTERNS` / `KNOWN_MODEL`). Queried live on 2026-08-29, the newest record
carrying a populated `model` field has an `_msg` consisting of nothing but that
same model name indented by four spaces, and its neighbours are litellm
`register_model:` *startup warnings*. The field is scraping model names out of text that has
nothing to do with a request. The underlying proxy access log is uvicorn's, whose
lines carry client IP, method, path and status and **no model at all**.

**Exhaustion is one global slot.** `ferry-dash:172-180` matches two hardcoded
substrings — `"permission_error"` + `"usage limit"` → `kimi_quota`, and
`"rate_limit"` / `"ratelimiterror"` → `rate_limit` — and writes the result to a
single `self.last_event` field that each new event overwrites. One backend's 429
therefore erases another backend's still-live quota outage from the display.
There is no per-deployment state anywhere in the stack. The banner text is also a
hardcoded string naming one specific vendor and status code, rather than what the
provider actually said.

**The topology metrics are unlabelled scalars.** Queried live on 2026-08-29:
`ferry_fallback_chain_length` is 1 series, no labels, value `3` — the `orch`
chain only. `ferry_worker_pool_size` is 1 series, no labels, value `1`. The live
config has **19 `model_name` deployments across 8 fallback chains**; seven of
those chains are invisible, and there is no series anywhere that names a hop.

**Three alerts are firing right now, and none of them can say what happened.**
Grafana's rule API on 2026-08-29 reports:

```
Key pool shrank                    firing
Orchestrator fallback fired        firing
Deployment cooled down             firing
Backend rate-limit storm           inactive  (health: nodata)
```

Each is a symptom with no attached detail. *Orchestrator fallback fired* is
`sum(increase(litellm_deployment_successful_fallbacks_total[5m])) > 0` — it
proves a fallback happened somewhere in the last five minutes and names neither
the lane, the hop that failed, nor why. *Deployment cooled down* is the same
shape. Real failover is occurring on this host and the stack cannot show which
edge was traversed.

*Key pool shrank* is `ferry_worker_pool_size < 2` for 10m. The live config
contains no pooled `model_name` at all, so the metric sits at 1 and the rule has
no non-firing state to return to — it is measuring a config shape the deployment
no longer has.

*Backend rate-limit storm* sits in `nodata` because
`ferry_backend_events_total{kind="rate_limit"}` is omitted entirely while its
count is zero (`observ/ferry-metrics-exporter:298-301`), and the count only
moves when one of the two hardcoded substrings above matches a log line.

## The finding this design rests on

**litellm already emits a complete per-request attribution record in its
response headers, and nothing in ferry reads it.**

Verified two ways.

*Traced.* `ProxyBaseLLMRequestProcessing.get_custom_headers`
(`litellm/proxy/common_request_processing.py:1016`) builds
`x-litellm-call-id` (:1061), `x-litellm-model-id` (:1062),
`x-litellm-model-name` (:1063), `x-litellm-model-api-base` (:1065) and
`x-litellm-response-duration-ms` (:1082). Every call site splats
`**additional_headers` into it (:1158 non-streaming, :1868 streaming, :2046,
:2131), and `additional_headers` is where the router puts
`x-litellm-attempted-fallbacks` and `x-litellm-fallback-errors`
(`litellm/router_utils/add_retry_fallback_headers.py:241` and the
`FallbackErrorInfo` TypedDict at :7-11, fields `message`, `type`, `param`,
`code`). The streaming branch is `common_request_processing.py:1852-1868`, so
these are set on SSE responses too — they land at ASGI `http.response.start`,
before the first token.

*Observed.* A real request through the live proxy on port 8090 (local lane,
zero cost) returned:

```
x-litellm-call-id:              c290a7f2-6501-4256-bd09-8aad0894f611
x-litellm-model-group:          local-sub
x-litellm-model-id:             local-sub-mlx
x-litellm-model-name:           openai/mlx-community/<local-model>
x-litellm-model-api-base:       http://127.0.0.1:8093/v1
x-litellm-attempted-fallbacks:  0
x-litellm-attempted-retries:    0
x-litellm-response-duration-ms: 335.734
x-litellm-overhead-duration-ms: 1.049
```

`x-litellm-model-group` is the lane the client asked for; `x-litellm-model-id`
is the deployment that served. Those two fields plus `attempted-fallbacks` and
`fallback-errors` are the whole problem statement, per request, already on the
wire.

## Constraints

- **`ferry_front.py`'s hot path must not buffer.** The file exists partly to
  avoid Starlette's `BaseHTTPMiddleware` (`front/ferry_front.py:27-31`), and
  `__call__` currently returns early for every non-catalogue request with the
  comment "No wrapper around `send`, so a streamed completion is byte-for-byte
  what litellm produced" (`front/ferry_front.py:112-114`). This design **adds a
  `send` wrapper to that path**, which is a real change to a documented
  property. See *Rollout gate*.
- **`ferry-dash` is stdlib-only by contract** — its docstring requires it to run
  under any `python3` with no venv and no pip. Anything it imports inherits that:
  no PyYAML in the topology parser.
- **The scrape floor is 15s** (`observ/victoriametrics/scrape.yml`), for both the
  `ferry-exporter` and `litellm` jobs. Nothing sourced from VictoriaMetrics can
  be live in the sense this design means.
- **`ferry_requests_total` has no backfill** — it is an in-memory counter that
  starts at exporter boot.
- **`observ/CONTRACT.md`'s one-writer-per-file ownership table is stale.** It
  predates `ferry-log-shipper`, `ferry-logs.json`, `ferry-models.json` and
  `provisioning/datasources/ferry-vlogs.yml`, none of which it lists. This work
  claims a new `live-events` seat rather than assuming an existing owner.
- **A concurrent agent owns the `ferry dash` Routes panel**
  (`docs/superpowers/specs/2026-08-29-ferry-route-editor-design.md`, approved,
  not yet implemented). Both features need the same lane/hop/pool model. The
  resolution is the shared module below, not two parsers.

## Design

### The event record

One NDJSON line per completed request:

```json
{"t":"2026-08-29T19:31:07.421Z","call_id":"c290a7f2-…","lane":"flash",
 "deployment":"flash-alt-1","model":"someprovider/some-model",
 "provider":"someprovider","api_base":"https://api.example.invalid/v1",
 "status":200,"fallbacks":1,"retries":0,
 "hop_errors":[{"code":"429","type":"RateLimitError","message":"…"}],
 "duration_ms":3841.2,"overhead_ms":1.049,"cost":0.00042,
 "client_ip":"192.168.1.44","path":"/v1/chat/completions"}
```

Deployment and provider names in every example here are synthetic. The real
values come from the host's own gitignored `~/.config/ferry/litellm.yaml` at
runtime and are deliberately not written into this repo.

`lane` is `x-litellm-model-group`; `deployment` is `x-litellm-model-id`;
`provider` is derived from the `model` prefix, falling back to the `api_base`
host. `hop_errors` is the parsed `x-litellm-fallback-errors` array.

**Not in the record: token counts.** The headers do not carry them and the
response body is never read. Tokens remain the 15s metrics pipeline's job
(`litellm_total_tokens_metric_total` and friends). This is a deliberate
limitation of the header-tap approach, stated so no panel is built assuming
otherwise.

### The tap

`front/ferry_front.py` gains an `EventTap`. On the hot path it wraps `send` with
a function that forwards **every** message unchanged and, only when
`message["type"] == "http.response.start"`, reads the header list and offers a
record to a queue.

The property to preserve is not "no wrapper" — the tap is a wrapper — but:

- every message is passed to the real `send` unmodified, including `body`,
  `more_body`, and message ordering;
- the wrapper performs no I/O and no `await` other than the forwarded `send`;
- `content-length` is never touched (unlike `_flush`, which rewrites it for the
  catalogue path).

The tap never reads the request body, so `receive` is not wrapped at all. The
lane comes from a response header, not from parsing what the client sent.

Writes are fire-and-forget: a bounded `queue.Queue` and one daemon thread that
drains it and appends to `${TMPDIR:-/tmp}/ferry-logs/ferry-events.ndjson`
(alongside `cloud-proxy-8090.log`, per `lib/ferry-core.zsh` `LOG_DIR`). A full
queue drops the oldest record rather than blocking a request. Rotation is a size
cap with rename-and-reopen; consumers already detect rotation by `(st_dev,
st_ino)` plus a head fingerprint (`observ/ferry-log-shipper`, `Tailer`).

**Fail-open, always** — the file's existing doctrine. Every tap operation is
inside `try/except`; repeated failures disable the tap for the process. A
request is never failed, delayed, or altered because event capture broke.
Kill switch: `FERRY_EVENTS=off`.

### Shared topology module

New `ferrylib/topology.py`, standard library only:

```
Lane{name, public, hops:[Hop]}
Hop{name, deployments:[Deployment], is_pool}
Deployment{model_name, model, model_id, provider, api_base}
```

A hop is one name. A name is one deployment, **or several deployments sharing
that name** — which is the pool. Ordered chains come from
`router_settings.fallbacks`; pools come from repeated `model_name` in
`model_list`. The two compose: any hop in a chain may fan out into a pool. This
is the same model the route-editor spec defines, so the editor writes it and
this design renders it.

`ferry-dash` keeps a `load_topology` shim delegating to the module, because
`observ/ferry-metrics-exporter` imports that function out of `ferry-dash` by
`importlib` and the route editor references it.

### Live view

New `ferrylib/live.py`. `ferry-dash` gains an import, two routes, and a mount
point — a deliberately small diff, since another agent is editing that file.

- `GET /events` — SSE, tailing the NDJSON from EOF
- `GET /topology` — parsed lanes plus per-deployment state

Public lanes (`model_info: {public: true}`) render by default, expandable to all
19 backends; the back-compat duplicates — three lane names that resolve to one
underlying deployment — would otherwise dominate the view. Each lane draws its
ordered hops left to right, a pooled hop as a fan-out with per-member traffic
share.

Edges animate per request. A record with `fallbacks: 1` lights hop 0 red with
its `hop_errors[0].code` and hop 1 green — so a `flash` request failing its
primary with a 429 and landing on the next hop is visible as it happens, not
inferred from a counter. Below the graph, a scrolling feed of the same events.

### Exhaustion

A per-deployment state machine, keyed by `model_id`:

`healthy · rate_limited · quota_exhausted · auth_dead · unreachable`

Inputs: event `status` and `hop_errors`, plus
`litellm_deployment_cooled_down_total`, `litellm_deployment_state`, and
`litellm_remaining_requests_metric` where a provider populates it (only one
provider does today — 5 series).

Classification is a provider-keyed table matching on status code, error type and
message substring — a `permission_error` paired with usage-limit wording, a
`RESOURCE_EXHAUSTED` code, an insufficient-balance message, a 402. **The table
itself is data, not code**, loaded from the host's own config so that adding a
provider does not mean editing this repo, and so that no vendor's identity or
plan terms are committed here. An error that matches no row classifies as
`unknown`, never as `healthy`.

Two properties that fix current defects:

- **Per deployment**, so one backend's 429 cannot erase another's live outage.
- **`quota_exhausted` is sticky** until a 2xx on that deployment. A weekly quota
  does not clear on the 60s decay that suits a rate limit.

The UI shows **the provider's own error text verbatim** alongside the classified
state. A window is displayed only when the provider names one. "Weekly" is not
inferred from a status code.

### Metrics and Grafana

`observ/ferry-metrics-exporter` gains, from the topology and the event stream:

| metric | labels |
|---|---|
| `ferry_lane_hop` | `lane, position, hop, deployment, model, provider, pool_size` |
| `ferry_lane_chain_length` | `lane` |
| `ferry_pool_size` | `hop` |
| `ferry_deployment_state` | `deployment, provider, state` |
| `ferry_deployment_state_since_seconds` | `deployment` |
| `ferry_events_total` | `lane, deployment, provider, outcome` |
| `ferry_fallback_edges_total` | `lane, from_deployment, to_deployment, code` |

`ferry_worker_pool_size` and `ferry_fallback_chain_length` are retained
unchanged so existing dashboards and alert rules keep working.

`observ/ferry-log-shipper` ships the structured event instead of regex-guessing
`model` / `requested_model`, which is what fixes the polluted field.

New dashboard `observ/grafana/dashboards/ferry-routes.json` (uid `ferry-routes`,
datasource `ferry-vm`): topology table, per-lane chain health, fallback-edge
table, exhaustion timeline.

New alert rules:

- any deployment in `quota_exhausted` for > 5m
- **a lane whose entire chain is exhausted** — the real outage condition, and
  currently unalertable because no per-lane series exists
- a pool member absent from a hop whose `pool_size` was previously higher

`ferry-key-pool-shrank` is rewritten against `ferry_pool_size{hop}` so it
measures a hop that actually shrank rather than the global maximum.

## Hypotheses — verify before relying on

Written as hypotheses because the code path has not been read.

1. **Attribution headers on the error path.** When every hop fails and the proxy
   returns 5xx, it is *not established* that `get_custom_headers` runs. Only the
   success paths were traced. Test by fault injection against a throwaway
   instance with `mock_testing_fallbacks`. If the headers are absent, error
   events carry `lane: "unknown"` and are attributed from
   `litellm_proxy_failed_requests_metric_total{requested_model}` instead.
2. **`x-litellm-model-group` universality.** Observed on one successful local
   request. Whether it is present for every provider and both streaming modes is
   unverified. Same test covers it; the fallback is the same.
3. **`hop_errors` ordering.** The design assumes `fallback_errors[i]` corresponds
   to hop `i` of the chain. `add_retry_fallback_headers.py` appends errors in
   order, but the mapping to *chain position* was not traced end to end. If it
   does not hold, edges are drawn from counts only and the per-hop error code is
   dropped.

## Validation

- every `lane` in an event resolves to a real `model_name` in the topology
- every `deployment` resolves to a real `model_info.id`
- an event whose `fallbacks` count exceeds its lane's chain length is logged as
  a topology/observation mismatch rather than silently rendered
- the tap's dropped-record counter is exported, so a queue that is overflowing is
  visible rather than quietly lossy

## Testing

- **Pass-through fidelity.** Synthetic ASGI `send` messages through the tap;
  assert body chunk boundaries, `more_body` flags, message order and headers are
  byte-identical to input. **This is the test that gates the rollout.** Control:
  a deliberately mutating wrapper must fail it.
- **Header mapping.** Records built from a streaming `http.response.start`, a
  fallback response carrying `x-litellm-fallback-errors`, and a response with no
  `x-litellm-*` headers at all. The last must emit a record with
  `lane: "unknown"`, not drop it.
- **Topology parse** against a copy of the real 19-backend config: all 8 chains,
  correct hop order, `public` flags. Control that must fail: a config with two
  deployments sharing a `model_name` must report `is_pool` and `pool_size 2` —
  the live config has no pool, so without this control the pool code path is
  never exercised.
- **Classifier fixtures** covering each error *shape*; an unmatched payload must
  land on `unknown`. Fixtures use synthetic vendor names, model ids and hosts
  (`someprovider`, `some-model`, `example.invalid`) — a tracked test file in a
  public repo is as published as the README, and real values have reached one
  before by being "just test data".
- **End to end.** Drive a genuine fallback through a throwaway instance with
  `general_settings.dangerously_allow_mock_testing_request_params: true` and
  `mock_testing_fallbacks` — never against the LAN-facing proxy — and assert the
  event stream reproduces the exact hop path.
- **Load.** The tap under sustained streaming traffic, asserting no growth in
  response latency percentiles against a control run with `FERRY_EVENTS=off`.

## Rollout gate

The tap ships **off by default** behind `FERRY_EVENTS=on`. It is flipped on by
default only after both:

1. the pass-through fidelity test passes with its control failing, and
2. a real streaming session runs clean through it end to end.

This is because the change touches the single process every request crosses, and
it removes a property the file documents about itself
(`front/ferry_front.py:112-114`).

## v1 scope

Per-request event capture; the live lane/hop/pool view with animated edges and a
request feed; per-deployment exhaustion state with verbatim provider text; the
labelled topology and event metrics; the `ferry-routes` dashboard and the new
alert rules.

**Out of scope:** live token counts per request (the headers do not carry them —
would require the callback-plugin approach, which runs inside litellm's venv and
can fail a request); historical backfill of events predating the tap; editing
routes, which is the concurrent route-editor spec's job.

## File ownership

Claims a new `live-events` seat, since `observ/CONTRACT.md`'s table is stale.

| File | Note |
|---|---|
| `front/ferry_front.py` | the tap |
| `ferrylib/topology.py` | new, shared with the route editor |
| `ferrylib/live.py` | new |
| `observ/ferry-metrics-exporter` | new metric families |
| `observ/ferry-log-shipper` | structured events replace regex guessing |
| `observ/grafana/dashboards/ferry-routes.json` | new |
| `observ/grafana/provisioning/alerting/rules.yml` | new rules + pool-alert fix |
| `ferry-dash` | **shared** — shim plus two routes and a mount point only |
