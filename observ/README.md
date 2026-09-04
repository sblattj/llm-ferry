# ferry observability stack

A local, on-demand **Grafana OSS + VictoriaMetrics + VictoriaLogs** dashboard stack for **llm-ferry**
(the LAN LLM relay). $0 (all OSS), localhost-only, up/down on demand. It coexists with the
g0vs1g desk stack on the same Mac by using distinct ports and a distinct data dir.

| Component | Port | Notes |
|---|---|---|
| ferry-metrics-exporter | **9092** | `/metrics` + `/healthz` (python3 stdlib, nohup daemon) |
| VictoriaMetrics (ferry) | **8429** | TSDB + promscrape; `/health`, `/api/v1/query` |
| VictoriaLogs (ferry) | **9428** | log storage + LogsQL; python3 stdlib shipper tails the proxy log |
| Grafana (ferry) | **3001** | UI; login `admin` / `ferry-observ` |
| litellm proxy (existing) | 8090 | native `/metrics` scraped **on by default** (see "Enable litellm-native metrics") |

## Architecture

```
  proxy access log  ─┐
  (cloud-proxy-8090) │
  litellm /health/liveliness ─┤
  litellm /v1/models          ├──▶ ferry-metrics-exporter :9092 ──▶ VictoriaMetrics :8429 ──▶ Grafana :3001
  ~/.config/ferry/litellm.yaml ┘         (emits ferry_* series)          (TSDB, 15s scrape)      (5 dashboards)
                                                                              ▲
                              litellm :8090/metrics ─────────────────────────┘
                              (ON by default — see "Enable litellm-native metrics")

  proxy access log ──▶ ferry-log-shipper ──▶ VictoriaLogs :9428 ──▶ Grafana :3001
  (cloud-proxy-8090)    (python3 stdlib,        (LogsQL)              (victoriametrics-logs-datasource)
                          tails + ships)
```

The exporter derives every `ferry_*` series from three zero-disruption sources: the proxy
access log (traffic counters), litellm `/health/liveliness` + `/v1/models` (health/serving),
and `~/.config/ferry/litellm.yaml` (topology). It does **not** depend on litellm's native
`/metrics`. Separately, ferry-log-shipper tails the same proxy access log and ships raw log
lines to VictoriaLogs so Grafana can search/filter per-model request logs and surface errors
and fallback events — independent of both the exporter and litellm's `/metrics`.

## Quickstart

```bash
ferry dash --grafana --open          # bring the stack up + open Grafana
# equivalently, directly:
bash observ/bringup.sh --open
bash observ/verify.sh                # smoke-test every layer (PASS/FAIL per check)
```

Grafana opens at **http://127.0.0.1:3001** (login **admin / ferry-observ**). The stack is a
LOCAL surface (127.0.0.1). To reach it from the LAN or tailnet, expose Grafana explicitly —
e.g. `tailscale serve --bg --https=3443 http://127.0.0.1:3001`, or launch Grafana with
`GF_SERVER_HTTP_ADDR=0.0.0.0`.

## Dashboards (Grafana folder "Ferry")

| Dashboard | What it shows |
|---|---|
| **ferry-overview** | Up/serving status, models served, health-check latency, request rate, error rate — the at-a-glance page. |
| **ferry-traffic** | Per-client-IP × HTTP-status request volume, RPS, error rate, backend events (quota_exhausted / rate_limited). |
| **ferry-backends** | Topology (worker-pool size, deployments, fallback-chain length, config mtime) + an "LLM internals" row fed by litellm-native metrics + a **"Failures & Fallbacks"** row (failures by model/reason, cooldowns, fallbacks fired). |
| **ferry-models** | Per-model request rate, tokens, spend, latency, success/failure — from litellm-native metrics. |
| **ferry-logs** | Searchable per-model proxy logs from VictoriaLogs; errors & fallbacks view. |
| **ferry-lanes** | Every hop of every fallback chain, the pools, per-deployment health with the state it is in and how long it has held it, per-lane request rate, and the fallback edges traffic actually took. The lane half reads `litellm.yaml`; the event half needs the front-door tap (below). |

### The front-door event tap (`ferry-lanes` and `ferry dash`'s live view)

The proxy access log records **no model at all** — measured over 13,182 real
records — so nothing derived from it can say which deployment served a request.
The tap is what supplies that: an ASGI middleware on the inference path reads
litellm's own response headers (`x-litellm-model-group`, `x-litellm-model-id`,
`x-litellm-attempted-fallbacks`, `x-litellm-fallback-errors`) and appends one
NDJSON line per request. It is **off by default** and forwards every message
unmodified when on.

```bash
FERRY_EVENTS=on ferry up                   # arm the tap
ferry dash                                 # live view: lanes, chains, per-request feed
ferry-metrics-exporter                     # picks the stream up at its default path
```

Both readers default to `${TMPDIR}/ferry-logs/ferry-events.ndjson`; override with
`--events`. The writer is bounded (64MB with rotation, a 2048-deep non-blocking
queue) and announces any drop in the stream itself, which the exporter surfaces
as `ferry_events_dropped_total` — a silently overflowing tap would make every
other number on the lanes dashboard an undercount.

**Per-deployment health needs a classifier table you write yourself.** Copy
`event-rules.example.json` to `~/.config/ferry/event-rules.json` and replace the
placeholder wording with what your providers actually say; it is data rather
than code so a vendor's name, its error wording and its plan terms stay out of
this repo. With no table every failure classifies as `unknown` — visible, never
silently `healthy`.

## Halt

```bash
ferry dash --grafana --down          # stop the stack (data preserved)
# equivalently:
bash observ/teardown.sh              # stop daemons, keep state
bash observ/teardown.sh --purge      # stop + delete the TSDB / Grafana DB / provisioning / logs
```

## Where data lives

Everything runtime lives under **`~/.config/ferry/observ/`** (ferry convention):

```
~/.config/ferry/observ/
├── vm-data/                  VictoriaMetrics TSDB (retention 12 months)
├── grafana-data/             Grafana SQLite DB (dashboards, users)
├── grafana-provisioning/     materialized provisioning tree (envsubst'd; runtime-only)
├── logs/                     vm.log · exporter.log · grafana.log · Grafana logs
├── vm.pid · exporter.pid · grafana.pid
```

The repo-tracked provisioning (`observ/grafana/provisioning/**`) carries `${VM_URL}`,
`${FERRY_ALERT_WEBHOOK}`, `${GF_SECURITY_ADMIN_USER}` tokens; `bringup.sh` materializes them
via `envsubst` into `grafana-provisioning/` so a real webhook/admin value never touches the
repo. Grafana is always launched pointed at the materialized copy.

## Metric catalog

The exporter emits exactly the `ferry_*` metric set defined in [**CONTRACT.md**](CONTRACT.md)
(§ "Metric contract") — meta (`ferry_exporter_up`, `ferry_scrape_timestamp_seconds`, …), health/serving
(`ferry_up`, `ferry_health_check_latency_ms`, `ferry_models_served`, `ferry_model_info`),
traffic counters (`ferry_requests_total`, `ferry_backend_events_total`, …), and topology
(`ferry_worker_pool_size`, `ferry_deployment_info`, `ferry_fallback_chain_length`,
`ferry_route_config_mtime_seconds`). CONTRACT.md is the single source of truth for the exact
names, labels, and semantics that dashboards and alerts consume.

## Enable litellm-native metrics (on by default)

The **ferry-backends** "LLM internals"/"Failures & Fallbacks" rows and the **ferry-models**
dashboard show request-latency histograms, token counts, per-deployment success/failure,
spend, and fallback events — sourced directly from litellm's own `/metrics` endpoint, **not**
from our exporter. `litellm-route-example.yaml` ships with the prometheus callback already
set, and `ferry install` bundles the `prometheus_client` dep it needs, so this works
**out of the box** for any route config seeded from the template:

```yaml
litellm_settings:
  callbacks: ["prometheus"]
```

VictoriaMetrics scrapes `127.0.0.1:8090/metrics` (the `litellm` job in
`observ/victoriametrics/scrape.yml`), so the LLM-internals and ferry-models panels populate
on the next 15s scrape — no observ-stack restart needed.

To **disable** it (e.g. to shed the extra `/metrics` overhead), remove the `callbacks` line
from `~/.config/ferry/litellm.yaml` and restart the proxy:

```bash
ferry down && ferry up --route
```

With it off, `litellm:8090/metrics` 404s again and the `litellm_*`-fed panels go blank; the
`ferry_*` panels never depend on any of this — they keep working regardless.

> Note: some litellm builds gate parts of the prometheus exporter behind their enterprise
> tier, so a subset of `litellm_*` metrics may still be absent.

## Log search — VictoriaLogs

The **ferry-logs** dashboard and the logs panel in **ferry-backends** are backed by
**VictoriaLogs** (`:9428`), populated by a small python3-stdlib shipper (`ferry-log-shipper`)
that tails the proxy access log and ships each line. Grafana reaches it via the
**`victoriametrics-logs-datasource`** plugin, auto-installed at bringup through
`GF_INSTALL_PLUGINS` — no manual plugin install step required.
