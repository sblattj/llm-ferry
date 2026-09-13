<h1 align="center">llm-ferry 🛥️</h1>

<p align="center">
  <b>Turn one Mac into a private AI gateway for your whole LAN.</b><br>
  Serve your cloud API keys <i>and</i> local GPU models to every device — from one OpenAI-compatible endpoint.<br>
  <b>Keys never leave the host. Clients join with one <code>curl</code>.</b>
</p>

<p align="center">
  <a href="https://github.com/sblattj/llm-ferry/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/sblattj/llm-ferry?style=social"></a>
  <a href="https://github.com/sblattj/llm-ferry/releases"><img alt="Latest release" src="https://img.shields.io/github/v/tag/sblattj/llm-ferry?sort=semver&label=release&color=0aa"></a>
  <a href="https://github.com/sblattj/llm-ferry/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/sblattj/llm-ferry"></a>
  <a href="https://github.com/sblattj/llm-ferry/issues"><img alt="Open issues" src="https://img.shields.io/github/issues/sblattj/llm-ferry"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20·%20Linux-lightgrey.svg">
  <img alt="Runtime" src="https://img.shields.io/badge/runtime-zsh%20+%20python3%20stdlib-success.svg">
  <img alt="API" src="https://img.shields.io/badge/API-OpenAI--compatible-412991.svg">
</p>

<p align="center"><img src="docs/demo.gif" alt="llm-ferry terminal workflow demo" width="760"></p>

<p align="center">
  <a href="docs/signal-studio.md"><img src="docs/images/signal-studio-desktop.png" alt="Signal Studio desktop: searchable model library, editable fallback routes, and route overview" width="1100"></a><br>
  <sub><b>Signal Studio</b> — build fallback routes, preview changes, and follow live requests. Actual dashboard capture with synthetic demonstration data.</sub>
</p>

<p align="center"><b>Design your fallback routes once — reach them from any screen.</b></p>
<table align="center">
  <tr>
    <td><img src="docs/images/signal-studio-ipad.png" alt="Signal Studio on iPad: drag fallback hops between ordered chains" width="520"></td>
    <td><img src="docs/images/signal-studio-mobile.png" alt="Signal Studio on phone: tap to edit a route from anywhere on the LAN" width="200"></td>
  </tr>
  <tr>
    <td align="center"><sub>Drag fallback hops between ordered chains — tablet layout.</sub></td>
    <td align="center"><sub>Reroute a model from the couch — phone layout.</sub></td>
  </tr>
</table>

<p align="center">
  <img src="docs/images/signal-studio-metrics.png" alt="Live request stream: per-request first-text latency, token counts, throughput, and the fallback hop each request walked" width="900"><br>
  <sub><b>Every request, accounted for.</b> First-text latency, in/out/reasoning tokens, and throughput per call — and when a lane degrades, you watch it light the next hop instead of erroring.</sub>
</p>

You have a strong Mac. You have other laptops. You have a drawer full of API keys copied onto every device. **llm-ferry** collapses all of that into one host: it runs models on your Mac's GPU (via [MLX](https://github.com/ml-explore/mlx)) **and/or** proxies to cloud providers behind the host's own keys, then exposes a single standard **OpenAI-compatible** API (`/v1/chat/completions`, `/v1/models`) that any laptop, editor, or device on the LAN can point at. One command on the host, one `curl | zsh` on each client, and everyone's tools just work — with the API keys staying on exactly one machine.

It goes further than serving inference: it can **ferry whole models and files** from the host to clients and **route a client's downloads through the host** — all over your private LAN.

## By the numbers

- **97 GB → 56 GB peak GPU** and **~60% faster decode** — the KV-cache governor, measured on a 128 GB M5 Max during a 121k-token agentic session (idle retained memory fell 57 GB → 35 GB).
- **Eight lanes, one endpoint** — local GPU *and* cloud models behind a single OpenAI-compatible API, each lane with strict named fallback chains.
- **Zero heavyweight dependencies** — a single-file CLI built from `zsh` + `python3` **standard library**; `litellm`/`mlx` arrive via `uv` only when you actually serve inference.

## Contents

- [Is this for you?](#is-this-for-you)
- [Why not just…?](#why-not-just)
- [Features](#features)
- [Quickstart](#quickstart)
- [Recent releases](#recent-releases)
- [The stack — eight lanes on one endpoint](#the-stack--eight-lanes-on-one-endpoint)
- [Fleets](#fleets)
- [The local GPU lanes](#the-local-gpu-lanes)
- [Dashboards & observability](#dashboards--observability)
- [Encrypted transfer off the LAN](#encrypted-transfer-off-the-lan--ferry-drop--ferry-pickup)
- [Ports](#ports)
- [Ferrying models & files across the LAN](#ferrying-models--files-across-the-lan)
- [Route a client's downloads through the host](#route-a-clients-downloads-through-the-host)
- [Reverse expose: publish a client's port through the host](#reverse-expose-publish-a-clients-port-through-the-host)
- [Remote access (Tailscale)](#remote-access-tailscale)
- [Local models — operating notes](#local-models--operating-notes)
- [Platform support](#platform-support)
- [Privacy](#privacy)
- [Command reference](#command-reference)
- [FAQ](#faq)
- [Contributing](#contributing)
- [Acknowledgments](#acknowledgments)
- [Development](#development)
- [License](#license)
- [Deep dives](docs/deep-dives.md) — client-wiring scopes, route forensics, plugin-cache archaeology, measured known issues

## Is this for you?

- 🧑‍💻 **You have more than one machine.** A beefy Apple Silicon Mac plus laptops that should borrow its GPU and its keys instead of each hoarding their own.
- 🏠 **You run a home lab.** One box becomes the inference appliance; everything else is a thin client.
- 👥 **A small team wants to share one set of API keys.** Centralize billing and secrets on a host; clients never see a key.
- 🤖 **You do agentic coding and want cheap + smart on tap.** Serve a big **orchestrator** model and a pool of cheap **workers** on the same endpoint, and let your agent fan out across both.
- 🔒 **Mac/Linux host, LAN-only, your hardware.** Client↔host traffic is plain HTTP on your private network behind one shared master key; cloud calls go host→provider over HTTPS with the host's keys. This is not a public gateway or a hosted service — and that's the point.

## Why not just…?

`llm-ferry` is built **on** LiteLLM and MLX — it's the glue that turns them into a shared LAN appliance. Honest comparison of **focus**, not "better":

| Capability | Per-device API keys | [Ollama](https://ollama.com) / [LM Studio](https://lmstudio.ai) | Raw [LiteLLM](https://github.com/BerriAI/litellm) proxy | [OpenRouter](https://openrouter.ai) (hosted) | **llm-ferry** |
|---|:---:|:---:|:---:|:---:|:---:|
| Keys stay on **your** hardware | ✗ *(on every device)* | n/a | ✓ | ✗ *(3rd party sees traffic)* | ✓ |
| Local GPU model serving | — | ✓ *(GGUF)* | — | — | ✓ *(MLX)* |
| Cloud provider proxy | ✓ *(each device)* | — | ✓ | ✓ | ✓ |
| Local **and** cloud on **one** endpoint | — | — | — | — | ✓ |
| One-command LAN client onboarding | — | — | — | — | ✓ |
| Named lanes + strict fallback hops | — | — | ✓ *(hand-config)* | partial | ✓ *(+ bundled skills)* |
| Multi-key worker pool, least-used + auto-cooldown | — | — | ✓ *(hand-config)* | n/a | ✓ *(template)* |
| Ferry models/files across LAN + forward proxy | — | — | — | — | ✓ |
| Cost | — | free | free | paid markup | free · OSS |

Ollama and LM Studio are excellent local runtimes; a raw LiteLLM proxy is a great cloud gateway; OpenRouter is a fine hosted aggregator. `llm-ferry` is for the specific job none of them targets: **sharing one Mac's local + cloud models across a LAN**, with the client onboarding, routing, and file/model ferrying that job needs — preconfigured.

## Features

- 🧾 **Run on your subscriptions, not just API keys** — ChatGPT- and Claude Pro/Max-subscription lanes log in once over OAuth (`ferry auth-claude login`); each carries metered fallback hops so an exhausted subscription degrades to pay-per-token instead of erroring the client. [[releases]](docs/releases/v1.37.0.md)
- 🌐 **One endpoint, every device** — OpenAI-compatible (`/v1/chat/completions`, `/v1/models`); Anthropic `/v1/messages` too, so **Claude Code runs on the ferry backend** (`claude-ferry` wrappers). [[The stack →]](#the-stack--eight-lanes-on-one-endpoint)
- 🔑 **Keys stay on the host** — provider keys never leave the host; clients hold one shared master key. [[Privacy →]](#privacy)
- ⚡ **Local GPU + cloud, same endpoint** — Apple MLX inference on the Mac, or a cloud proxy, or both in one route config. [[Local GPU lanes →]](#the-local-gpu-lanes)
- 🧠 **Named lanes with explicit fallback hops** — clients pick a role (`heavy`, `flash`, …); you swap the backends without editing a single client. [[The stack →]](#the-stack--eight-lanes-on-one-endpoint)
- 🗺️ **Fleets** — switch every cloud lane between routing sets (e.g. `domestic` ↔ `international`) per caller, mid-session, no restart. [[Fleets →]](#fleets)
- 🎛️ **Multi-key worker pool** — pooled deployments with least-used spread and automatic 429 cooldown/failover.
- 🚀 **One-curl client onboarding** — installs the CLI, writes the client profile, and auto-wires the editor (opencode / Continue / Cursor). [[Quickstart →]](#quickstart)
- 🎨 **Signal Studio route editor** — search your model library *and* the live public OpenRouter catalog, add/reorder/copy fallback hops, undo, preview the exact YAML diff, apply. Desktop, tablet, and phone layouts. [[Tour →]](docs/signal-studio.md)
- 📊 **See each request clearly** — first-text latency, duration, and reported tokens in the live dashboard; optional Grafana + VictoriaMetrics + VictoriaLogs for persistent observability. [[Dashboards →]](#dashboards--observability)
- 📦 **Ferry models & files across the LAN** — stream whole models from the host's HuggingFace cache, offer/fetch arbitrary files, or push over netcat. [[→]](#ferrying-models--files-across-the-lan)
- 🕳️ **Forward proxy for offline clients** — route a client's uv/PyPI/HuggingFace/git downloads through the host's connection. [[→]](#route-a-clients-downloads-through-the-host)
- 🔄 **Reverse tunnel for locked-down clients** — publish one of a client's own ports through the host, with the client only ever dialling out (`ferry relay` / `ferry expose`); browser VNC included, so a phone needs only a URL. [[→]](#reverse-expose-publish-a-clients-port-through-the-host)
- 🔐 **Encrypted drop for machines off the LAN** — `ferry drop` writes an authenticated, self-contained blob movable over any channel; `ferry pickup` verifies and decrypts it. The passphrase, not the carrier, is the security boundary. [[→]](#encrypted-transfer-off-the-lan--ferry-drop--ferry-pickup)
- 🪶 **Single-file CLI** — `zsh` + `python3` standard library only; clients fetch the CLI as one script over the LAN.

## Quickstart

**1 · Host (your Mac).** One line installs `uv`, MLX inference (`mlx-vlm`), the cloud proxy (`litellm`), downloads the default local models (~16.6 GB), and links the `ferry` CLI globally:

```bash
curl -fsSL https://github.com/sblattj/llm-ferry/archive/refs/heads/main.tar.gz | tar xz && ./llm-ferry-main/host-bootstrap.sh
# or: git clone https://github.com/sblattj/llm-ferry.git && cd llm-ferry && ./host-bootstrap.sh
```

For cloud mode, set a provider key (never commit it) and start serving:

```bash
export OPENROUTER_API_KEY="..."      # or drop it in ~/.config/ferry/secrets.env
ferry auth-claude login              # optional: Claude Pro/Max subscription lanes (browser OAuth)
ferry up                             # interactive: pick from the host's live model catalog
ferry share                          # print the one-liner clients run (LAN share server on 8095)
```

**2 · Client (any other laptop on the same LAN).** Run the command `ferry share` prints — it embeds your host's live mDNS name and share port:

```bash
curl -fsSL http://your-mac.local:8095/client-bootstrap.sh | zsh
```

The bootstrapper is non-interactive when the host is reachable: it installs the `ferry` CLI to `~/.local/bin`, writes `~/.config/ferry/client.json`, wires opencode to the host endpoint, and adds `opencode-cloud` / `opencode-local` / `opencode-super` shell shortcuts — and, when `claude` is installed, `claude-ferry` / `claude-ferry-local` / `claude-ferry-super`. Bare `opencode` and `claude` are deliberately untouched. Then check in:

```bash
ferry status                     # connection health + the lanes the host serves
ferry msg "note"                 # send a quick note to the host's log
some-command 2>&1 | ferry log    # stream logs/errors back to the host
```

That's it — every editor and CLI on the client now talks to one endpoint on the host. Narrower takeover scopes, catch-ups, and full removal (`client-reset.sh` / `client-cleanup.sh`): [Deep dives](docs/deep-dives.md#opencode-takeover-scopes); the host reads client telemetry back with `ferry inbox` ([attribution internals](docs/deep-dives.md#reading-what-the-clients-sent)).

> If ferry replaced your key-sync ritual, [⭐ star the repo](https://github.com/sblattj/llm-ferry). It helps other people find it.

## Recent releases

- **[v1.37.0 — Claude subscription lanes](docs/releases/v1.37.0.md)** — `ferry auth-claude login` serves `claude-*` traffic from a Claude Pro/Max subscription over OAuth, with metered fallback hops.
- **[v1.36.0 — the extraction lane comes home](docs/releases/v1.36.0.md)** — Schematron-8B HTML→JSON extraction now runs on the host GPU, on its own door (`ferry up --schematron`). [Full history →](docs/releases)

## The stack — eight lanes on one endpoint

`ferry up -c/-m` serves **one** model. Plain **`ferry up`** serves the **stack**: eight named **lanes** on a single OpenAI-compatible endpoint, driven by a [LiteLLM config](https://docs.litellm.ai/docs/proxy/configs) plus three local MLX servers.

| Lane | Where it runs | What it is |
|---|---|---|
| **`heavy`** | cloud | The driving model; the domestic template has one fallback on the same ChatGPT subscription |
| **`medium`** | cloud | General work when advertised; the domestic template runs GPT-5.6 Terra at xhigh with an OpenRouter Terra fallback |
| **`flash`** | cloud | Explore worker; the domestic template runs GPT-5.6 Luna at xhigh, then Gemini Flash Latest, then Terra |
| **`super-flash`** | cloud | Compaction, title, and summary; `openrouter/~google/gemini-flash-latest` at minimal reasoning with throughput routing and no fallback |
| **`schematron`** | host GPU | HTML→JSON structured extraction at temperature 0, **on-machine** (`pchamart/schematron8B-mlx-8bit`, an 8-bit MLX quant, on internal port 8100); no fallback; used by cdp-toolkit `extract_page` |
| **`schematron-cloud`** | cloud | The same extraction job off-box (`openrouter/inference-net/schematron-v2-turbo`, temperature 0). A lane you ask for **by name** — nothing falls back to it, deliberately |
| **`local-orch`** | host GPU | The smart local model (Qwen 3.8-27B nvfp4 + MTP speculative draft) |
| **`local-sub`** | host GPU | The cheap local fan-out model (Nemotron 3 Nano 30B A3B NVFP4) |

A lane **name is the contract** — the model behind it is swappable on the host without editing a single client, which is why lanes are named for their *role*, not a model id: clients just send `{"model":"local-sub",…}` to the endpoint like any OpenAI model.

**How it fits together.** LiteLLM on `:8090` is the only door. The three GPU lanes are `mlx_vlm.server` processes on internal loopback ports that LiteLLM fronts as ordinary OpenAI-compatible backends — a local and a cloud model are indistinguishable to a client apart from the name it asks for. The extraction lane can also run **beside** the stack on its own door (`:8094`) with `ferry up --schematron`, so a scraper workload never disturbs the main endpoint. The first run seeds `~/.config/ferry/litellm.yaml` from [`litellm-route-example.yaml`](litellm-route-example.yaml) and **stops** for you to edit it — the `domestic.heavy`/`domestic.medium` primaries log in through the ChatGPT device-code session (no API key); the OpenRouter routes want `OPENROUTER_API_KEY` — then re-run.

Routing rules the template ships with (each with full forensic detail in [Deep dives](docs/deep-dives.md#route-config-forensics)):

- **Every cloud lane has a fallback entry**; the local lanes are deliberately outside every chain — a stopped GPU lane surfaces as an error rather than quietly spending a cloud quota.
- **Worker pools load-balance**: deployments sharing a `model_name` form a pool — least-used spread, automatic 429 cooldown — and OpenRouter deployments route to the fastest provider (`provider.sort: throughput`), re-ranked on OpenRouter's side every request.
- **Only lanes are advertised**: `/v1/models` lists lanes marked `model_info: {public: true}` and never a fallback hop, via a small ASGI filter — not a second process.
- **⚠ An alias has no fallback chain** — litellm resolves fallbacks by the raw model string, *before* alias resolution. Duplicate the deployment instead of aliasing it.
- ChatGPT-bridge lanes carry **ferry's neutral preamble** instead of litellm's injected Codex prompt — [what replaces it and how to verify](docs/deep-dives.md#claude-code-wiring--the-chatgpt-bridge).
- LiteLLM only **routes and fails over** — the agent logic lives in your client, and the bundled skills ([`add-fallback-orchestrator`](.claude/skills/add-fallback-orchestrator/SKILL.md), [`add-worker-model`](.claude/skills/add-worker-model/SKILL.md)) walk Claude Code through editing your `litellm.yaml` correctly.

**opencode auto-wiring.** `ferry opencode` takes opencode's config over so **every** agent routes through the host (`--local` picks the GPU pair). It is a **surgical takeover, not a merge**: four keys are replaced outright (`model` → `ferry/<driver>`, `small_model` → the inexpensive lane, `permission`, `agent`); everything else — `mcp`, `lsp`, `theme`, your own keys — is left exactly as it was, and the previous config is snapshotted before every write. Agent → lane map:

| role | agents | cloud | GPU |
|---|---|---|---|
| driver | `build`, `plan` | `heavy` | `local-orch` |
| light (0-50) | `light` | `flash` | `local-sub` |
| standard (51-100) | `standard` | `medium` when advertised; otherwise `flash` | `local-sub` |
| explore | `explore` | `flash` | `local-sub` |
| compaction / title / summary | `compaction`, `title`, `summary` | `super-flash` | `local-sub` |

The goal plugin the takeover installs has its own forensic history — [install internals](docs/deep-dives.md#the-opencode-goal-plugin-install-internals).
## Fleets

A **fleet** is a complete routing set — a primary and a fallback entry for every cloud lane (`heavy`, `medium`, `flash`, `super-flash`) — living in the same `litellm.yaml`, distinguished only by a `<fleet>.<lane>` prefix on deployment names. Clients keep sending bare lane names exactly as before; the front door resolves each request from an explicit `X-Ferry-Fleet` header, the caller's own sticky selection, or the host-wide default. Any session can move between fleets without a config edit or a restart.

| Template fleet | `heavy` | `medium` | `flash` | `super-flash` |
|---|---|---|---|---|
| `domestic` | GPT-6 Astra → GPT-5.6 Sol, both on the ChatGPT subscription at `xhigh` | GPT-5.6 Terra on the ChatGPT subscription (`xhigh`) → GPT-5.6 Terra on OpenRouter (`xhigh`); used by standard when advertised | OpenRouter GPT-5.6 Luna (`xhigh`) → Gemini Flash Latest (`xhigh`) → GPT-5.6 Terra (`xhigh`); used by light and explore | `openrouter/~google/gemini-flash-latest` (`minimal`, throughput); deliberate empty fallback list; used by compaction/title/summary |

```bash
ferry fleet ls                    # list fleets, primaries, the default, and `keys missing` if unset
ferry fleet show                  # who am I, my resolved fleet, every client's selection
ferry fleet use international     # this caller follows `international` from now on
FERRY_FLEET=international opencode-super   # one-shot pin, regardless of sticky selection
```

Fleet internals — sticky-selection vs `FERRY_FLEET` visibility, the headerless-Tailscale edge case, international-fleet guidance — in [Deep dives](docs/deep-dives.md#fleets--configuration-detail).

## The local GPU lanes

All three GPU lanes run under `mlx-vlm` and start together with `ferry up`; each can also be served alone on `:8090` with `ferry up --local-orch` / `--local-sub` / `--local-schematron`.

- **`local-orch` — Qwen 3.8-27B nvfp4** (~15 GB) with an MTP speculative draft model. The heavier, more capable local model, and the only local lane with a drafter.
- **`local-sub` — NVIDIA Nemotron 3 Nano 30B A3B NVFP4** (~18 GB). A `nemotron_h` hybrid MoE whose KV cache is ~6 KB/token — under 1 GB per 128k-token agent stream — which is what makes it the right lane for **concurrent subagents**.
- **`schematron` — Schematron-8B, 8-bit MLX** (~8.5 GB). An HTML→JSON structured-extraction fine-tune of Llama-3.1-8B with an unquantized KV cache for verbatim copying out of the prompt. The model expects the JSON schema inside the user message; ferry fronts it verbatim and rewrites no prompts.

All three are **defaults** — swap any of them for an MLX-compatible model your Mac's unified memory can hold via `LOCAL_MODEL_ORCH` / `LOCAL_MODEL_SUB` / `LOCAL_MODEL_SCHEMATRON` in `lib/ferry-core.zsh` (then `./build.zsh`). Running all three keeps ~42 GB of weights resident before any KV cache; the governor below keeps that safe, with per-lane overrides (`LOCAL_SUB_MAX_KV`, …) to shrink one lane without touching the others.

## Dashboards & observability

```bash
ferry dash --open              # live web dashboard at http://localhost:8091
FERRY_EVENTS=on ferry up       # arm the per-request event tap, then re-open dash
ferry dash --grafana --open    # full Grafana + VictoriaMetrics + VictoriaLogs stack
```

- **Signal Studio** puts the configured model library, editable fallback routes, and live traffic in one local workspace. Its library also searches the live [public OpenRouter catalog](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties) — model IDs, context length, pricing, capabilities. Drag or tap to add/reorder/copy hops, undo, keep the primary pinned until you explicitly promote another backend, then **Edit → Preview changes → Apply** with a snapshot saved before writing. Fleet tabs filter the routes in view; tablet and phone layouts included. [Open the Signal Studio guide →](docs/signal-studio.md)
- **Live traffic** (event tap on): every public lane drawn as its chain of hops — the served hop lit green, the hops it walked past lit red with the status code that pushed it on — plus per-deployment health and a feed of the last 200 requests with first-text latency, total duration, streaming mode, and reported input/output/reasoning tokens. The tap is **off by default**, forwards every request unmodified, and drops rather than ever blocking a response.
- **Schema repair, recorded**: the front patches tool schemas a provider is known to reject *without an error* (e.g. Gemini's `array_without_items`) and records what it found — forensics in [Deep dives](docs/deep-dives.md#event-tap-schema-repair--attribution).
- **Grafana stack** on localhost (`:3001`, login `admin` / `ferry-observ`): request-rate, error-rate, and latency dashboards, per-model usage (requests, tokens, spend), a **Failures & Fallbacks** view, and searchable proxy logs, persisting across sessions. All OSS, $0. See [`observ/README.md`](observ/README.md).

## Encrypted transfer off the LAN — `ferry drop` / `ferry pickup`

For machines that aren't on your network at all — a cloud desktop, a VDI, a locked-down work laptop that can only make outbound requests:

```bash
ferry drop brief.md                   # -> brief.md.ferrydrop + a fresh passphrase
ferry drop --msg "the API is at :8090"
# on the other machine, once the blob has arrived by any means at all:
ferry pickup brief.md.ferrydrop
```

Ferry supplies **confidentiality, not delivery** — a deliberate limit that keeps it free of any account, credential file, or third-party service. The blob is AES-256-CBC with PBKDF2 (600k iterations) plus an HMAC-SHA256 over header and ciphertext, verified *before* the decrypt path runs; a modified blob fails closed. **The passphrase is the entire security boundary**, so send it by a *different* channel than the blob. Needs only `openssl` (stock macOS LibreSSL and OpenSSL 3.x blobs are mutually decryptable). Full format and exit-code detail: [Deep dives](docs/deep-dives.md#encrypted-drop--crypto-detail).

## Ports

**8090** endpoint · **8091** dashboard · **8094** extraction door · **8095** LAN share · **8096** HF proxy · **8097** forward proxy · **8098** relay · **8099** VNC viewer · **8092/8093/8100** internal MLX backends · **9099** netcat — the full table with who starts each: [Deep dives](docs/deep-dives.md#ports).

## Ferrying models & files across the LAN

`ferry` moves whole models (from the host's local HuggingFace cache) and arbitrary files/dirs from the **host** to a **client**, over three transports:

```bash
ferry pull mlx-community/Qwen3.8-27B-nvfp4 --host your-mac.local   # http: stream from the host's HF cache (8095)
ferry pull org/model --host your-mac.local --transport hf          # EXPERIMENTAL: through the host's HF proxy
ferry offer ~/datasets/eval.jsonl                                  # host: record files for clients
ferry get eval.jsonl --host your-mac.local --to ./data             # client: fetch by basename
ferry receive --port 9099 --to ./incoming                          # direct push: client listens (netcat)
ferry send ~/some/dir client-laptop.local --port 9099              # ...then the host pushes
curl -fsS http://your-mac.local:8095/manifest                      # plain curl too: cached models + offered files
```

`ferry serve-hf` (experimental) is a pass-through proxy to `https://huggingface.co` (port 8096, LFS→CDN redirects followed), so a client with `HF_ENDPOINT=http://<host>:8096` downloads *through* the host.

## Route a client's downloads through the host

A client with no (or limited) internet pulls its own dependencies and models *through* the host — **anything that honors the standard proxy env vars**, routed via the host's own connection, no caching:

```bash
ferry serve-proxy                            # host
eval "$(ferry env)"                          # client: HTTP(S)_PROXY / HF_ENDPOINT / NO_PROXY exports
uvx whosaid ...                              # uv/PyPI, huggingface_hub, git, curl — via the host
```

`ferry env` stays `eval`-able (`--write` persists into `~/.zshrc`); HTTPS goes via `CONNECT` tunneling with backpressure, so a CDN pushing a multi-GB model cannot outrun a slower LAN client.

## Reverse expose: publish a client's port through the host

Every other feature pushes **host → client**. This is the missing direction: a locked-down laptop that can only make **outbound** connections dials the host, and the host does the listening.

```bash
ferry relay                                  # host: accept registrations, publish ports
ferry expose 4290 --as 4290 --token <token>  # client: serve 127.0.0.1:4290 from the host

ferry expose-vnc --token <token>   # client: publish the screen (RFB preflight, kind: vnc)
ferry serve-vnc --fetch            # host, once: download the pinned noVNC release
ferry serve-vnc                    # host: browser VNC viewer + WebSocket bridge on 8099
```

The token authenticates the client that *registers* — expose something with its own auth. Ferry's own ports are refused as publish targets outright. Published ports bind the LAN by default (`--bind 127.0.0.1` keeps an exposure host-local); `ferry status` lists them, `ferry down` tears the relay down. How the bytes move, teardown semantics, and the VNC security model: [Deep dives](docs/deep-dives.md#reverse-expose--how-the-bytes-move).

## Remote access (Tailscale)

The endpoint is a LAN appliance; ferry publishes nothing to the internet. When you want it from *outside* the LAN, front it with [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve) — one command on the host puts a real TLS certificate and your tailnet's identity in front of the same local port:

```bash
# host: serve the endpoint over the tailnet
tailscale serve --bg --https=443 http://127.0.0.1:8090

# client: re-point client.json at "your-mac.<tailnet>.ts.net", then regenerate with the real key
ferry opencode --key <master-key>     # or: ferry claude --key <master-key>
```

**What this does not cover:** the share server (`8095` — bootstrap, `pull`/`get`, `/hq` telemetry), the relay (`8098`), and the download proxies stay LAN-only — a remote client can drive inference but cannot bootstrap, ferry files, or send telemetry. This is a documented recipe, not an integration: ferry does not install, start, or manage Tailscale for you.

## Local models — operating notes

**KV-cache memory governor:** local launches ship with `--kv-bits 4`, `--max-kv-size 131072`, `--max-num-seqs 4`, and `APC_NUM_BLOCKS=512`. Measured on a 128GB M5 Max during a 121k-token agentic session: peak GPU footprint dropped **97 GB → 56 GB**, idle retained memory fell **57 GB → 35 GB**, and decode ran **~60% faster**. Monitor live usage with `footprint <pid>` (`ps` RSS does not show Metal wired memory) — `ferry status` prints it per lane. Disable any knob by setting it to `""` in `lib/ferry-core.zsh`, or govern one lane only with the per-lane overrides (`LOCAL_ORCH_MAX_KV`, `LOCAL_SUB_MAX_SEQS`, …).

Measured known issues — the `local-orch` deep-prefill streaming crash (self-recovering), the MTP-draft + quantized-KV crash and the config it shipped to avoid it, empty `/compact` summaries on huge sessions, and the auto-patched `nemotron_h` batching bug — live in [Deep dives](docs/deep-dives.md#local-model-known-issues).

## Platform support

| Platform | Local MLX serving | Cloud proxy · route · dash · client wiring · LAN share/transfer |
|---|:---:|:---:|
| **macOS (Apple Silicon)** | ✓ | ✓ |
| **Linux / Ubuntu** | — *(macOS only)* | ✓ |

**Local GPU serving uses Apple MLX and is macOS / Apple Silicon only.** On Linux, plain `ferry up` automatically degrades to the cloud lanes; serve models with `--route`, `--cloud`, or `--model <id>` against a cloud / OpenAI-compatible endpoint instead. `ferry install` on Ubuntu skips MLX and the model downloads, and may prompt you to `apt install zsh` (ferry is a zsh script); `avahi-daemon` (so `.local` mDNS names resolve) and `iproute2` are recommended.

## Privacy

Everything runs on your own hardware and network. The front door answers only requests carrying the **master key** — one shared secret you set in `LITELLM_MASTER_KEY` and every client holds a copy of (a keyless request gets a 401). The LAN transport is still **plain HTTP**, so that key travels in a header anyone sharing the wire can read: it is an auth layer, not encryption — enough to keep a neighbor's laptop or a misaddressed `curl` out, not enough for a hostile network. The hostile-network answer is [Tailscale Serve](#remote-access-tailscale). The MLX servers bind `127.0.0.1`, so the GPU lanes are reachable only through the front door. Cloud calls go host→provider over HTTPS using the host's keys, so **client devices never see the provider keys** — the master key is the one credential a client holds. The one transport built for an untrusted channel is `ferry drop` / `ferry pickup`, which encrypts before the data leaves the machine. Client telemetry (`ferry msg` / `ferry log`) is appended to `~/.config/ferry/client_logs.txt` on the host, outside any checkout. The observability stack binds to `127.0.0.1` only. A port published with `ferry relay` is reachable by anything that can reach the host on that port — whatever you `ferry expose` must carry its own authentication.

## Command reference

| Command | Mode | What it does |
|---|---|---|
| `install` | host | Install `uv`, `litellm` (+ `mlx-vlm` & default models on macOS), link `ferry` globally |
| `up [-c\|-m <id>\|-r\|--schematron\|--local-*\|-i]` / `down [--port P]` | host | **No args → the full stack**: all eight lanes on `8090`; `-r` → cloud only; `--local-*` → one GPU lane raw; `--schematron` → the extraction lane on its own door (`8094`); `-i` → interactive catalog. `down` stops everything; `--port P` retires one door |
| `status` | both | Host: per-lane listeners, memory, and served lane names. Client: connection health + the host's lanes |
| `update [--full] [--host\|--client] [--dry-run]` | both | Catch this machine up (host rebuilds and re-links, client re-pulls). `--full` also reloads the GPU lanes |
| `dash [--open] [--port P] [--ferry URL]` | host | Live route-proxy dashboard on `8091` (`--grafana` → full Grafana/VictoriaMetrics stack; also standalone `ferry-dash`) |
| `share` | host | Serve the client bootstrap + ferry transfer routes over the LAN (`8095`) |
| `auth-claude login\|status\|refresh\|logout` | host | Manage the Claude Pro/Max subscription OAuth credential |
| `msg <text>` / `log` / `inbox` | client / host | Send a note or pipe stdin to the host's log; read it back dated and attributed |
| `fleet ls\|show\|use <name>` | both | List fleets, show resolved selections, set a caller's sticky fleet |
| `relay` / `expose <port>` / `expose-vnc` | host / client | Reverse expose: client dials out, host publishes its port (RFB preflight for VNC) |
| `serve-vnc [--bind ADDR] [--fetch]` | host | Browser VNC viewer + WebSocket bridge (default `8099`) |
| `offer <path>...` / `get <name>` | host / client | Record files for clients; fetch an offered file/dir by basename |
| `pull <model-id> [--transport http\|hf\|nc]` | client | Pull a model from the host cache (three transports) |
| `receive` / `send <path> <client-host>` | client / host | Netcat tar stream (default port `9099`) |
| `serve-hf` / `serve-proxy` / `env [--write]` | host / client | HF pass-through (8096) + HTTP(S) forward proxy (8097); `env` emits the client's proxy exports |
| `drop <path>\|--msg <text>` / `pickup <blob>` | any | Encrypted off-LAN transfer (AES-256-CBC + HMAC, openssl) |
| `opencode [--local\|--cloud] [--key KEY] [--model M] [--small-model SM] [--housekeeper HK] [--super] [--keep N] [--no-default]` | dual | Take the opencode config over: agents pinned to lane names, `general` disabled, `light`/`standard` subagents added, snapshots first. `--key` writes the master key into the configs |
| `claude [--key KEY] [--wrappers]` | dual | Install the `claude-ferry*` wrappers pointing Claude Code at the ferry endpoint by lane name |
| `migrate [--dry-run] [--full] [--dir D]` | client | Promote this client into a host of its own ([how](docs/deep-dives.md#promoting-a-client-into-a-host)) |

`ferry --help` prints the built-in usage banner.

## FAQ

**Does any of my data leave the LAN?** Local-lane inference never leaves the host; cloud lanes call the provider from the host over HTTPS with the host's keys. Client↔host traffic is plain HTTP on your private network behind one shared master key — for hostile networks, front the endpoint with [Tailscale](#remote-access-tailscale). See [Privacy](#privacy).

**Does it run on Linux?** The CLI, cloud proxy, dashboards, and LAN share/transfer run on macOS and Linux/Ubuntu. Local MLX GPU serving is macOS / Apple Silicon only — on Linux, `ferry up` degrades to the cloud lanes automatically. See [Platform support](#platform-support).

**Do clients need API keys?** No. Clients hold exactly one shared master key for the front door; provider keys and OAuth subscription logins exist only on the host.

## Contributing

Issues and PRs are welcome — [open an issue](https://github.com/sblattj/llm-ferry/issues) for bugs, feature ideas, or provider-compatibility findings (they feed the [route forensics](docs/deep-dives.md#route-config-forensics)). Release notes live in [docs/releases/](docs/releases).

## Acknowledgments

- [LiteLLM](https://github.com/BerriAI/litellm) — ferry is built on its proxy routing, fallbacks, and provider adapters.
- [MLX](https://github.com/ml-explore/mlx) — Apple's machine-learning framework powering the local GPU lanes.

## Development

`ferry` is assembled from 18 per-domain modules in [`lib/`](lib/). The shipped `ferry` is a **generated** single file — clients fetch it as one script over the LAN — so edit the modules, regenerate, and commit both (`build.zsh --check` flags drift; don't hand-edit `ferry`):

```bash
./build.zsh --check    # regenerate ./ferry from lib/ferry-*.zsh; fail on drift
for suite in lib/*.test.py observ/*.test.py; do python3 "$suite" || exit 1; done
node lib/ferry-dashui.test.mjs
```

How the suites are designed — real embedded Python against a throwaway `$HOME`, the client-scope end-to-end runs — is in [Deep dives](docs/deep-dives.md#test-suite-detail).

## License

MIT — see [LICENSE](LICENSE). © 2026 Stephen Blatt.
