# ferry observability stack — build contract (the interface every seat consumes)

**Goal:** a Grafana OSS + VictoriaMetrics dashboard stack for **llm-ferry** (the LAN LLM
relay), in the same style/richness as the g0vs1g desk stack, launched from the ferry CLI
via **`ferry dash --grafana`**. Local (localhost), on-demand (up/down), $0 (all OSS).

**Reference implementation — READ IT, adapt from it:** the g0vs1g stack lives on disk at
`/Users/sblatt/fin/g0vs1g-desk/deploy/observ/`. Its `grafana/grafana.ini`,
`grafana/provisioning/**`, `mac/bringup.sh`, and dashboard JSONs are the proven template —
mirror their structure and the validated dataviz palette. Adapt names/ports/metrics for ferry.

**Preflight facts (established — do NOT re-check):**
- ferry is a zsh+python LAN LLM relay (litellm 1.97.0 proxy on **:8090**, local MLX optional).
  Repo: `~/code/llm-ferry`. You work in the worktree `~/code/llm-ferry/.claude/worktrees/observ`
  on branch `ferry-observ`. Do NOT run git or `build.zsh`; the orchestrator regenerates `ferry`.
- brew already has `grafana 13.2.0` + `victoriametrics 1.150.0` installed (shared binaries).
- The g0vs1g stack is ALSO running on this Mac at :3000/:8428/:9847 — ferry MUST use different
  ports and a different data dir so they never collide.
- litellm's native `/metrics` is currently **404** (prometheus callback off). Our exporter does
  NOT depend on it. litellm metrics are an OPT-IN enrichment (see § litellm-native below).
- Data sources available with zero disruption: the proxy access log, litellm `/health/liveliness`
  + `/v1/models`, and `~/.config/ferry/litellm.yaml`.

---

## Ports (fixed contract) — distinct from g0vs1g's 3000/8428/9847 and ferry's 8090/8091/8095-8097

| Component | Port |
|---|---|
| ferry-metrics-exporter | **9092** (`/metrics` + `/healthz`) |
| VictoriaMetrics (ferry) | **8429** |
| Grafana (ferry) | **3001** |
| litellm proxy (existing) | 8090 (native `/metrics` scraped opt-in) |

## Grafana datasource UID (fixed): `ferry-vm` · url `http://127.0.0.1:8429`

## State dir (ferry convention): `~/.config/ferry/observ/`
`{vm-data, grafana-data, grafana-provisioning (materialized), logs, *.pid}` all under it.

## Data-source paths the exporter reads (from ferry-core.zsh)
- Proxy access log: `${TMPDIR:-/tmp}/ferry-logs/cloud-proxy-8090.log` (auto-discover like ferry-dash's `find_log`; on macOS TMPDIR is under `/var/folders/*/*/T/`). The exporter MUST reuse ferry-dash's discovery + `Activity` log parser (see `~/code/llm-ferry/ferry-dash`).
- litellm base: `http://127.0.0.1:8090` (`/health/liveliness`, `/v1/models`; bearer key default `local`).
- Route config: `~/.config/ferry/litellm.yaml` (topology: the `orch` lane + its fallbacks, the `flash` pool, and the two local GPU lanes `local-orch`/`local-sub`).

### The proxy log's lifecycle across a restart (why the shipper can go dark)

`ferry up` gives each relaunch a **fresh log inode** (`_ferry_reset_log` unlinks the
old file) and opens it in **append** mode, and it **waits** for the previous litellm
to exit (`_ferry_stop_litellm`) before doing either. All three parts are load-bearing
and none may be simplified back to a plain `> "$cloud_log"`:

- litellm drops its listener seconds before its last log write, so a port check
  reports "free" while uvicorn is still writing `Application shutdown complete`.
- Those straggler writes go out on an fd whose offset is already tens of KB in. If
  the launch truncated that same inode first, they land past the truncation point
  and punch a **sparse hole of NUL bytes** that restores the file's size, while the
  newly launched process writes underneath it from offset 0 and never reaches EOF.
- The log's mtime then ticks on every request while its **size never moves**, so any
  consumer that attaches at EOF — `ferry-log-shipper`, `ferry-dash` — sits forever
  past everything being written. Every process stays healthy; the pipeline is dark.

Consumers MUST detect rotation by **inode**, not by size (the shipper does: `log
rotated (inode changed) — rereading from byte 0`). Regression tests:
`python3 lib/ferry-serve.test.py`. Observed 2026-08-26: a 91,956-byte log pinned at
exactly 91,956 bytes through 20 minutes of live traffic.

The access-log line format (reuse this regex verbatim): `(\d+\.\d+\.\d+\.\d+):\d+ - "(\w+) (\S+) [^"]*" (\d+)` → (ip, method, path, status). Count only `path.startswith("/v1/chat/completions")` as a real inference request (skip `/v1/models` + `/health` polls). The log carries NO latency/token fields — those come only from litellm-native metrics.

---

## Metric contract — the exporter emits EXACTLY these (`ferry_` prefix). Dashboards/alerts consume these.

All gauges unless marked counter. HELP+TYPE once per metric name. Omit a series whose source value is missing; never NaN. Graceful: if litellm is down or the log is absent, still emit `ferry_exporter_up 1` + `ferry_up 0` and whatever else is available; never 500.

### Meta
- `ferry_exporter_up` = 1
- `ferry_exporter_build_info{version="1"}` = 1
- `ferry_scrape_timestamp_seconds`
- `ferry_exporter_uptime_seconds`

### Health / serving (litellm `/health/liveliness`, `/v1/models`)
- `ferry_up` — 1 if `/health/liveliness` returns ok, else 0
- `ferry_health_check_latency_ms` — the http round-trip ms to `/health/liveliness` (passive, no token spend)
- `ferry_models_served` — count of ids from `/v1/models`
- `ferry_model_info{model}` = 1 — one series per served model id

### Traffic (proxy log — CUMULATIVE counters kept in memory since exporter start; tail incrementally like ferry-dash)
- `ferry_requests_total{client,status}` — **counter**, per client-IP × HTTP status (only `/v1/chat/completions`)
- `ferry_backend_events_total{kind}` — **counter**, kind ∈ {quota_exhausted, rate_limited} (from the log's event detection, via `lib/ferry_live.classify_log_line`). Renamed 2026-09-04 from the vendor-specific {kimi_quota, rate_limit} — the old series names disappear outright (not aliased), so a Grafana query or alert still keyed on `kind="kimi_quota"` or `kind="rate_limit"` returns nothing after this release and must be updated to the names above
- `ferry_backend_event_timestamp_seconds{kind}` — epoch of the last such event

VM derives RPS = `rate(sum(ferry_requests_total))`, error rate = `sum(rate(ferry_requests_total{status=~"[45].."})) / sum(rate(ferry_requests_total))`, per-client, per-status.

### Topology (parse `~/.config/ferry/litellm.yaml`)
- `ferry_worker_pool_size` — number of deployments whose `model_name` is the pooled lane (e.g. `flash`)
- `ferry_deployment_info{model_name,model}` = 1 — one per `model_list` deployment
- `ferry_fallback_chain_length` — length of the `orch` lane's fallback chain (0 if none; the pre-rename `orchestrator` key is still accepted)
- `ferry_route_config_mtime_seconds` — mtime of litellm.yaml (detects a config edit)

### Lanes (same parse, with labels — ADDITIVE; the two scalars above keep their exact meaning)
`ferry-backends.json` panels 1-2 and the alert rules read the unlabelled scalars, so these
never replace them; they add the dimensions a scalar cannot carry.
- `ferry_lane_hop{lane,position,hop,deployment,model,provider,pool_size}` = 1 — one series per deployment sitting at a position in a lane's chain. `position="0"` is the primary. `deployment="unknown"` means the config set no `model_info.id`, and that hop can therefore never be joined to a live event
- `ferry_lane_chain_length{lane}` — hops in the lane's chain, **counting the primary** (1 = no fallbacks). Distinct from `ferry_fallback_chain_length`, which counts only the fallbacks and only for the driving lane
- `ferry_pool_size{hop}` — deployments sharing one `model_name`. 0 = the hop is named by a chain but defined nowhere

### Events (front-door tap NDJSON — CUMULATIVE since exporter start; the tail opens at EOF, so a restart never replays a backlog into a counter)
The tap's stream is the only place a request is joined to the deployment that served it — the
proxy access log carries no model at all. Absent tap ⇒ every family here has zero samples and
omits itself; the rest of the scrape is unchanged.
- `ferry_events_total{lane,deployment,provider,outcome}` — **counter**, outcome ∈ {ok, error}
- `ferry_fallback_edges_total{lane,from_deployment,to_deployment,code}` — **counter**, one per observed hop-to-hop move. An edge with an unknown end is not counted: attributing a failure to a possibly-healthy backend is worse than attributing nothing
- `ferry_deployment_state{deployment,provider,state}` = 1 — state ∈ {healthy, rate_limited, quota_exhausted, auth_dead, unreachable, unknown}. Only the CURRENT state emits, so a state the deployment has left disappears and its alert clears
- `ferry_deployment_state_since_seconds{deployment}` — seconds held in that state
- `ferry_events_dropped_total` — **counter**, events the tap's bounded queue dropped rather than block a response. Always emitted (including 0) once a tap is being read: a counter that only appears after the first drop cannot be `rate()`d

### litellm-native (OPT-IN — scraped by VM directly from `http://127.0.0.1:8090/metrics`, NOT emitted by our exporter)
Off by default (callback disabled). The `ferry-backends` dashboard has a clearly-captioned
"LLM internals — requires litellm prometheus (see observ/README.md)" row using best-effort
litellm 1.97.0 metric names (e.g. `litellm_request_total_latency_metric_bucket`,
`litellm_total_tokens_metric`, `litellm_deployment_success_responses`,
`litellm_deployment_failure_responses`, `litellm_spend_metric`). These panels show "No data"
until the user enables it. Never make a ferry_* panel depend on these.

**Prometheus text format:** `# HELP`/`# TYPE` per metric name, escaped label values, plain decimals, trailing newline, deterministic ordering. Content-Type `text/plain; version=0.0.4; charset=utf-8`.

---

## File ownership (ONE writer per file). All new files under `observ/` unless noted.

| Seat | Files it owns |
|---|---|
| **exporter** | `observ/ferry-metrics-exporter` (executable py), `observ/ferry-metrics-exporter.test.py` |
| **grafana-prov** | `observ/grafana/grafana.ini`, `observ/grafana/provisioning/datasources/ferry-vm.yml`, `observ/grafana/provisioning/dashboards/dashboards.yml` |
| **dash-overview** | `observ/grafana/dashboards/ferry-overview.json` |
| **dash-traffic** | `observ/grafana/dashboards/ferry-traffic.json` |
| **dash-backends** | `observ/grafana/dashboards/ferry-backends.json` |
| **dash-lanes** | `observ/grafana/dashboards/ferry-lanes.json` (uid `ferry-lanes`) |
| **alerting** | `observ/grafana/provisioning/alerting/{rules.yml,contactpoints.yml,policies.yml}` |
| **bringup** | `observ/bringup.sh`, `observ/teardown.sh`, `observ/verify.sh`, `observ/victoriametrics/scrape.yml`, `observ/README.md` |
| **cli-wiring** | `lib/ferry-dash.zsh` (edit existing), `lib/ferry-usage.zsh` (edit the `dash` help line only), `README.md` (repo root — edit the Dashboard section only) |

## Runtime (Mac primary; Linux documented)
Processes run as **nohup daemons with PID files** under `~/.config/ferry/observ/` (ferry's own idiom — NOT launchd), so `observ/teardown.sh` can stop them. Mac uses the brew `grafana`/`victoria-metrics` binaries (already installed); Linux falls back to docker or static binaries (bringup detects + documents). Grafana binds `127.0.0.1:3001`, VM `127.0.0.1:8429`, exporter `127.0.0.1:9092`.

## Webhook (contract between alerting + bringup)
Grafana alert contact point uses token `${FERRY_ALERT_WEBHOOK}` (optional — alerts still show in Grafana UI if unset). bringup materializes the provisioning tree via `envsubst` into `~/.config/ferry/observ/grafana-provisioning/` (same pattern as g0vs1g). Datasource url token `${VM_URL}` default `http://127.0.0.1:8429`.

## Grafana admin: `admin` / `ferry-observ` (via GF_SECURITY_ADMIN_* env at launch).

## CLI (cli-wiring seat, do NOT run build.zsh):
Extend `cmd_dash` in `lib/ferry-dash.zsh`: if args include `--grafana`, delegate to
`$APP_DIR/observ/bringup.sh` (add `--down` → `observ/teardown.sh`), passing `--open` through and
stripping `--grafana`/`--down`; otherwise keep today's behavior (exec the `ferry-dash` python page).
Update the `dash` line in `lib/ferry-usage.zsh`'s banner and the Dashboard section of the root `README.md`.
