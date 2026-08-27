<h1 align="center">llm-ferry 🛥️</h1>

<p align="center">
  <b>Turn one Mac into a private AI gateway for your whole LAN.</b><br>
  Serve your cloud API keys <i>and</i> local GPU models to every device — from one OpenAI-compatible endpoint.<br>
  <b>Keys never leave the host. Clients join with one <code>curl</code>.</b>
</p>

<p align="center">
  <a href="https://github.com/sblattj/llm-ferry/releases"><img alt="Latest release" src="https://img.shields.io/github/v/tag/sblattj/llm-ferry?sort=semver&label=release&color=0aa"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20·%20Linux-lightgrey.svg">
  <img alt="Runtime" src="https://img.shields.io/badge/runtime-zsh%20+%20python3%20stdlib-success.svg">
  <img alt="API" src="https://img.shields.io/badge/API-OpenAI--compatible-412991.svg">
</p>

<!--
  Replace docs/demo.gif with a fresh screen recording of `ferry up` on the host +
  the one-line client curl bootstrap. A ~30s GIF above the fold is worth 1000 words.
  The one committed here is a placeholder-quality capture — swap it freely.
-->
<p align="center"><img src="docs/demo.gif" alt="llm-ferry demo" width="760"></p>

You have a strong Mac. You have other laptops. You have a drawer full of API keys copied onto every device. **llm-ferry** collapses all of that into one host: it runs models on your Mac's GPU (via [MLX](https://github.com/ml-explore/mlx)) **and/or** proxies to cloud providers behind the host's own keys, then exposes a single standard **OpenAI-compatible** API (`/v1/chat/completions`, `/v1/models`) that any laptop, editor, or device on the LAN can point at. One command on the host, one `curl | zsh` on each client, and everyone's tools just work — with the API keys staying on exactly one machine.

It goes further than serving inference: it can **ferry whole models and files** from the host to clients and **route a client's downloads through the host** — all over your private LAN.

---

## Is this for you?

- 🧑‍💻 **You have more than one machine.** A beefy Apple Silicon Mac plus laptops that should borrow its GPU and its keys instead of each hoarding their own.
- 🏠 **You run a home lab.** One box becomes the inference appliance; everything else is a thin client.
- 👥 **A small team wants to share one set of API keys.** Centralize billing and secrets on a host; clients never see a key.
- 🤖 **You do agentic coding and want cheap + smart on tap.** Serve a big **orchestrator** model and a pool of cheap **workers** on the same endpoint, and let your agent fan out across both.
- 🔒 **Mac/Linux host, LAN-only, your hardware.** Client↔host traffic is plain HTTP on your private network; cloud calls go host→provider over HTTPS with the host's keys. This is not a public gateway, an auth layer, or a hosted service — and that's the point.

## Why not just…?

`llm-ferry` is built **on** LiteLLM and MLX — it's the glue that turns them into a shared LAN appliance. Honest comparison of **focus**, not "better":

| | Per-device API keys | Ollama / LM Studio | Raw LiteLLM proxy | OpenRouter (hosted) | **llm-ferry** |
|---|:---:|:---:|:---:|:---:|:---:|
| Keys stay on **your** hardware | ✗ *(on every device)* | n/a | ✓ | ✗ *(3rd party sees traffic)* | ✓ |
| Local GPU model serving | — | ✓ *(GGUF)* | — | — | ✓ *(MLX)* |
| Cloud provider proxy | ✓ *(each device)* | — | ✓ | ✓ | ✓ |
| Local **and** cloud on **one** endpoint | — | — | — | — | ✓ |
| One-command LAN client onboarding | — | — | — | — | ✓ |
| Orchestrator + strict fallback chain | — | — | ✓ *(hand-config)* | partial | ✓ *(+ bundled skills)* |
| Multi-key worker pool, least-used + auto-cooldown | — | — | ✓ *(hand-config)* | n/a | ✓ *(template)* |
| Ferry models/files across LAN + forward proxy | — | — | — | — | ✓ |
| Cost | — | free | free | paid markup | free · OSS |

Ollama and LM Studio are excellent local runtimes; a raw LiteLLM proxy is a great cloud gateway; OpenRouter is a fine hosted aggregator. `llm-ferry` is for the specific job none of them targets: **sharing one Mac's local + cloud models across a LAN**, with the client onboarding, routing, and file/model ferrying that job needs — preconfigured.

## Features

- 🌐 **One endpoint, every device** — OpenAI-compatible (`/v1/chat/completions`, `/v1/models`); Anthropic `/v1/messages` too in cloud/route mode via LiteLLM.
- 🔑 **Keys stay on the host** — clients authenticate to the LAN, never to your providers. No key ever ships to a client.
- ⚡ **Local GPU + cloud, same endpoint** — Apple MLX inference on the Mac, or a cloud proxy, or **both models on one route config**.
- 🧠 **Orchestrator + strict fallback chain** — a big planning model with an ordered failover chain across **independent** providers (Kimi, Fireworks DeepSeek/GLM, a ChatGPT subscription).
- 🎛️ **Multi-key worker pool** — several API keys pooled with `usage-based-routing-v2` (proactive least-used spread) and automatic 429 cooldown/failover.
- 🚀 **One-curl client onboarding** — `curl … | zsh` installs the CLI, writes the client profile, and auto-wires the editor (opencode / Continue / Cursor).
- 📊 **Observability, new in v1.5** — a zero-dependency stdlib live page **and** a full Grafana + VictoriaMetrics + VictoriaLogs stack (per-model requests/tokens/spend/latency, a Failures & Fallbacks view, searchable logs).
- 📦 **Ferry models & files across the LAN** — stream whole models from the host's HuggingFace cache, offer/fetch arbitrary files, or push over netcat.
- 🕳️ **Forward proxy for offline clients** — route a client's uv/PyPI/HuggingFace/git downloads through the host's connection.
- 🪶 **Single-file CLI** — `zsh` + `python3` **standard library** only; `litellm`/`mlx` installed via `uv` only when you actually serve inference. Clients fetch the CLI as one script over the LAN.

## Quickstart

### 1. Host (your Mac)

A zero-config bootstrapper installs `uv`, MLX inference (`mlx-vlm`), the cloud proxy (`litellm`), downloads the default local models, and links the `ferry` CLI globally:

```bash
git clone https://github.com/sblattj/llm-ferry.git
cd llm-ferry
./host-bootstrap.sh
```

For cloud mode, set a provider key (never commit it):

```bash
export GEMINI_API_KEY="..."          # or drop it in ~/.config/ferry/secrets.env
```

Start serving, then advertise the client bootstrap over the LAN:

```bash
ferry up             # interactive: pick from the host's live model catalog
ferry share          # print the one-liner clients run (LAN share server on 8095)
```

### 2. Client (any other laptop on the same LAN)

Run the command `ferry share` prints — it embeds your host's live mDNS name and share port:

```bash
curl -fsSL http://your-mac.local:8095/client-bootstrap.sh | zsh
```

`ferry share` prints both the `.local` name **and** the raw LAN IP — use the IP form if `.local` doesn't resolve on your network. The bootstrapper is non-interactive when the host is reachable: it installs the `ferry` CLI to `~/.local/bin`, writes `~/.config/ferry/client.json`, wires opencode to the host endpoint (cloud pair as the persistent default), and adds a `host-code` shell shortcut. It also installs two opencode lane shortcuts into `~/.zshrc` (idempotent, per-invocation):

- `opencode-cloud` — the **cloud pair**: `orch` drives (build/plan), `flash` runs the fan-out (general/explore), `super-flash` handles the background models (title/summary/compaction).
- `opencode-local` — the **GPU pair**: `local-orch` drives, `local-sub` runs the fan-out. Nothing leaves the host.
- bare `opencode` — whichever pair you used **last** (cloud until you first run `opencode-local`; the last-used lane is remembered in `~/.config/ferry/last-lane`).

Both need `ferry up` on the host, which serves all four lanes at once.

#### Catching a client up later

When the host changes — new lanes, a re-pointed alias, a newer `ferry` — an already-bootstrapped client catches up with:

```bash
curl -fsSL http://your-mac.local:8095/client-reset.sh | zsh
```

It re-pulls the CLI and re-applies the opencode takeover to all three configs. It does **not** touch `~/.zshrc`, rewrite `client.json`, or prompt for anything — it reuses the profile the bootstrap left behind. Re-run the bootstrap instead if the machine is new or the shell wrappers need refreshing.

**Why the CLI is re-pulled first, always:** `ferry opencode` is what performs the takeover, so an out-of-date CLI quietly does the *old* thing and reports success either way. The download is validated (shebang, `cmd_opencode` present, `zsh -n` clean) before it replaces the working binary, so a share server that is down cannot leave you with an HTML error page named `ferry`.

Then:

```bash
ferry status                     # connection health + the lanes the host serves
ferry msg "note"                 # send a quick note to the host's log
some-command 2>&1 | ferry log    # stream logs/errors back to the host
```

That's it — every editor and CLI on the client now talks to one endpoint on the host.

### More host commands

```bash
ferry up             # THE STACK: all four lanes on one endpoint (port 8090)
ferry up --route     # cloud lanes only — no GPU weights resident
ferry up --local-orch # just the local-orch GPU lane, alone on 8090
ferry up --local-sub  # just the local-sub GPU lane, alone on 8090
ferry up -c          # cloud proxy to the default cloud model, on port 8090
ferry up -m <id>     # cloud proxy for a specific LiteLLM model id
ferry up -i          # interactive catalog (queries Gemini's live model list)
ferry dash --open    # live route-proxy dashboard at http://localhost:8091
ferry status         # per-lane health, memory, and served lane names
ferry down           # stop all servers, proxies, and share servers
```

#### Catching the host up

The host's counterpart to `client-reset.sh`, and deliberately not its mirror — the host has nothing to download, because `~/.local/bin/ferry` is a symlink into the checkout. Its staleness comes from somewhere else: `ferry` out of sync with `lib/`, a `litellm.yaml` edit the running proxy never picked up, or a symlink that decayed into a plain copy.

```bash
./host-reset.sh            # fast-forward, rebuild, re-link, bounce the proxy   (seconds)
./host-reset.sh --full     # ...and reload the GPU lanes                        (minutes)
./host-reset.sh --no-pull  # skip the git fast-forward (offline, or a dirty tree)
```

By default the MLX lanes are **left running** — `ferry up --route` re-reads the same `litellm.yaml` the stack uses, and litellm reaches the GPU lanes over HTTP on loopback, so a lane does not care that its front door restarted. Only `--full` reloads ~33GB of weights.

**The route config is validated before anything live is touched.** litellm does not check its config beyond parsing it, so a duplicate key, a dangling `model_group_alias`, a fallback naming a lane that does not exist, or an unset `os.environ/…` reference all start cleanly and then fail at request time, on one lane, looking exactly like a provider outage. `host-reset.sh` checks all four while the old proxy is still serving and aborts without restarting anything, so a bad edit costs a failed reset rather than an endpoint.

It then re-applies the opencode takeover to the host's own three configs — wiring the host to its own endpoint is the point of running one — and verifies against the live catalogue that every lane those configs name actually resolves. Hidden aliases are counted as resolvable (they never appear in `/v1/models` by design), and the local backends are probed **directly** on their own ports, because litellm lists `local-orch`/`local-sub` whether or not an MLX server is behind them.

`git pull --ff-only` runs first and never rebases or merges — divergence and uncommitted changes to tracked files stop the run, since both are decisions for a human. Being offline only warns.

---

<sub>Everything below is the full reference — route configs, fallback chains, dashboards, file/model ferrying, and the forward proxy.</sub>

## Contents

- [The stack — four lanes on one endpoint](#the-stack--four-lanes-on-one-endpoint)
- [The local GPU lanes](#the-local-gpu-lanes)
- [Dashboards & observability](#dashboards--observability)
- [Ports](#ports)
- [Ferrying models & files across the LAN](#ferrying-models--files-across-the-lan)
- [Route a client's downloads through the host](#route-a-clients-downloads-through-the-host)
- [Local models](#local-models)
- [Platform support](#platform-support)
- [Privacy](#privacy)
- [Command reference](#command-reference)
- [Development](#development)
- [License](#license)

## The stack — four lanes on one endpoint

`ferry up -c/-m` serves **one** model. Plain **`ferry up`** serves the **stack**: four named **lanes** on a single OpenAI-compatible endpoint, driven by a [LiteLLM config](https://docs.litellm.ai/docs/proxy/configs) plus two local MLX servers.

| Lane | Where it runs | What it is |
|---|---|---|
| **`orch`** | cloud | The big driving model, with a strict **fallback chain** to independent providers |
| **`flash`** | cloud | Cheap high-volume worker, **pooled across many API keys** |
| **`local-orch`** | host GPU | The smart local model (Qwen 3.8-27B nvfp4 + MTP speculative draft) |
| **`local-sub`** | host GPU | The cheap local fan-out model (Nemotron 3 Nano 30B A3B NVFP4) |

```bash
ferry up      # all four, on http://<host>.local:8090/v1
```

A lane **name is the contract**. The model behind it is swappable on the host without editing a single client — that is why the lanes are named for their *role* rather than for a model id. Clients just name a lane:

```bash
curl -s http://your-mac.local:8090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-sub","messages":[{"role":"user","content":"hi"}]}'
```

**How it fits together.** LiteLLM on `:8090` is the only door. The two GPU lanes are `mlx_vlm.server` processes on internal loopback ports (`8092`, `8093`) that LiteLLM fronts as ordinary OpenAI-compatible backends — so a local model and a cloud model are indistinguishable to a client apart from the name it asks for.

The first run seeds `~/.config/ferry/litellm.yaml` from [`litellm-route-example.yaml`](litellm-route-example.yaml) and **stops** so you can edit it — set your model ids and export the keys it references (`KIMI_API_KEY`, `GEMINI_API_KEY`, `FIREWORKS_API_KEY`, in your shell or `~/.config/ferry/secrets.env`) — then re-run.

**Worker pool (load-balanced).** The `flash` lane can be **several identical `flash` deployments** in the yaml: `usage-based-routing-v2` sends each call to the least-used key (proactive even split), and on a `429` it cools the dead key out (`cooldown_time`) and rolls traffic to another. **One Google project, one key.** Gemini quota is per-GCP-project, so a pool of projects really would multiply throughput — which is exactly why it's prohibited: Google APIs ToS §2.d bars circumventing per-API limits (nine burst-created projects were suspended for it in one night, 2026-08-25). Grow the pool only with keys from genuinely separate **providers or accounts**, and get more Gemini capacity by raising the paid tier on the one project (Tier 3 = 20M TPM) instead.

**Orchestrator fallback (strict chain).** The `orch` lane gets an *ordered* fallback chain: `orch-fallback` deployments (example: **Fireworks DeepSeek V4 Pro** first, **GLM 5.2** as last resort, via `FIREWORKS_API_KEY`) that `router_settings.fallbacks` reroutes to **in order, only** when `orch` errors — a `429`, a `5xx`, or a hard quota `403`. Choose fallbacks whose capacity is **independent** of the primary (a different provider or account): one that shares a rate-limit bucket with your primary — or with your own interactive use of that same account — will `429` exactly when you need it. A pay-per-token API like Fireworks has its own capacity and bills only when a hop actually fires. Put the fast model first, and keep `num_retries` low so a hard-down primary falls through quickly instead of burning retry-backoff first.

A **ChatGPT Plus/Pro subscription** can serve a fallback hop too, via LiteLLM's `chatgpt/` provider (device-code login, not an API key) — though in litellm 1.97.0 it's **streaming-only** (a non-streamed call falls through to the next hop, which is harmless in a chain).

**The local lanes are deliberately outside every fallback chain.** The whole point of naming `local-orch` or `local-sub` is that the request stays on your machine — so a stopped GPU lane surfaces as an error rather than quietly spending a cloud quota. (`flash` pool overflow still spills to `orch`, which is a cloud-to-cloud hop.)

**Renaming a lane without breaking clients.** `router_settings.model_group_alias` maps an old name onto a new one, and `hidden: true` keeps the alias out of `/v1/models` so the catalog advertises only the real lanes:

```yaml
router_settings:
  model_group_alias:
    orchestrator: {model: "orch",  hidden: true}   # legacy name, still resolves
    super-flash:  {model: "flash", hidden: true}   # a role, not a model
```

**Never let a real model id become the name clients type.** It is tempting to alias a lane to the model currently behind it, and it goes wrong the first time you re-point that lane: clients keep sending a vendor's model name and get someone else's model back, while `hidden: true` means nothing in `/v1/models` reveals the discrepancy. Name the *role* instead — a role survives the model behind it changing, which is the entire reason clients address lanes. `ferry opencode` enforces the same rule from the client side: it writes only lane names, never a model id.

A `hidden` alias is also how you give a lane to one class of traffic without advertising it. `super-flash` above is the **housekeeping** lane — `ferry opencode` points opencode's `title`/`summary`/`compaction` agents at it — and because it is an alias rather than a `model_name`, it never appears in the catalogue that every client on the LAN can read.

**Add models with Claude Code.** This repo bundles two skills — [`add-fallback-orchestrator`](.claude/skills/add-fallback-orchestrator/SKILL.md) and [`add-worker-model`](.claude/skills/add-worker-model/SKILL.md) — that walk Claude through editing your `litellm.yaml` correctly: the strict-failover-chain vs. load-balanced-pool distinction, the independent-capacity rule for fallbacks, and the per-project-quota gotcha **plus the Google ToS line a worker-key pool must not cross**. Just ask Claude Code to "add a fallback orchestrator" or "add another worker key."

> LiteLLM only **routes and fails over** — the "driver delegates to workers" agent logic lives in **your client** (opencode / Claude Code / etc.). Point it at `http://<host>.local:8090/v1` with the main model set to a driving lane (`orch` or `local-orch`) and the subagent model to its cheap partner (`flash` or `local-sub`).

**opencode auto-wiring.** On a client, `ferry opencode` takes opencode's config over so **every** agent routes through the host. Add `--local` to pick the GPU pair instead of the cloud pair:

```bash
ferry opencode            # orch drives, flash fans out
ferry opencode --local    # local-orch drives, local-sub fans out
```

It is a **surgical takeover, not a merge**. Four keys belong to ferry and are replaced outright; everything else in your config — `mcp`, `lsp`, `theme`, `command`, your own keys — is left exactly as it was:

| key | becomes |
|---|---|
| `model` | `ferry/<driver>` |
| `small_model` | `ferry/<housekeeper>` |
| `permission` | `"allow"` |
| `agent` | all seven built-ins pinned (below) |

`plugin` is *appended* to, never replaced — [`@prevalentware/opencode-goal-plugin`](https://github.com/prevalentWare/opencode-goal-plugin) is added if it isn't already there.

All seven of opencode's built-in agents get pinned across **three roles**, so nothing silently escapes to a model you aren't paying for on purpose:

| role | agents | cloud | GPU |
|---|---|---|---|
| driver | `build`, `plan` | `orch` | `local-orch` |
| worker | `general`, `explore` | `flash` | `local-sub` |
| housekeeper | `title`, `summary`, `compaction` | `super-flash` | `local-sub` |

The housekeeping three matter more than they look. They fire on their own schedule rather than as part of a fan-out, and an unpinned `compaction` sends your *entire transcript* to whatever the default model is. Giving them their own lane also keeps a compaction — the largest single request opencode ever makes — from queueing behind a fan-out that has just saturated the worker pool. `small_model` follows the same lane, since opencode's schema describes it as the model "for tasks like title generation".

On the GPU pair there is no third lane, so the housekeeper shares `local-sub`. Point the housekeeper anywhere with `--housekeeper <lane>`.

**`agent` is replaced wholesale rather than merged**, which is the point — a stale per-agent pin is exactly the drift this ends. Before every write, the previous config is copied to `<name>.<UTC-timestamp>.jsonc` beside it (last 10 kept, `--keep N` to change), so a takeover is always reversible and any custom agent you had is recoverable. The `.jsonc` extension is deliberate: opencode's schema allows comments, and the snapshot is where they survive the rewrite.

Only the **lane pair** is ever declared as a model — never the served catalogue. The host also advertises the fallback deployments (`flash-gem`, `flash-or`, `orch-deepseek`, …), but those are reached by the *router* on overflow, not by a client picking one out of a menu.

## The local GPU lanes

Both GPU lanes run under `mlx-vlm` and start together with `ferry up`; each can also be served alone on `:8090` with `ferry up --local-orch` / `--local-sub`.

**`local-orch` — Qwen 3.8-27B nvfp4** (~15 GB) with `mlx-community/Qwen3.8-27B-MTP-8bit` as a speculative draft model. The heavier, more capable local model, and the only local lane with an MTP drafter, so speculative decoding applies to it.

**`local-sub` — NVIDIA Nemotron 3 Nano 30B A3B NVFP4** (~18 GB). A `nemotron_h` hybrid MoE — only 6 of 52 layers are full attention with just 2 KV heads × 128 head dim — so the KV cache is ~6 KB/token, under 1 GB per 128k-token agent stream. That plus ~3B active params (A3B) is what makes it the right lane for **concurrent subagents**: many parallel streams fit in RAM and decode stays fast. No speculative draft model exists for it.

Both are just **defaults** — swap either for any MLX-compatible model your Mac's unified memory can hold by editing `LOCAL_MODEL_ORCH` / `LOCAL_MODEL_SUB` in `lib/ferry-core.zsh` (then `./build.zsh`), and point the matching deployment in `litellm.yaml` at the new HuggingFace id. Local GPU serving is **macOS / Apple Silicon only**; on Linux `ferry up` degrades to the cloud lanes automatically.

**Memory.** Running both lanes keeps ~33 GB of weights resident before any KV cache. The governor below is what keeps that safe; per-lane overrides (`LOCAL_SUB_MAX_KV=65536`, etc.) let you shrink one lane without touching the other.

## Dashboards & observability

**Lightweight live page — `ferry dash`:**

```bash
ferry dash --open        # live web dashboard at http://localhost:8091
```

A live local dashboard for the route proxy — it runs on the host, no browser polling of the LAN. It shows ferry up/down, the served model groups, the **orchestrator topology read from your `litellm.yaml`** (primary → the `fallbacks` chain), the worker pool, and **recent request activity parsed from the proxy log** (rate, status breakdown, per-client, a sparkline). **Auto-refresh costs nothing** — it only reads the local log plus `/health/liveliness` and `/v1/models`. A **"Test backends"** button is the only thing that spends tokens: it actively pings each backend and reports *which fallback hop actually served* + latency. Pure Python **standard library**, so it runs under any `python3` — no venv, no pip. (Also available standalone as `ferry-dash`.)

**Full stack — `ferry dash --grafana` (new in v1.5):**

```bash
ferry dash --grafana --open      # stand up Grafana + VictoriaMetrics + VictoriaLogs
ferry dash --grafana --down      # tear it down
```

Stands up a full **Grafana + VictoriaMetrics + VictoriaLogs** observability stack on `http://localhost:3001` (login `admin` / `ferry-observ`) — request-rate, error-rate, and latency dashboards, backend/fallback topology, per-model usage (requests, tokens, spend, latency, success/failure), a **Failures & Fallbacks** view (failures by model/reason, cooldowns, fallbacks fired), and searchable per-model proxy logs — backed by metrics and log history that persist across sessions. It runs as local `nohup` daemons under `~/.config/ferry/observ/` (Grafana `:3001`, VictoriaMetrics `:8429`, VictoriaLogs `:9428`, a metrics exporter `:9092`). All OSS, $0, localhost-only. See [`observ/README.md`](observ/README.md) for setup, ports, and what each dashboard covers.

## Ports

| Port | Purpose | Started by |
|---|---|---|
| **8090** | The endpoint — every lane, for every client | `ferry up` |
| **8091** | Live route-proxy dashboard (localhost only) | `ferry dash` |
| **8092** | `local-orch` MLX backend (**internal** — clients use 8090) | `ferry up` |
| **8093** | `local-sub` MLX backend (**internal** — clients use 8090) | `ferry up` |
| **8095** | LAN share server — client bootstrap, model/file ferry routes, client telemetry | `ferry share` |
| **8096** | HuggingFace pass-through proxy (experimental) | `ferry serve-hf` |
| **8097** | General HTTP(S) download forward proxy | `ferry serve-proxy` |
| **9099** | Default netcat port for direct `ferry send` / `ferry receive` | `ferry send` / `ferry receive` |
| **3001 / 8429 / 9428 / 9092** | Grafana / VictoriaMetrics / VictoriaLogs / metrics exporter (localhost only) | `ferry dash --grafana` |

## Ferrying models & files across the LAN

`ferry` can move whole models (from the host's local HuggingFace cache) and arbitrary files/dirs from the **host** to a **client**, over three transports.

### Models — `ferry pull`

```bash
# http (default): stream + untar from the host's local HF cache via the share server (8095)
ferry pull mlx-community/Qwen3.8-27B-nvfp4 --host your-mac.local
ferry pull org/model --host your-mac.local --to ~/models     # choose the destination

# hf (EXPERIMENTAL): download THROUGH the host's proxy (host must run `ferry serve-hf`)
ferry pull org/model --host your-mac.local --transport hf

# nc: this laptop listens; then on the host run `ferry send` (see below)
ferry pull org/model --transport nc
```

`--host` / `--port` default to the client's saved profile (`~/.config/ferry/client.json`); pass them explicitly from an unconfigured machine. The `http` transport reads the **host's local HuggingFace cache**, so the host must already have the model — or use `ferry serve-hf` + `--transport hf` to fetch it *through* the host.

Plain `curl` works too — the share server exposes the transfer routes directly:

```bash
curl -fsS http://your-mac.local:8095/pull/mlx-community/Qwen3.8-27B-nvfp4 | tar -x
curl -fsS http://your-mac.local:8095/manifest       # list cached models + offered files
```

### Files — `ferry offer` / `ferry get`

```bash
# host — record files/dirs in ~/.config/ferry/offered.json as {basename: absolute-path}
ferry offer ~/datasets/eval.jsonl ~/configs/prompt.txt

# client — fetch by basename
ferry get eval.jsonl --host your-mac.local --to ./data
curl -fsS http://your-mac.local:8095/file/eval.jsonl | tar -x   # plain-curl equivalent
```

### Direct push — `ferry send` / `ferry receive`

For a one-off push with no share server, use netcat (default port 9099):

```bash
# client (start first — it listens)
ferry receive --port 9099 --to ./incoming

# host (then push)
ferry send ~/some/dir client-laptop.local --port 9099
```

### `ferry serve-hf` (experimental)

Starts a pass-through HTTP proxy to `https://huggingface.co` on port 8096, following HF's LFS→CDN redirects, so a client with `HF_ENDPOINT=http://<host>:8096` downloads *through* the host. Intentionally minimal; stop it with `ferry down`.

```bash
ferry serve-hf                                                   # host
HF_ENDPOINT=http://your-mac.local:8096 hf download org/model     # client
# ...or simply: ferry pull org/model --host your-mac.local --transport hf
```

## Route a client's downloads through the host

A client with no (or limited) internet can pull its own dependencies and models *through* the host. Start a general HTTP(S) forward proxy on the host, then have the client emit the proxy env vars into its shell:

```bash
# host
ferry serve-proxy
# client
eval "$(ferry env)"        # or: eval "$(ferry env --host your-mac.local)"
uvx whosaid ...            # uv/PyPI, huggingface_hub, git, curl — now download via the host
```

`ferry env` prints the `HTTP(S)_PROXY` / `HF_ENDPOINT` / `NO_PROXY` exports on stdout so it stays `eval`-able (add `--write` to persist them into `~/.zshrc`). The proxy handles HTTPS via `CONNECT` tunneling and plain HTTP by forwarding, and covers **anything that honors the standard proxy env vars**. It routes each request straight through **the host's own connection with no caching** — the host just needs internet. Stop it with `ferry down`.

## Local models

The two GPU lanes and how to swap their models are covered in [The local GPU lanes](#the-local-gpu-lanes). This section is the operational detail.

**KV-cache memory governor:** local launches ship with `--kv-bits 4`, `--max-kv-size 131072`, `--max-num-seqs 4`, and `APC_NUM_BLOCKS=512`. Measured on a 128GB M5 Max during a 121k-token agentic session: peak GPU footprint dropped 97GB -> 56GB, idle retained memory fell 57GB -> 35GB, and decode ran ~60% faster. Monitor live usage with `footprint <pid>` (`ps` RSS does not show Metal wired memory) — `ferry status` prints it per lane. Disable any knob by setting it to `""` in `lib/ferry-core.zsh`, or govern one lane only with the per-lane overrides (`LOCAL_ORCH_MAX_KV`, `LOCAL_SUB_MAX_SEQS`, …).

Both lanes run at these settings, so the stack keeps ~33GB of weights resident and two simultaneously-busy deep-context lanes can approach the ~90-100GB wired ceiling. If that bites, shrink the subagent lane first — `LOCAL_SUB_MAX_KV=65536` — since fan-out work rarely needs 128k of context.

**Known issue on the `local-orch` (Qwen) lane (measured 2026-08-25):** deep-context **streaming** requests can die mid-prefill. The mlx-vlm server raises `RuntimeError: There is no Stream(gpu, 1) in current thread` (observed ~40s into a ~44k-token prefill), litellm surfaces it as `MidStreamFallbackError` / `APIConnectionError: An error occurred during streaming`, and the client sees a dropped stream. The server **self-recovers** — subsequent requests succeed, and non-streaming requests were unaffected — so just retry the turn. No cloud fallback is wired for this by design (a dead GPU lane must error, not silently bill a cloud lane).

**MTP draft + quantized KV crashes the qwen3_5 verify path (diagnosed 2026-08-25):** with `--kv-bits` *and* an MTP draft model, the draft-verify branch of mlx-vlm's qwen3_5 attention crashed on **every cache-hit request** (turn 2+ of any conversation): any quantized cache (`BatchQuantizedKVCache`, regardless of 4/8 bits) returns keys as a tuple of `(packed, scales, biases)` arrays, and `prefix_len = keys.shape[-2]` raised `AttributeError` → 500 `APIConnectionError`. Deterministic repro: the same request twice → 200 then 500. **The shipped `local-orch` config is MTP draft + UNquantized KV capped at 64k** (16GB KV budget): stable (3/3 cache-hit requests 200) and ~53% faster decode (37.8 vs 24.8 tok/s). `local-sub` (no drafter, 4-bit KV) is unaffected.

**Compacting big sessions on `opencode-local` produces empty summaries (measured 2026-08-25):** a `/compact` sends the whole transcript (~43k-72k tokens) to the driving lane; on `local-orch` (Qwen 3.8 nvfp4) both observed compacts completed a full prefill and then generated **3 tokens** and stopped — an effectively empty summary. Compacting large sessions via `opencode-cloud` (or at least the first compact of a huge session) is the workaround.

**Known issues on the `local-sub` (Nemotron) lane (measured 2026-08-25):**

- **`nemotron_h` continuous-batching crash (mlx-vlm) — patched automatically.** The batching engine passes both `input_ids` and `inputs_embeds`; the `nemotron_h` `LanguageModel.__call__` forwards both to a backbone that requires exactly one → `ValueError: Provide exactly one of inputs or inputs_embeds` on **every** request. `ferry install` and `host-bootstrap.sh` now apply the two-line fix to `.../site-packages/mlx_vlm/models/nemotron_h/language.py` after installing mlx-vlm. The patch is idempotent and no-ops once upstream fixes the call site — but note that **any manual `uv tool install mlx-vlm --force` wipes it**, so re-run `ferry install` after upgrading mlx-vlm yourself.
- **Flaky `task`-tool calls.** Nemotron frequently emits malformed task calls (hallucinated `task_id`, missing `description`) that opencode rejects *before the tool runs* — the model then silently retries the identical broken call (measured: 444 consecutive errors; also 22 identical 38-token retries). Fix shipped: `client-bootstrap.sh` installs a `/fan-out` command and a `spawning-subagents` skill into `~/.config/opencode/`. The recipe must sit in the **user message** (`/fan-out` does this); placing it in system instructions made failures worse. With it: 3/3 valid parallel task calls, zero schema errors. This matters less now that Nemotron is the *subagent* lane rather than the driver — but it still applies to whatever small local model is driving.
- **Residual model limits.** Bare tool calls (read/write/bash) are reliable; single delegation works. Complex multi-brief orchestration exceeds the 30B model — it duplicates briefs or stops to ask clarifying questions instead of integrating. This is exactly why it sits on `local-sub` and `local-orch` (Qwen) drives.
- **Headless-run doom signature.** Watch the *server* log (opencode's `--format json` stream lags and misses in-flight loops): 3+ consecutive requests with identical generated-token counts and `finish_reason=tool_calls` = kill it.

## Platform support

`ferry` and `ferry-dash` run on **macOS and Linux/Ubuntu**.

| Platform | Local MLX serving | Cloud proxy · route · dash · client wiring · LAN share/transfer |
|---|:---:|:---:|
| **macOS (Apple Silicon)** | ✓ | ✓ |
| **Linux / Ubuntu** | — *(macOS only)* | ✓ |

**Local GPU serving uses Apple MLX and is macOS / Apple Silicon only.** On Linux, plain `ferry up` automatically degrades to the cloud lanes. On Linux, serve models with `--route`, `--cloud`, or `--model <id>` against a cloud / OpenAI-compatible endpoint instead. `ferry install` on Ubuntu skips MLX and the model downloads — it installs `uv` + `litellm` and links the CLI — and may prompt you to `apt install zsh` (ferry is a zsh script), and recommends `avahi-daemon` (so `.local` mDNS names resolve) and `iproute2` (for the `ip` command used in LAN IP detection).

## Privacy

Everything runs on your own hardware and network. Client↔host traffic stays on your **private LAN as plain HTTP**; cloud calls go host→provider over HTTPS using the host's keys, so **client devices never see the keys**. Inbound client telemetry (`ferry msg` / `ferry log`) is appended to `~/.config/ferry/client_logs.txt` on the host — outside any checkout, so it survives a repo that moves or a worktree that is removed. The observability stack binds to `127.0.0.1` only.

## Command reference

| Command | Mode | What it does |
|---|---|---|
| `install` | host | Install `uv`, `litellm` (+ `mlx-vlm` & default models on macOS), link `ferry` globally |
| `up [--local-orch\|--local-sub\|-c\|-m <id>\|-r\|-i] [-p <port>]` | host | **No args → the full stack**: `orch` + `flash` (cloud) and `local-orch` + `local-sub` (GPU) on `8090`. `-r`/`--route` → cloud lanes only; `--local-orch`/`--local-sub` → one GPU lane alone; `-c`/`-m` → a single cloud model; `-i` → interactive catalog |
| `down` | host | Stop all servers, cloud proxies, and share/proxy servers |
| `status` | both | Host: per-lane listeners, memory, and served lane names. Client: connection health + the host's lanes |
| `dash [--open] [--port P] [--ferry URL]` | host | Live route-proxy dashboard on `8091` (`--grafana` → full Grafana/VictoriaMetrics stack; also standalone `ferry-dash`) |
| `share` | host | Serve the client bootstrap + ferry transfer routes over the LAN (`8095`) |
| `msg <text>` | client | Send a text note to the host's `~/.config/ferry/client_logs.txt` |
| `log` | client | Pipe stdin straight to the host's `~/.config/ferry/client_logs.txt` |
| `offer <path>...` | host | Record files/dirs in `offered.json` for clients to fetch |
| `pull <model-id> [--host H] [--port P] [--transport http\|hf\|nc] [--to DIR]` | client | Pull a model from the host cache (three transports) |
| `get <name> [--host H] [--port P] [--to DIR]` | client | Fetch an offered file/dir by basename |
| `receive [--port P] [--to DIR]` | client | Listen for a netcat tar stream (default port `9099`) |
| `send <path> <client-host> [--port P]` | host | Push a file/dir to a listening client via netcat (default `9099`) |
| `serve-hf [--port P]` | host | Start the experimental HuggingFace pass-through proxy (default `8096`) |
| `serve-proxy [--port P]` | host | Start the general HTTP(S) download forward proxy (default `8097`) |
| `env [--host H] [--proxy-port P] [--hf-port P2] [--write]` | client | Emit shell exports so this laptop routes downloads via the host proxy |
| `opencode [--host H] [--port P] [--config PATH] [--local\|--cloud] [--model M] [--small-model SM] [--housekeeper HK] [--keep N] [--no-default]` | dual | Take the opencode config over: `permission`, `model`, `small_model`, and all seven built-in agents, pinned to lane names. Snapshots the original first |

Run `ferry --help` for the built-in usage banner.

## Development

`ferry` is assembled from per-domain modules so the CLI isn't one file to reason about. Source lives in [`lib/`](lib/) as **10 modules**: `ferry-core` (bootstrap, LAN/mDNS discovery, config, secrets), `ferry-usage`, `ferry-install`, `ferry-serve` (up/down/status/catalog), `ferry-share`, `ferry-transfer` (pull/get/send/receive/offer), `ferry-proxy` (serve-hf/serve-proxy), `ferry-integrate` (env/opencode), `ferry-dash`, and `ferry-main` (dispatch). The shipped `ferry` is a **generated** single file — clients fetch it as one script over the LAN — so edit the modules and regenerate:

```bash
./build.zsh            # regenerate ./ferry from lib/ferry-*.zsh
./build.zsh --check    # CI / pre-commit: fails if ferry has drifted from lib/
```

Commit both `lib/` and the regenerated `ferry`; don't hand-edit `ferry` (the sync guard will flag it).

### Tests

Stdlib `unittest`, no dependencies, each suite runnable on its own:

```bash
python3 lib/ferry-serve.test.py       # lane ports, launch flags, KV governor
python3 lib/ferry-integrate.test.py   # the opencode takeover: scope, lane split, snapshots
python3 lib/ferry-share.test.py       # share server + client-script placeholder injection
python3 lib/ferry-hostreset.test.py   # host-reset: route-config validation, endpoint verify
```

The share and host-reset suites deliberately run the **real** embedded Python — extracted out of the built `ferry` and out of `host-reset.sh` — rather than a reimplementation, so an edit that breaks the shipped behaviour fails in the suite instead of on a laptop.

## License

MIT — see [LICENSE](LICENSE). © 2026 Stephen Blatt.
