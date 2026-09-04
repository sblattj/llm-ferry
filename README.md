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
- 🔒 **Mac/Linux host, LAN-only, your hardware.** Client↔host traffic is plain HTTP on your private network behind one shared master key (v1.22); cloud calls go host→provider over HTTPS with the host's keys. This is not a public gateway or a hosted service — and that's the point.

## Why not just…?

`llm-ferry` is built **on** LiteLLM and MLX — it's the glue that turns them into a shared LAN appliance. Honest comparison of **focus**, not "better":

| | Per-device API keys | Ollama / LM Studio | Raw LiteLLM proxy | OpenRouter (hosted) | **llm-ferry** |
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

- 🌐 **One endpoint, every device** — OpenAI-compatible (`/v1/chat/completions`, `/v1/models`); Anthropic `/v1/messages` too, so **Claude Code runs on the ferry backend** (`claude-ferry` wrapper, new in v1.20).
- 🔑 **Keys stay on the host** — your *provider* keys never leave the host; clients hold one shared master key (v1.22) and never see a provider key.
- ⚡ **Local GPU + cloud, same endpoint** — Apple MLX inference on the Mac, or a cloud proxy, or **both models on one route config**.
- 🧠 **Driver lane, no silent failover** — a big planning model (`heavy`) on the ChatGPT subscription with **no** fallback chain, by design: a driver call errors rather than silently continuing the session on a different model. The **worker** lanes (`flash`, `super-flash`) run OpenRouter's `~google/gemini-flash-latest` alias (currently Gemini 3.8 Flash), with `flash` using xhigh reasoning and a Terra fallback, and `super-flash` using minimal reasoning and a Luna fallback.
- 🎛️ **Multi-key worker pool** — several API keys pooled with `usage-based-routing-v2` (proactive least-used spread) and automatic 429 cooldown/failover.
- 🚀 **One-curl client onboarding** — `curl … | zsh` installs the CLI, writes the client profile, and auto-wires the editor (opencode / Continue / Cursor).
- 📊 **Observability, new in v1.5** — a zero-dependency stdlib live page **and** a full Grafana + VictoriaMetrics + VictoriaLogs stack (per-model requests/tokens/spend/latency, a Failures & Fallbacks view, searchable logs).
- 📦 **Ferry models & files across the LAN** — stream whole models from the host's HuggingFace cache, offer/fetch arbitrary files, or push over netcat.
- 🕳️ **Forward proxy for offline clients** — route a client's uv/PyPI/HuggingFace/git downloads through the host's connection.
- 🔄 **Reverse tunnel for locked-down clients** — publish one of a client's own local ports through the host, with the client only ever dialling out (`ferry relay` on the host, `ferry expose <port>` on the client).
- 🔐 **Encrypted drop for machines off the LAN, new in v1.17** — `ferry drop` writes an authenticated, self-contained blob you can move over any channel ferry doesn't trust; `ferry pickup` verifies and decrypts it. The passphrase, not the carrier, is the security boundary.
- 🪶 **Single-file CLI** — `zsh` + `python3` **standard library** only; `litellm`/`mlx` installed via `uv` only when you actually serve inference (plus `openssl`, an OS-provided binary, for `drop`/`pickup`). Clients fetch the CLI as one script over the LAN.

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
export OPENROUTER_API_KEY="..."      # or drop it in ~/.config/ferry/secrets.env
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

`ferry share` prints both the `.local` name **and** the raw LAN IP — use the IP form if `.local` doesn't resolve on your network. The bootstrapper is non-interactive when the host is reachable: it installs the `ferry` CLI to `~/.local/bin`, writes `~/.config/ferry/client.json`, wires opencode to the host endpoint (cloud pair as the persistent default), and adds a `host-code` shell shortcut. It also installs three opencode lane shortcuts into `~/.zshrc` (idempotent, per-invocation):

- `opencode-cloud` — the **cloud pair**: `heavy` drives (build/plan), `flash` runs the fan-out (general/explore), `super-flash` handles the background models (title/summary/compaction).
- `opencode-local` — the **GPU pair**: `local-orch` drives, `local-sub` runs the fan-out. Nothing leaves the host.
- `opencode-super` — the **cheapest cloud profile**, new in v1.21: `heavy` still drives, but `super-flash` runs **both** the fan-out and the background models.
- bare `opencode` — whichever profile you used **last** (cloud until you pick another; the last-used lane is remembered in `~/.config/ferry/last-lane`).

Both need `ferry up` on the host, which serves all five lanes at once.

**Claude Code works too, as of v1.20.** The ferry endpoint speaks the Anthropic
`/v1/messages` protocol, so Claude Code can run on the ferry backend with no
changes to `claude` itself. When `claude` is installed, the bootstrap also
installs three wrappers into `~/.zshrc` (skip with `--no-claude`):

- `claude-ferry` — the **cloud lanes**: `heavy` drives, `flash` covers background
  tasks and subagents.
- `claude-ferry-local` — the **GPU lanes**: `local-orch` drives, `local-sub` fans
  out. Nothing leaves the host.
- `claude-ferry-super` — the **cheapest cloud profile**, new in v1.21: `heavy`
  drives, `super-flash` covers background tasks and subagents.

Both point Claude Code at the host with `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`
scoped to the child process, with the compatibility flags a non-Anthropic backend
needs (`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`, and
`CLAUDE_CODE_DISABLE_THINKING=1` on the local lanes). Bare `claude` is deliberately
untouched — it's your personal tool. The host gets the same wrappers via
`ferry install` / `ferry update`, pointed at `127.0.0.1:8090`, and
`client-reset.sh` re-applies the wiring so `ferry update` delivers it to existing
clients (re-run the bootstrap one-liner only if the shell block itself needs
refreshing).

**The host gets these too, as of v1.17.** `ferry opencode` deliberately wires the
host to its own endpoint, so the host drives the local lanes exactly like a client
does — but the wrappers were written only by `client-bootstrap.sh`, leaving a host
with the two profile files and no way to select between them. `ferry install` and
`ferry update` now install them (named wrappers only; bare `opencode` is left
alone, since a host that exports `OPENCODE_CONFIG` chose that deliberately). They
land under the same `# >>> ferry opencode profiles >>>` marker the client uses, so
a hand-wired block from before this change is absorbed rather than duplicated.

#### How much of opencode it takes over

The default above assumes the client's opencode is yours to wire. On a laptop that already has its own opencode setup, narrow the scope — the flag goes after `zsh -s --`:

```bash
curl -fsSL http://your-mac.local:8095/client-bootstrap.sh | zsh -s -- --profiles-only
curl -fsSL http://your-mac.local:8095/client-bootstrap.sh | zsh -s -- --no-opencode
```

| Scope | Writes | Leaves alone |
|---|---|---|
| *(default)* | `~/.config/opencode/opencode.json`, both ferry profiles, all three `~/.zshrc` wrappers, the guardrail files | — |
| `--profiles-only` | `~/.config/ferry/opencode-{cloud,local}.json`, the two **named** wrappers, `host-code` → `opencode-cloud` | everything under `~/.config/opencode`; bare `opencode` keeps your config |
| `--no-opencode` | the `ferry` CLI and `~/.config/ferry/client.json`, nothing else | every opencode file, ferry's own profiles included |

Under `--profiles-only` the lanes are opt-in per invocation — `opencode-cloud` / `opencode-local`, or `OPENCODE_CONFIG=~/.config/ferry/opencode-cloud.json opencode …` without the wrappers.

The local-lane guardrails (`/fan-out` and the `spawning-subagents` skill) live in `~/.config/opencode/`, so they follow the scope: on by default, off in the two narrow modes. They only *add* files, so `--with-guardrails` opts back into them, and `--no-guardrails` out.

The chosen scope is recorded in `client.json` as `opencode_mode`, which is what keeps the catch-up below from silently re-widening the machine.

#### Catching a client up later

When the host changes — new lanes, a re-pointed alias, a newer `ferry` — an already-bootstrapped client catches up with:

```bash
curl -fsSL http://your-mac.local:8095/client-reset.sh | zsh
```

It re-pulls the CLI and re-applies the opencode takeover. It does **not** touch `~/.zshrc`, rewrite `client.json`, or prompt for anything — it reuses the profile the bootstrap left behind. Re-run the bootstrap instead if the machine is new or the shell wrappers need refreshing.

**It re-applies the scope, not the maximum.** `opencode_mode` from `client.json` decides which configs get written: `full` (or a profile from before the key existed) does all three, `profiles` does only ferry's own two, `none` re-pulls the CLI and writes no config at all. The same three flags override it for one run — `client-reset.sh --profiles-only` — without rewriting the profile, so an override can't quietly redefine the machine.

**Why the CLI is re-pulled first, always:** `ferry opencode` is what performs the takeover, so an out-of-date CLI quietly does the *old* thing and reports success either way. The download is validated (shebang, `cmd_opencode` present, `zsh -n` clean) before it replaces the working binary, so a share server that is down cannot leave you with an HTML error page named `ferry`.

#### Removing it from a client

```bash
curl -fsSL http://your-mac.local:8095/client-cleanup.sh | zsh -s -- --dry-run   # print, change nothing
curl -fsSL http://your-mac.local:8095/client-cleanup.sh | zsh                   # apply
```

The inverse of the bootstrap, and scope-agnostic: it removes whatever is actually there, so it undoes a default install, a `--profiles-only` one, and a `--no-opencode` one without being told which. Out go the `ferry` CLI, `~/.config/ferry` (profile, lane profiles, snapshots, telemetry), the `~/.zshrc` wrapper block and `host-code` alias, and the guardrail files — under both the `skill/` and `skills/` spellings, since the two installers disagree.

It edits `~/.config/opencode/opencode.json` **surgically**: only the provider ferry wrote (the `ferry` provider entry) is removed, the file is snapshotted to `.<UTC>.jsonc` first, and a config with no ferry provider in it is left byte-identical. Your own providers, MCP servers and commands survive.

Two things it deliberately keeps: the `opencode` binary (not ferry's to uninstall) and `~/.local/share/opencode`, your session history — `--full --yes` is the only way to delete that, and `--full` without `--yes` is refused outright so a piped fat-finger can't wipe it.

Then:

```bash
ferry status                     # connection health + the lanes the host serves
ferry msg "note"                 # send a quick note to the host's log
some-command 2>&1 | ferry log    # stream logs/errors back to the host
```

That's it — every editor and CLI on the client now talks to one endpoint on the host.

#### Reading what the clients sent

On the **host**:

```bash
ferry inbox            # index the 20 most recent entries, dated and attributed
ferry inbox -n 3       # the last three, in full
ferry inbox -f         # follow new ones as they land
ferry inbox --all      # every entry
ferry inbox --path     # the two files this reads
```

The answer lives in two files and neither holds all of it. `~/.config/ferry/client_logs.txt` has every body verbatim, append-only — but the `/hq` handler writes a delimiter and the body, **no timestamp and no client IP**. The share server's access log has both, and is **truncated on every `ferry share` restart**. So `ferry inbox` aligns them from the *end*: the receipts still in the access log belong to the most recent entries, and everything older is printed as `—` rather than given a borrowed date.

Two details that would otherwise skew it: a `POST /hq` that returned non-200 means the handler raised and **no entry was written**, so those are counted separately (`WARNING: N POST(s) … returned an error`) instead of consuming a slot; and every `share-*.log` is read, not just the default port's, because `ferry share` scans upward when its port is taken.

```
 11  28/Aug 17:47  192.168.1.42     ########## HANDOFF FILE: prxref-HANDOFF.md ##########
 12  28/Aug 17:47  192.168.1.42     ########## HANDOFF FILE: reverse-expose-handoff.md ##
 13  28/Aug 17:50  127.0.0.1        self-test
```

### More host commands

```bash
ferry up             # THE STACK: all five lanes on one endpoint (port 8090)
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
ferry update --host         # update this existing host: rebuild, re-link, bounce proxy
ferry update --host --full  # same, plus reload ~33GB of GPU weights (slow, optional)
```

Update the host first, then catch each existing client up from that host:

```bash
ferry update --client       # update this existing client from its configured host
ferry status                 # verify host/client connectivity and served lanes
curl -sS http://your-mac.local:8090/v1/models  # add Authorization if the host requires it
```

The lower-level and recovery forms remain available when needed:

```bash
./host-reset.sh --no-pull   # host recovery/offline form; skips the git fast-forward
./host-reset.sh              # lower-level host reset (fast-forward, rebuild, re-link, bounce)
./host-reset.sh --full      # lower-level reset plus the slow ~33GB GPU reload
curl -fsSL http://your-mac.local:8095/client-reset.sh | zsh  # lower-level client reset
```

By default the MLX lanes are **left running** — `ferry up --route` re-reads the same `litellm.yaml` the stack uses, and litellm reaches the GPU lanes over HTTP on loopback, so a lane does not care that its front door restarted. Only `--full` reloads ~33GB of weights.

**The route config is validated before anything live is touched.** litellm does not check its config beyond parsing it, so a duplicate key, a dangling `model_group_alias`, a fallback naming a lane that does not exist, or an unset `os.environ/…` reference all start cleanly and then fail at request time, on one lane, looking exactly like a provider outage. `host-reset.sh` checks all four while the old proxy is still serving and aborts without restarting anything, so a bad edit costs a failed reset rather than an endpoint.

It then re-applies the opencode takeover to the host's own three configs — wiring the host to its own endpoint is the point of running one — and verifies against the live catalogue that every lane those configs name actually resolves. Hidden aliases are counted as resolvable (they never appear in `/v1/models` by design), and the local backends are probed **directly** on their own ports, because litellm lists `local-orch`/`local-sub` whether or not an MLX server is behind them.

`git pull --ff-only` runs first and never rebases or merges — divergence and uncommitted changes to tracked files stop the run, since both are decisions for a human. Being offline only warns.

---

<sub>Everything below is the full reference — route configs, fallback chains, dashboards, file/model ferrying, and the forward proxy.</sub>

## Contents

- [The stack — five lanes on one endpoint](#the-stack--five-lanes-on-one-endpoint)
- [Fleets](#fleets)
- [The local GPU lanes](#the-local-gpu-lanes)
- [Dashboards & observability](#dashboards--observability)
- [Ports](#ports)
- [Ferrying models & files across the LAN](#ferrying-models--files-across-the-lan)
- [Route a client's downloads through the host](#route-a-clients-downloads-through-the-host)
- [Reverse expose: publish a client's port through the host](#reverse-expose-publish-a-clients-port-through-the-host)
- [Remote access (Tailscale)](#remote-access-tailscale)
- [Local models](#local-models)
- [Platform support](#platform-support)
- [Privacy](#privacy)
- [Command reference](#command-reference)
- [Development](#development)
- [License](#license)

## The stack — five lanes on one endpoint

`ferry up -c/-m` serves **one** model. Plain **`ferry up`** serves the **stack**: five named **lanes** on a single OpenAI-compatible endpoint, driven by a [LiteLLM config](https://docs.litellm.ai/docs/proxy/configs) plus two local MLX servers.

| Lane | Where it runs | What it is |
|---|---|---|
| **`heavy`** | cloud | The big driving model, with a strict **fallback chain** to independent providers |
| **`flash`** | cloud | Cheap high-volume worker (`~google/gemini-flash-latest`, currently Gemini 3.8 Flash); xhigh reasoning, Terra fallback |
| **`super-flash`** | cloud | Housekeeping — `title`, `summary`, `compaction`, on their own chain |
| **`local-orch`** | host GPU | The smart local model (Qwen 3.8-27B nvfp4 + MTP speculative draft) |
| **`local-sub`** | host GPU | The cheap local fan-out model (Nemotron 3 Nano 30B A3B NVFP4) |

```bash
ferry up      # all five, on http://<host>.local:8090/v1
```

A lane **name is the contract**. The model behind it is swappable on the host without editing a single client — that is why the lanes are named for their *role* rather than for a model id. Clients just name a lane:

```bash
curl -s http://your-mac.local:8090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-sub","messages":[{"role":"user","content":"hi"}]}'
```

**How it fits together.** LiteLLM on `:8090` is the only door. The two GPU lanes are `mlx_vlm.server` processes on internal loopback ports (`8092`, `8093`) that LiteLLM fronts as ordinary OpenAI-compatible backends — so a local model and a cloud model are indistinguishable to a client apart from the name it asks for.

The first run seeds `~/.config/ferry/litellm.yaml` from [`litellm-route-example.yaml`](litellm-route-example.yaml) and **stops** so you can edit it — the `domestic.heavy` driver (its legacy `orch`/`orchestrator` names still resolve to it — see [Fleets](#fleets)) logs in once via device code (written to `~/.config/litellm/chatgpt/auth.json`, no API key needed), and `domestic.flash`/`domestic.super-flash` plus their Terra/Luna hops need `OPENROUTER_API_KEY` exported (in your shell or `~/.config/ferry/secrets.env`) — then re-run.

**Worker pool (load-balanced).** The template ships `flash` as one deployment (Gemini 3.8 Flash through OpenRouter, routed to the fastest provider, so the provider spread is OpenRouter's problem); any deployments you add **sharing the `flash` model_name** form a pool: `usage-based-routing-v2` sends each call to the least-used one (proactive even split), and on a `429` it cools the dead deployment out (`cooldown_time`) and rolls traffic to another. If you pool Gemini on a **native** key instead, **widen it with model ids, never with keys.** Google says it plainly — *"Rate limits are applied per project, not per API key"* — so a second key in the same project shares one bucket and buys nothing, and a second *project* to multiply the limit is circumvention under Google APIs ToS §2.d (nine burst-created projects suspended in one night, 2026-08-25, and the account's OAuth APIs restricted). But the limit is per-project-**per-model**: every model id carries its own RPM/TPM/RPD bucket, so pooling `gemini-3.8-flash` with, say, `gemini-3.5-flash` on **one** key is two independent buckets and nothing to circumvent. Pick members that are interchangeable for the lane's *role*, so a caller cannot tell which one answered. The other sanctioned lever is raising the paid tier on that one project (Tier 3 = 20M TPM).

**Only lanes are advertised.** `/v1/models` lists the lanes you mark `model_info: {public: true}` and nothing else. That matters more than it sounds: `router_settings.fallbacks` is keyed by model group, so `flash` has a Terra chain and `super-flash` has a Luna chain — a client that picks a fallback hop out of a model list gets a single provider with **no failover at all**, and only finds out when that hop is down, which is the case the chain exists for. litellm has no setting for this (`hidden` applies to `model_group_alias` entries only), so `ferry up` serves litellm's own app through a small ASGI filter (`front/ferry_front.py`) that trims the listing. It is not a second process and not a reverse proxy — every request that is not the model listing goes to litellm untouched, so nothing sits between a client and a streamed token. Hiding is not removing: an unadvertised hop is still callable by name if you ask for it. If the filter cannot start, ferry says so and serves litellm directly rather than leaving the endpoint down.

**The driver lane has no fallback, by design.** `heavy` (and the legacy `orch`/`orchestrator` names, resolved to `heavy` by the front door since fleets, 2026-09-04 — see [Fleets](#fleets)) runs on the ChatGPT subscription via litellm's native `chatgpt/` provider (`chatgpt/responses/gpt-5.6-sol`, device-code login at `~/.config/litellm/chatgpt/auth.json` — not an API key) at `reasoning_effort: xhigh`, the top effort value litellm's chat→responses bridge actually forwards (it silently drops `max`). It carries **no** `router_settings.fallbacks` entry, on purpose: a driver call that fails should error, not silently continue the session on a different model the user never chose. The ChatGPT backend is also **streaming-only** on litellm 1.99.0 — a non-streamed call `500`s — which a streaming client never notices but rules out serving `heavy` to a non-streaming caller at all.

**Each worker lane gets its own single strict fallback hop.** `flash` (`openrouter/~google/gemini-flash-latest`, currently resolving to Gemini 3.8 Flash, routed to the fastest-throughput OpenRouter provider) uses `reasoning.effort: xhigh` and falls back to `flash-terra` (`openrouter/openai/gpt-5.6-terra`); `super-flash` uses the same primary at `reasoning.effort: minimal` and falls back to `super-flash-luna` (`openrouter/openai/gpt-5.6-luna`). Each fallback fires only when its primary errors — a `429`, a `5xx`, or a hard quota `403` — and is never public.

**OpenRouter hops route to the fastest provider.** One OpenRouter model id is served by many providers — GLM 5.3 Flash by 22 on 2026-09-02, from 111 tok/s at the top to 17 at the bottom — and OpenRouter's default picks among the *cheapest* of them, weighted by inverse-square price. **Gemini 3.8 Flash** — the model behind `flash` and `super-flash` (whose `flash-luna`/`super-flash-luna` hops ride OpenRouter too) — therefore carries `extra_body: {provider: {sort: throughput}}`, which is [OpenRouter's own provider-routing object](https://openrouter.ai/docs/features/provider-routing) forwarded verbatim by litellm: every request is re-ranked by each provider's p50 tokens/s over a rolling 5-minute window, on OpenRouter's side. Nothing in ferry polls or pins a provider name, so a provider that is rate-limited *this minute* is simply not at the top this minute — pinning `order: ["Baseten"]` (the fastest on the page) returned `429 temporarily rate-limited upstream` while `sort: throughput` on the same model was served by Friendli and Fireworks at once (verified 2026-09-02 through `ferry_front.py`, with an unsorted control lane landing on Z.AI). The trade is price: throughput sort ignores it, so a model with a discounted provider may be served at full rate instead. Drop the block from any deployment you would rather run cheap than fast.

**The local lanes are deliberately outside every fallback chain.** The whole point of naming `local-orch` or `local-sub` is that the request stays on your machine — so a stopped GPU lane surfaces as an error rather than quietly spending a cloud quota. (`flash` still spills to its own `flash-terra` hop, and `super-flash` to `super-flash-luna`, cloud-to-cloud fallbacks — just never off the host's GPU.)

**⚠ An alias has no fallback chain.** `router_settings.model_group_alias` looks like the way to keep an old client-facing name working, and it does resolve — for *deployment selection* only. litellm reads the fallbacks map with the **raw model string the client sent, before any alias is resolved**:

```
router.py:6411   model_group = kwargs.get("model")      # "orchestrator"
router.py:6345   get_fallback_model_group(fallbacks, model_group)
router.py:6357   fallback_model_group is None -> raise original_exception
```

Alias → target resolution lives at `router.py:9278`, on the deployment-selection path that lookup never reaches. So an aliased lane matches no `fallbacks:` entry, its primary's error goes straight to the client, and the entire chain is skipped — silently, because the config and `/v1/models` both look correct. Verified 2026-08-28 against a live stack whose primary was quota-blocked: the alias returned `500`; the real `model_name` returned `200` from the second hop.

**Duplicate the deployment instead.** To keep a legacy name alive, give it a real `model_name` of its own rather than an alias — one extra block. (`heavy`'s own legacy names, `orch`/`orchestrator`, are the one case that needs no block at all: the front door's `LEGACY_HEAVY` map resolves them to `heavy` since fleets, 2026-09-04 — see [Fleets](#fleets).) A worker lane like `flash` still needs its **own** `fallbacks:` entry — not an alias of it — to keep failing over to its hop; here's the pattern for a hypothetical `flash-v1` rename:

```yaml
model_list:
  - model_name: flash-v1         # a legacy rename: same model, its OWN entry
    litellm_params: {model: openrouter/~google/gemini-flash-latest, api_key: os.environ/OPENROUTER_API_KEY, reasoning_effort: xhigh}
    model_info: {id: or-gemini-flash-v1}        # no public: true — not advertised

  - model_name: flash
    litellm_params: {model: openrouter/~google/gemini-flash-latest, api_key: os.environ/OPENROUTER_API_KEY, reasoning_effort: xhigh}
    model_info: {public: true, id: or-gemini-flash}
  - model_name: flash-terra      # the fallback hop — a real model_name, never public
    litellm_params: {model: openrouter/openai/gpt-5.6-terra, api_key: os.environ/OPENROUTER_API_KEY, reasoning_effort: xhigh}
    model_info: {id: or-terra-flash-fb}

router_settings:
  fallbacks: [{"flash": ["flash-terra"]}, {"flash-v1": ["flash-terra"]}, {"super-flash": ["super-flash-luna"]}]
```

`or-gemini-flash-v1`, `or-gemini-flash`, and `or-terra-flash-fb` above are just `model_info.id` — metric labels litellm stamps onto each deployment's Grafana series, not something a client ever sends.

**Never let a real model id become the name clients type.** It is tempting to name a lane after the model currently behind it, and it goes wrong the first time you re-point that lane: clients keep sending a vendor's model name and get someone else's model back, and nothing in `/v1/models` reveals the discrepancy. Name the *role* instead — a role survives the model behind it changing, which is the entire reason clients address lanes. `ferry opencode` enforces the same rule from the client side: it writes only lane names, never a model id.

**Keeping a lane out of the catalogue is a separate control** — the one an alias only appeared to offer. Omit `model_info: {public: true}` and `front/ferry_front.py` leaves the lane out of `/v1/models` while it still routes *and still keeps its chain*. Use it sparingly: an unlisted lane is invisible to everything that reads the catalogue, including ferry's own `host-reset.sh` verifier, which will report it as not served while calls to it keep succeeding. `super-flash` — the **housekeeping** lane `ferry opencode` points `title`/`summary`/`compaction` at — is a real and *advertised* `model_name` for exactly that reason: it carries compaction, and a failed compaction does not retry, it drops the whole transcript.

**Add models with Claude Code.** This repo bundles two skills — [`add-fallback-orchestrator`](.claude/skills/add-fallback-orchestrator/SKILL.md) and [`add-worker-model`](.claude/skills/add-worker-model/SKILL.md) — that walk Claude through editing your `litellm.yaml` correctly: the strict-failover-chain vs. load-balanced-pool distinction, the independent-capacity rule for fallbacks, and the per-project-quota gotcha **plus the Google ToS line a worker-key pool must not cross**. Just ask Claude Code to "add a fallback orchestrator" or "add another worker key."

> LiteLLM only **routes and fails over** — the "driver delegates to workers" agent logic lives in **your client** (opencode / Claude Code / etc.). Point it at `http://<host>.local:8090/v1` with the main model set to a driving lane (`heavy` or `local-orch`) and the subagent model to its cheap partner (`flash` or `local-sub`).

**opencode auto-wiring.** On a client, `ferry opencode` takes opencode's config over so **every** agent routes through the host. Add `--local` to pick the GPU pair instead of the cloud pair:

```bash
ferry opencode            # heavy drives, flash fans out
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
| driver | `build`, `plan` | `heavy` | `local-orch` |
| worker | `general`, `explore` | `flash` | `local-sub` |
| housekeeper | `title`, `summary`, `compaction` | `super-flash` | `local-sub` |

The housekeeping three matter more than they look. They fire on their own schedule rather than as part of a fan-out, and an unpinned `compaction` sends your *entire transcript* to whatever the default model is. Giving them their own lane also keeps a compaction — the largest single request opencode ever makes — from queueing behind a fan-out that has just saturated the worker pool. `small_model` follows the same lane, since opencode's schema describes it as the model "for tasks like title generation".

On the GPU pair there is no third lane, so the housekeeper shares `local-sub`. Point the housekeeper anywhere with `--housekeeper <lane>`.

**`agent` is replaced wholesale rather than merged**, which is the point — a stale per-agent pin is exactly the drift this ends. Before every write, the previous config is copied to `<name>.<UTC-timestamp>.jsonc` beside it (last 10 kept, `--keep N` to change), so a takeover is always reversible and any custom agent you had is recoverable. The `.jsonc` extension is deliberate: opencode's schema allows comments, and the snapshot is where they survive the rewrite.

Only the **lane pair** is ever declared as a model — never the served catalogue. The host does **not** advertise the fallback hops (`flash-terra`, `super-flash-luna`, …) — only lanes marked `model_info: {public: true}` make it into `/v1/models` — but a hop still routes by name: it's the *router* that reaches it on overflow, not a client picking one out of a menu.

## Fleets

**Fleets, new in v1.26.0.** A **fleet** is a complete routing set — a primary and a
fallback CHAIN for every cloud lane (`heavy`, `flash`, `super-flash`) — living in the same
`litellm.yaml` as every other fleet, distinguished only by a `<fleet>.<lane>` prefix on its
deployment names. Clients keep sending bare lane names exactly as before; the front door
resolves each request to a fleet, in order, from an explicit `X-Ferry-Fleet` header, the
caller's own sticky selection, or the host-wide default recorded in
`~/.config/ferry/fleets.json`. This host ships two: `domestic` (US-only models — GPT-5.6
Sol drives `heavy`, OpenRouter Gemini Flash Latest drives the workers) and `international`
(the cheapest lane across every model — flat-rate coding plans tried first, Kimi K3 and
Z.ai GLM 5.3, per-token OpenRouter last on every chain). The Codex/ChatGPT subscription is
**domestic-only**: `international` never touches it, so its shared usage limit stays
reserved for `domestic`'s driver and its two ChatGPT-bridge fallback hops (Terra, Luna).
Every chain is two hops now and, except `domestic.heavy` (which has none, by design), ends
on OpenRouter's `~openai/gpt-latest` alias as the shared last resort. Any session can move
between fleets without a config edit or a restart — the very next request after a switch
resolves to the new fleet, in every worker process. The local GPU lanes (`local-orch`,
`local-sub`) have no fleet variant; they stay unprefixed and shared, exactly as before
fleets existed.

| Fleet | `heavy` | `flash` | `super-flash` |
|---|---|---|---|
| `domestic` | GPT-5.6 Sol (ChatGPT subscription), no chain | `~google/gemini-flash-latest` via OpenRouter at `reasoning.effort: xhigh`, falls back to GPT-5.6 Terra (ChatGPT bridge, same subscription, `xhigh`), then OpenRouter GPT latest (`xhigh`) | same Gemini alias at `reasoning.effort: minimal`, falls back to GPT-5.6 Luna (ChatGPT bridge, reasoning off), then OpenRouter GPT latest (reasoning off) |
| `international` | Kimi K3 (`anthropic/k3`, `xhigh`), falls back to Z.ai GLM 5.3 (`thinking: enabled`), then OpenRouter GPT latest (`xhigh`) | Z.ai GLM 5.3 Flash (coding plan, `thinking: enabled`), falls back to `~google/gemini-flash-latest` via OpenRouter (`xhigh`), then OpenRouter GPT latest (`xhigh`) | Z.ai GLM 5.3 Flash (`thinking: disabled`), falls back to `~google/gemini-flash-latest` via OpenRouter (`minimal`), then OpenRouter GPT latest (reasoning off) |

```bash
ferry fleet ls                    # list fleets, primaries, the default, and `keys missing` if unset
ferry fleet show                  # who am I, my resolved fleet, every client's selection
ferry fleet use international     # this caller follows `international` from now on
```

One-shot pin, regardless of the sticky selection:
```bash
FERRY_FLEET=international opencode-super
```

**Two things the CLI does not show.** `ferry fleet show` reports the sticky selection recorded on the host, not a `FERRY_FLEET` pin in your shell — the pin still wins for every request it is set on (it becomes the `X-Ferry-Fleet` header), it is just invisible to `show`. And `ferry opencode` rewrites the ferry provider's `options.headers` map wholesale on every run (`X-Ferry-Client` plus the `{env:FERRY_FLEET}` placeholder), so a header you added there by hand is replaced the next time the takeover runs — put custom headers on a different provider block.

**Headerless Tailscale note.** A client reaching the host through `tailscale serve` (see
[Remote access](#remote-access-tailscale)) arrives at the front door from loopback with no
`X-Ferry-Client` header unless it has re-run bootstrap or reset since this release, so it
resolves as `host` — the HOST's own fleet, not its own sticky selection — until it
regenerates its config. This is documented behavior, not a bug to work around.

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

**Live per-request attribution — `FERRY_EVENTS=on` (new in v1.16):**

```bash
FERRY_EVENTS=on ferry up          # arm the front-door event tap
ferry dash --open                 # live lane view + per-request feed
```

The proxy access log records **no model at all** — measured over 13,182 real records — so nothing derived from it can say which deployment served a request. The tap is what supplies that: an ASGI middleware on the inference path reads litellm's own response headers and appends one NDJSON line per request. It is **off by default**, never reads a request body, forwards every message unmodified, and drops rather than ever blocking a response.

With it on, `ferry dash` gains a **Live traffic** panel — every public lane drawn as its chain of hops, the served hop lit green, the hops it walked past lit red with the status code that pushed it on, per-deployment health, and a feed of the last 200 requests — and Grafana gains a **Ferry — Lanes & Fallbacks** dashboard plus three alerts, including `Lane chain exhausted`: *every* deployment in a lane is down at once, which is the outage the old metrics could not name.

Per-deployment health (`rate_limited` / `quota_exhausted` / `auth_dead` / `unreachable`) is inferred against a classifier table you own: copy [`event-rules.example.json`](event-rules.example.json) to `~/.config/ferry/event-rules.json` and fill in what your providers actually say. **With no table every failure reads `unknown`** — visible, never silently `healthy`.

## Encrypted transfer off the LAN — `ferry drop` / `ferry pickup`

Every other transport in this README assumes the private LAN. This pair is the
exception, and it exists for the case the LAN posture cannot serve: getting a
file to a machine that isn't on your network at all — a cloud desktop, a VDI, a
locked-down work laptop that can only make outbound requests.

```bash
ferry drop brief.md                   # -> brief.md.ferrydrop + a fresh passphrase
ferry drop --msg "the API is at :8090"
cat notes.txt | ferry drop -

# on the other machine, once the blob has arrived by any means at all
ferry pickup brief.md.ferrydrop
```

**Ferry supplies confidentiality, not delivery.** `drop` writes a blob; you move
it however you like (email, chat, a gist, object storage, a USB stick); `pickup`
reads it. That's a deliberate limit — it keeps ferry free of any account,
credential file, or third-party service, which is the same reason the rest of the
tool is LAN-only.

The blob is AES-256-CBC with PBKDF2 at 600k iterations, plus an **HMAC-SHA256
over the header and the ciphertext**. `pickup` verifies that MAC *before* it
invokes openssl, so a modified blob fails closed without ever entering the
decrypt path. The MAC covers the header because the header names the output file
— authenticating only the ciphertext would leave `name: ../../../.ssh/authorized_keys`
as a write-anywhere primitive. Independently of the crypto, `pickup` reduces that
name to a basename and refuses to write through a symlink.

The header is plain ASCII, so a stray blob is identifiable:

```
FERRYDROP/1
cipher: aes-256-cbc
kdf: pbkdf2
iter: 600000
kind: file
name: brief.md
mac: 685bf67e…
--
U2FsdGVkX1…
```

**The passphrase is the entire security boundary**, so send it by a *different*
channel than the blob. It's generated fresh per drop from openssl's CSPRNG and
printed once; it's never written into the blob, a log, or `client_logs.txt`, and
never passed in argv (`-pass pass:` would expose it to `ps`). Exit codes
distinguish the cases: `3` means the passphrase was wrong *or* the blob was
modified — those are indistinguishable to the verifier and both mean stop.

`drop`/`pickup` need `openssl` on `PATH`. Stock macOS LibreSSL 3.3.6 and OpenSSL 3.x
produce mutually decryptable blobs (verified, including that LibreSSL honours
`-iter` rather than silently ignoring it). Without openssl both commands exit `5`
with an explanation rather than a stack trace.

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
| **8098** | Reverse-relay control port — a client dials this to register, then publishes one of its own local ports through the host | `ferry relay` |
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

## Reverse expose: publish a client's port through the host

Every other feature in `ferry` pushes **host → client**: inference, files, the forward proxy. This is the missing direction. A locked-down laptop — a managed firewall that resets inbound connections, a corporate proxy that blocks tunnel services and MITMs TLS — can only make **outbound** connections. Nothing on it is reachable from a phone or another machine, not even over the same Wi-Fi. Reverse expose fixes that by having the client dial the host and letting the host do the listening.

```bash
# host
ferry relay                                  # accept registrations, publish ports

# client
ferry expose 4290 --as 4290 --token <token>  # serve 127.0.0.1:4290 from the host
```

`ferry relay --token` prints the shared token without starting anything; the client saves it to `~/.config/ferry/relay-token` after its first successful run, so only the first `expose` needs `--token` spelled out.

**How the bytes move.** Two kinds of connection, both **dialled by the client**, so nothing ever connects into it:

- **control** — one long-lived connection carrying `{"op":"register"}`, then one `{"op":"open","id":N}` from the relay for every inbound visitor.
- **data** — one new connection per visitor, opened by the client the moment it sees an `open`.

The relay accepts a public connection, parks the raw socket keyed by id, and sends `open` down control. The client dials back with `{"op":"data","id":N}` on a fresh connection; the relay matches it to the parked socket and splices the two — from then on it just pumps bytes in both directions.

**Lifetime and teardown.** The exposure lives exactly as long as the client does. `ferry expose` `exec`s its tunnel in place rather than running it as a child, so killing the process kills the tunnel outright — nothing lingers behind a dead supervisor. On the relay side, a read that returns empty on the control connection *is* the disconnect signal: the listener and every socket still parked behind it close immediately. And because a laptop can vanish without ever closing anything — lid shut, Wi-Fi gone — the relay sets TCP keepalive on the control connection, so an absent client is eventually reaped instead of leaving a port published for nobody.

**Security.** The token authenticates the client that *registers* — it says nothing about whoever connects to the published port afterward. Expose something with its own auth. Ferry's own ports (the endpoint, dashboard, share server, and friends) are refused as publish targets outright. Published ports bind `0.0.0.0` — the LAN — by default; pass `--bind 127.0.0.1` to `ferry relay` to keep an exposure local to the host only.

```bash
ferry status    # lists every published port, its client, and when it started
ferry down      # tears down the relay and everything published through it
```

## Remote access (Tailscale)

The endpoint is a LAN appliance; ferry publishes nothing to the internet. When you want it from *outside* the LAN, front it with [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve) — one command on the host puts a real TLS certificate and your tailnet's identity in front of the same local port, so the master key gets a TLS wire to travel over and only devices on your tailnet (plus anyone you explicitly share the node with) can knock:

```bash
# host: serve the endpoint over the tailnet
tailscale serve --bg --https=443 http://127.0.0.1:8090

# client (on the tailnet): point the profile at the ts.net name, then
# regenerate the configs so the wrappers carry the real key
#   ~/.config/ferry/client.json  →  "host": "your-mac.<tailnet>.ts.net"
ferry opencode --key <master-key>     # or: ferry claude --key <master-key>
```

Clients now talk to `https://your-mac.<tailnet>.ts.net/v1`. **What this does not cover:** the share server (`8095` — bootstrap, `pull`/`get`, `/hq` telemetry), the relay (`8098`), and the download proxies stay LAN-only — a remote client can drive inference but cannot bootstrap, ferry files, or send telemetry. And this is a documented recipe, not an integration: ferry does not install, start, or manage Tailscale for you.

## Local models

The two GPU lanes and how to swap their models are covered in [The local GPU lanes](#the-local-gpu-lanes). This section is the operational detail.

**KV-cache memory governor:** local launches ship with `--kv-bits 4`, `--max-kv-size 131072`, `--max-num-seqs 4`, and `APC_NUM_BLOCKS=512`. Measured on a 128GB M5 Max during a 121k-token agentic session: peak GPU footprint dropped 97GB -> 56GB, idle retained memory fell 57GB -> 35GB, and decode ran ~60% faster. Monitor live usage with `footprint <pid>` (`ps` RSS does not show Metal wired memory) — `ferry status` prints it per lane. Disable any knob by setting it to `""` in `lib/ferry-core.zsh`, or govern one lane only with the per-lane overrides (`LOCAL_ORCH_MAX_KV`, `LOCAL_SUB_MAX_SEQS`, …).

Both lanes run at these settings, so the stack keeps ~33GB of weights resident and two simultaneously-busy deep-context lanes can approach the ~90-100GB wired ceiling. If that bites, shrink the subagent lane first — `LOCAL_SUB_MAX_KV=65536` — since fan-out work rarely needs 128k of context.

**Known issue on the `local-orch` (Qwen) lane (measured 2026-08-25):** deep-context **streaming** requests can die mid-prefill. The mlx-vlm server raises `RuntimeError: There is no Stream(gpu, 1) in current thread` (observed ~40s into a ~44k-token prefill), litellm surfaces it as `MidStreamFallbackError` / `APIConnectionError: An error occurred during streaming`, and the client sees a dropped stream. The server **self-recovers** — subsequent requests succeed, and non-streaming requests were unaffected — so just retry the turn. No cloud fallback is wired for this by design (a dead GPU lane must error, not silently bill a cloud lane).

**MTP draft + quantized KV crashes the qwen3_5 verify path (diagnosed 2026-08-25):** with `--kv-bits` *and* an MTP draft model, the draft-verify branch of mlx-vlm's qwen3_5 attention crashed on **every cache-hit request** (turn 2+ of any conversation): any quantized cache (`BatchQuantizedKVCache`, regardless of 4/8 bits) returns keys as a tuple of `(packed, scales, biases)` arrays, and `prefix_len = keys.shape[-2]` raised `AttributeError` → 500 `APIConnectionError`. Deterministic repro: the same request twice → 200 then 500. **The shipped `local-orch` config is MTP draft + UNquantized KV capped at 64k** (16GB KV budget): stable (3/3 cache-hit requests 200) and ~53% faster decode (37.8 vs 24.8 tok/s). `local-sub` (no drafter, 4-bit KV) is unaffected.

**Compacting big sessions on `opencode-local` produces empty summaries (measured 2026-08-25):** a `/compact` sends the whole transcript (~43k-72k tokens) to the driving lane; on `local-orch` (Qwen 3.8 nvfp4) both observed compacts completed a full prefill and then generated **3 tokens** and stopped — an effectively empty summary. Compacting large sessions via `opencode-cloud` (or at least the first compact of a huge session) is the workaround.

**Known issues on the `local-sub` (Nemotron) lane (measured 2026-08-25):**

- **`nemotron_h` continuous-batching crash (mlx-vlm) — patched automatically.** The batching engine passes both `input_ids` and `inputs_embeds`; the `nemotron_h` `LanguageModel.__call__` forwards both to a backbone that requires exactly one → `ValueError: Provide exactly one of inputs or inputs_embeds` on **every** request. `ferry install` and `host-bootstrap.sh` now apply the two-line fix to `.../site-packages/mlx_vlm/models/nemotron_h/language.py` after installing mlx-vlm. The patch is idempotent and no-ops once upstream fixes the call site — but note that **any manual `uv tool install mlx-vlm --force` wipes it**, so re-run `ferry install` after upgrading mlx-vlm yourself.
- **Flaky `task`-tool calls.** Nemotron frequently emits malformed task calls (hallucinated `task_id`, missing `description`) that opencode rejects *before the tool runs* — the model then silently retries the identical broken call (measured: 444 consecutive errors; also 22 identical 38-token retries). Fix shipped: `client-bootstrap.sh` installs a `/fan-out` command and a `spawning-subagents` skill into `~/.config/opencode/` (in the default scope; a `--profiles-only` / `--no-opencode` client opts in with `--with-guardrails`). The recipe must sit in the **user message** (`/fan-out` does this); placing it in system instructions made failures worse. With it: 3/3 valid parallel task calls, zero schema errors. This matters less now that Nemotron is the *subagent* lane rather than the driver — but it still applies to whatever small local model is driving.
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

Everything runs on your own hardware and network. The front door answers only requests carrying the **master key** — one shared secret you set in `LITELLM_MASTER_KEY` and every client holds a copy of (new in v1.22; a keyless request gets a 401). The LAN transport is still **plain HTTP**, so that key travels in a header anyone sharing the wire can read: it is an auth layer, not encryption — enough to keep a neighbor's laptop or a misaddressed `curl` out, not enough for a hostile network. The hostile-network answer is [Tailscale Serve](#remote-access-tailscale), which adds TLS and tailnet identity without ferry integrating a tunnel. The MLX servers bind `127.0.0.1`, so the GPU lanes are reachable only through the front door, never directly. Cloud calls go host→provider over HTTPS using the host's keys, so **client devices never see the provider keys** — the master key is the one credential a client holds. The one transport built for an untrusted channel is `ferry drop` / `ferry pickup`, which encrypts before the data leaves the machine — everything else assumes the LAN. Inbound client telemetry (`ferry msg` / `ferry log`) is appended to `~/.config/ferry/client_logs.txt` on the host — outside any checkout, so it survives a repo that moves or a worktree that is removed. The observability stack binds to `127.0.0.1` only. A port published with `ferry relay` binds on the **host** (the LAN by default) and is reachable by anything that can reach the host on that port — the relay authenticates only the client that registers it, so whatever you `ferry expose` must carry its own authentication.

## Command reference

| Command | Mode | What it does |
|---|---|---|
| `install` | host | Install `uv`, `litellm` (+ `mlx-vlm` & default models on macOS), link `ferry` globally |
| `up [--local-orch\|--local-sub\|-c\|-m <id>\|-r\|-i] [-p <port>]` | host | **No args → the full stack**: `heavy` + `flash` (cloud) and `local-orch` + `local-sub` (GPU) on `8090`. `-r`/`--route` → cloud lanes only; `--local-orch`/`--local-sub` → one GPU lane alone; `-c`/`-m` → a single cloud model; `-i` → interactive catalog |
| `down` | host | Stop all servers, cloud proxies, and share/proxy servers |
| `status` | both | Host: per-lane listeners, memory, and served lane names. Client: connection health + the host's lanes |
| `update [--full] [--host\|--client] [--dry-run]` | both | Catch this machine up. Detects the role from `~/.config/ferry/client.json` and runs that side's reset: a **host** rebuilds the CLI from its own checkout, re-links it, and bounces the proxy; a **client** re-pulls the CLI from its host. `--full` also reloads the GPU lanes (host only) |
| `dash [--open] [--port P] [--ferry URL]` | host | Live route-proxy dashboard on `8091` (`--grafana` → full Grafana/VictoriaMetrics stack; also standalone `ferry-dash`) |
| — | — | The dashboard's **Routes** panel edits each lane's failover chain in place: reorder, add or remove hops, preview the exact YAML diff, then apply. A timestamped snapshot is written first, only the `fallbacks:` line is rewritten (every comment in your config is left as-is), and the proxy picks the change up on the next `ferry update` |
| `share` | host | Serve the client bootstrap + ferry transfer routes over the LAN (`8095`). Clients pass the endpoint key through the one-liner as `FERRY_MASTER_KEY=…` so the new client's profile carries it |
| `msg <text>` | client | Send a text note to the host's `~/.config/ferry/client_logs.txt` |
| `log` | client | Pipe stdin straight to the host's `~/.config/ferry/client_logs.txt` |
| `inbox [-n N] [-f] [--all] [--path]` | host | Read that file back, dated and attributed from the share server's access log where the receipt still exists |
| `relay [--port P] [--bind ADDR] [--foreground] [--token]` | host | Accept reverse-expose registrations so a client can publish one of its own local ports through this host (control port `8098`) |
| `expose <port> [--as PUBLIC] [--host H] [--port P] [--token T]` | client | Publish this client's `127.0.0.1:<port>` from the host, dialling only outbound |
| `offer <path>...` | host | Record files/dirs in `offered.json` for clients to fetch |
| `pull <model-id> [--host H] [--port P] [--transport http\|hf\|nc] [--to DIR]` | client | Pull a model from the host cache (three transports) |
| `get <name> [--host H] [--port P] [--to DIR]` | client | Fetch an offered file/dir by basename |
| `receive [--port P] [--to DIR]` | client | Listen for a netcat tar stream (default port `9099`) |
| `send <path> <client-host> [--port P]` | host | Push a file/dir to a listening client via netcat (default `9099`) |
| `serve-hf [--port P]` | host | Start the experimental HuggingFace pass-through proxy (default `8096`) |
| `serve-proxy [--port P]` | host | Start the general HTTP(S) download forward proxy (default `8097`) |
| `env [--host H] [--proxy-port P] [--hf-port P2] [--write]` | client | Emit shell exports so this laptop routes downloads via the host proxy |
| `opencode [--host H] [--port P] [--config PATH] [--local\|--cloud] [--key KEY] [--model M] [--small-model SM] [--housekeeper HK] [--super] [--keep N] [--no-default]` | dual | Take the opencode config over: `permission`, `model`, `small_model`, and all seven built-in agents, pinned to lane names. `--key` writes the master key into the configs (v1.22) — without it they carry the keyless `local` placeholder, which a hardened front door rejects. `--super` pins worker AND housekeeper to `super-flash` (heavy keeps driving). Snapshots the original first |
| `claude [--host H] [--port P] [--key KEY] [--wrappers]` | dual | Point Claude Code at the ferry endpoint by lane name: installs the `claude-ferry` / `claude-ferry-local` / `claude-ferry-super` wrappers into `~/.zshrc` and writes `~/.config/ferry/claude.json` recording the lane map. `--key` bakes the master key into the wrappers (v1.22); `--wrappers` installs the `~/.zshrc` block only (the host-reset shim) |

Run `ferry --help` for the built-in usage banner.

## Development

`ferry` is assembled from per-domain modules so the CLI isn't one file to reason about. Source lives in [`lib/`](lib/) as **15 modules**: `ferry-core` (bootstrap, LAN/mDNS discovery, config, secrets), `ferry-usage`, `ferry-install`, `ferry-serve` (up/down/status/catalog), `ferry-share` (LAN share server + telemetry), `ferry-inbox` (read the telemetry back), `ferry-relay` (reverse expose), `ferry-transfer` (pull/get/send/receive/offer), `ferry-drop` (encrypted off-LAN transfer), `ferry-proxy` (serve-hf/serve-proxy), `ferry-integrate` (env/opencode), `ferry-claude` (Claude Code wiring), `ferry-dash`, `ferry-update`, and `ferry-main` (dispatch). The shipped `ferry` is a **generated** single file — clients fetch it as one script over the LAN — so edit the modules and regenerate:

```bash
./build.zsh            # regenerate ./ferry from lib/ferry-*.zsh
./build.zsh --check    # CI / pre-commit: fails if ferry has drifted from lib/
```

Commit both `lib/` and the regenerated `ferry`; don't hand-edit `ferry` (the sync guard will flag it).

### Tests

Stdlib `unittest`, no dependencies, each suite runnable on its own:

```bash
python3 lib/ferry-serve.test.py            # lane ports, launch flags, KV governor
python3 lib/ferry-front.test.py            # the front door: /v1/models advertises lanes only
python3 lib/ferry-integrate.test.py        # the opencode takeover: scope, lane split, snapshots
python3 lib/ferry-claude.test.py           # the Claude Code wiring: wrappers, lane map, snapshot
python3 lib/ferry-hostwrappers.test.py     # host-side opencode wrappers: marker strip, baked host/port
python3 lib/ferry-share.test.py            # share server + client-script placeholder injection
python3 lib/ferry-hostreset.test.py        # host-reset: route-config validation, endpoint verify
python3 lib/ferry-clientbootstrap.test.py  # client scope: bootstrap / reset / cleanup
python3 lib/ferry-update.test.py           # `ferry update`: role detection, client/host dispatch
python3 lib/ferry-inbox.test.py            # inbox: the receipt/entry join, host-only guard
python3 lib/ferry-relay.test.py            # reverse tunnel: byte round-trip, teardown on disconnect, refusals
python3 lib/ferry-drop.test.py             # drop/pickup: encrypt, authenticate, decrypt, refuse tampering
python3 lib/ferry-dashroutes.test.py       # the dash route editor: the fallbacks writer + snapshots
python3 lib/ferry-events.test.py           # ferry_events.py: the per-request event record and writer
python3 lib/ferry-live.test.py             # the live view: topology parse + the event tail
```

The share and host-reset suites deliberately run the **real** embedded Python — extracted out of the built `ferry` and out of `host-reset.sh` — rather than a reimplementation, so an edit that breaks the shipped behaviour fails in the suite instead of on a laptop.

The client-scope suite goes further: it runs `client-bootstrap.sh`, `client-reset.sh` and `client-cleanup.sh` end-to-end against a throwaway `$HOME` and a stub host that serves `/v1/models` and the repo's own `ferry`. The property it defends is an *absence* — that the narrow scopes never create `~/.config/opencode`, and that cleanup leaves everything that isn't ferry's standing — and an absence is only proved by looking.

## License

MIT — see [LICENSE](LICENSE). © 2026 Stephen Blatt.
