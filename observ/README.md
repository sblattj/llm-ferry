# ferry observability stack

A local, on-demand **Grafana OSS + VictoriaMetrics** dashboard stack for **llm-ferry**
(the LAN LLM relay). $0 (all OSS), localhost-only, up/down on demand. It coexists with the
g0vs1g desk stack on the same Mac by using distinct ports and a distinct data dir.

| Component | Port | Notes |
|---|---|---|
| ferry-metrics-exporter | **9092** | `/metrics` + `/healthz` (python3 stdlib, nohup daemon) |
| VictoriaMetrics (ferry) | **8429** | TSDB + promscrape; `/health`, `/api/v1/query` |
| Grafana (ferry) | **3001** | UI; login `admin` / `ferry-observ` |
| litellm proxy (existing) | 8090 | native `/metrics` scraped **opt-in** (off by default) |

## Architecture

```
  proxy access log  ─┐
  (cloud-proxy-8090) │
  litellm /health/liveliness ─┤
  litellm /v1/models          ├──▶ ferry-metrics-exporter :9092 ──▶ VictoriaMetrics :8429 ──▶ Grafana :3001
  ~/.config/ferry/litellm.yaml ┘         (emits ferry_* series)          (TSDB, 15s scrape)      (3 dashboards)
                                                                              ▲
                              litellm :8090/metrics ─────────────────────────┘
                              (OPT-IN; off by default — see "Enable litellm-native metrics")
```

The exporter derives every `ferry_*` series from three zero-disruption sources: the proxy
access log (traffic counters), litellm `/health/liveliness` + `/v1/models` (health/serving),
and `~/.config/ferry/litellm.yaml` (topology). It does **not** depend on litellm's native
`/metrics` (404 until you opt in).

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
| **ferry-traffic** | Per-client-IP × HTTP-status request volume, RPS, error rate, backend events (kimi_quota / rate_limit). |
| **ferry-backends** | Topology (worker-pool size, deployments, fallback-chain length, config mtime) + an OPT-IN "LLM internals" row fed by litellm-native metrics. |

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

## Enable litellm-native metrics (optional)

The **ferry-backends** dashboard's "LLM internals" row shows request-latency histograms,
token counts, per-deployment success/failure, and spend — sourced directly from litellm's
own `/metrics` endpoint, **not** from our exporter. It is off by default (litellm's
`/metrics` returns 404 until the prometheus callback is enabled). To light it up:

1. Add the prometheus callback under `litellm_settings:` in `~/.config/ferry/litellm.yaml`:
   ```yaml
   litellm_settings:
     callbacks: ["prometheus"]
   ```
2. Restart the proxy so it picks up the config:
   ```bash
   ferry down && ferry up --route
   ```

VictoriaMetrics already scrapes `127.0.0.1:8090/metrics` (the `litellm` job in
`observ/victoriametrics/scrape.yml`), so once the callback is on, the LLM-internals panels
begin populating on the next 15s scrape — no observ-stack restart needed.

> Note: some litellm builds gate parts of the prometheus exporter behind their enterprise
> tier, so a subset of `litellm_*` metrics may still be absent. The `ferry_*` panels never
> depend on any of this — they keep working regardless.
