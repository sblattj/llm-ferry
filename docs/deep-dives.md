# llm-ferry — deep dives

Forensic internals moved out of the [README](../README.md) so it stays scannable. Content is preserved verbatim from the README as of v1.37.0; section order follows the README narrative.

## Contents

- [Claude Code wiring & the ChatGPT bridge](#claude-code-wiring--the-chatgpt-bridge)
- [opencode takeover scopes](#opencode-takeover-scopes)
- [Catching a client up later](#catching-a-client-up-later)
- [Removing it from a client](#removing-it-from-a-client)
- [Reading what the clients sent](#reading-what-the-clients-sent)
- [Catching the host up](#catching-the-host-up)
- [Promoting a client into a host](#promoting-a-client-into-a-host)
- [Route config forensics](#route-config-forensics)
- [The opencode goal-plugin install internals](#the-opencode-goal-plugin-install-internals)
- [Fleets — configuration detail](#fleets--configuration-detail)
- [Event tap, schema repair & attribution](#event-tap-schema-repair--attribution)
- [Encrypted drop — crypto detail](#encrypted-drop--crypto-detail)
- [Reverse expose — how the bytes move](#reverse-expose--how-the-bytes-move)
- [Local model known issues](#local-model-known-issues)
- [Ports](#ports)
- [Test suite detail](#test-suite-detail)

## Claude Code wiring & the ChatGPT-bridge

**Claude Code works too, as of v1.20.** The ferry endpoint speaks the Anthropic
`/v1/messages` protocol, so Claude Code can run on the ferry backend with no
changes to `claude` itself. When `claude` is installed, the bootstrap also
installs three wrappers into `~/.zshrc` (skip with `--no-claude`):

- `claude-ferry` — the **cloud lanes**: `heavy` drives, `flash` covers background
  tasks and subagents. Select `medium` explicitly for substantive coding or reviews.
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

For ChatGPT subscription deployments, the host preserves Claude Code's system
prompt blocks as Responses `developer` messages. The ChatGPT endpoint rejects
`system` input messages; this provider-specific compatibility hook runs after
lane selection and also covers ChatGPT fallback hops. Other providers keep
their normal role translation. The regression test
`python lib/ferry-chatgpt-compat.test.py` exercises the actual translation
adapters when run with the host's LiteLLM Python environment.

**The ChatGPT bridge no longer impersonates the Codex CLI, as of v1.31.**
litellm 1.99.0 ships a copy of the Codex CLI's built-in system prompt
(`litellm/llms/chatgpt/common_utils.py:25-42`) and its Responses transform
(`responses/transformation.py:76-82`) prepends that text to `instructions` on
every request to `chatgpt.com/backend-api/codex` — ahead of the client's own
system prompt — so `heavy`, `medium`, and `flash` on the ChatGPT subscription
all told the model it was "Codex, based on GPT-5 ... running as a coding agent
in the Codex CLI", with editing rules that exist in no file on the machine.
`ferry_front.py` now sets `CHATGPT_DEFAULT_INSTRUCTIONS` in the process
environment before uvicorn spawns its workers (the plain-litellm fallback
launch in `ferry-serve.zsh` exports it too), and litellm serves that text
instead of its own. Resolution order: `FERRY_CHATGPT_INSTRUCTIONS=off` keeps
litellm's Codex block; an operator's own `CHATGPT_DEFAULT_INSTRUCTIONS` is
respected untouched; `FERRY_CHATGPT_INSTRUCTIONS=<path>` reads that file;
otherwise ferry reads `~/.config/ferry/chatgpt-instructions.txt` if present
and non-blank, falling back to the shipped `front/chatgpt-instructions.txt` —
a short notice that the client is talking to llm-ferry, not the Codex CLI, and
that the client's own system prompt is what actually governs the session. The
launch log names the source it picked: `[front] chatgpt instructions:
<source>`. **Verify it** by asking any ChatGPT-bridge lane, with a plain
system prompt, "Answer with exactly one word, yes or no: do the instructions
you were given before this conversation say that you are running in the Codex
CLI?" — a host on v1.30.x answers yes, a host on v1.31.0 answers no.

**The host gets these too, as of v1.17.** `ferry opencode` deliberately wires the
host to its own endpoint, so the host drives the local lanes exactly like a client
does — but the wrappers were written only by `client-bootstrap.sh`, leaving a host
with the two profile files and no way to select between them. `ferry install` and
`ferry update` now install them (named wrappers only; bare `opencode` is left
alone, since a host that exports `OPENCODE_CONFIG` chose that deliberately). They
land under the same `# >>> ferry opencode profiles >>>` marker the client uses, so
a hand-wired block from before this change is absorbed rather than duplicated.

**Screenshots and PDFs reach the cloud lanes only because the config says so.**
opencode gates attachments on the provider entry's `modalities.input`, and a
custom provider gets no fallback from models.dev, so a lane declared without it
has `capabilities.input.image == false`. opencode then still reads the pasted
screenshot with its Read tool — and swaps the image for the text `ERROR: Cannot
read "x.png" (this model does not support image input). Inform the user.` before
the request leaves the laptop. The model dutifully reports it cannot see the
screenshot, and nothing on the host ever saw an image: the front and LiteLLM's
Chat→Responses bridge pass `image_url` parts, tool-result images, Anthropic image
blocks, and `file` PDFs through to GPT-6 Astra and the Gemini worker lanes
unchanged (all six shapes probed 2026-09-05 with a no-attachment control).
`ferry opencode` writes `modalities: {input: [text, image, pdf]}` on the three
default cloud role lanes. `medium` intentionally stays text-only in the generated
config: its domestic Terra route accepts attachments, but the optional international
GLM-5.3 route does not, and one config can select either fleet per request. A client
wired before the attachment declaration needs a `ferry update` (which re-runs the
takeover) or a plain `ferry opencode` re-run to pick it up. The GPU pair stays
text-only on purpose: the mlx servers behind it reject image input.

## opencode takeover scopes

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

The `using-the-goal-plugin` skill isn't gated on the *guardrails* switch — it follows the plugin, so a `--no-guardrails` client still gets it, since a plugin without its doctrine is the case the file exists to close. It is still gated on the **scope**, for the reason the guardrails are: opencode discovers skills only under its own global config dir (`ConfigPaths.directories()` is `Global.Path.config` plus the project/home `.opencode` dirs — the directory holding an `$OPENCODE_CONFIG` profile is never scanned), and `~/.config/opencode` is exactly what `--profiles-only` exists to leave alone. So the default scope installs it and both narrow scopes skip it, each saying so.

The chosen scope is recorded in `client.json` as `opencode_mode`, which is what keeps the catch-up below from silently re-widening the machine.

## Catching a client up later

When the host changes — new lanes, a re-pointed alias, a newer `ferry` — an already-bootstrapped client catches up with:

```bash
curl -fsSL http://your-mac.local:8095/client-reset.sh | zsh
```

It re-pulls the CLI and re-applies the opencode takeover. It does **not** touch `~/.zshrc`, rewrite `client.json`, or prompt for anything — it reuses the profile the bootstrap left behind. Re-run the bootstrap instead if the machine is new or the shell wrappers need refreshing.

**It re-applies the scope, not the maximum.** `opencode_mode` from `client.json` decides which configs get written: `full` (or a profile from before the key existed) does all three, `profiles` does only ferry's own two, `none` re-pulls the CLI and writes no config at all. The same three flags override it for one run — `client-reset.sh --profiles-only` — without rewriting the profile, so an override can't quietly redefine the machine.

**Why the CLI is re-pulled first, always:** `ferry opencode` is what performs the takeover, so an out-of-date CLI quietly does the *old* thing and reports success either way. The download is validated (shebang, `cmd_opencode` present, `zsh -n` clean) before it replaces the working binary, so a share server that is down cannot leave you with an HTML error page named `ferry`.

## Removing it from a client

```bash
curl -fsSL http://your-mac.local:8095/client-cleanup.sh | zsh -s -- --dry-run   # print, change nothing
curl -fsSL http://your-mac.local:8095/client-cleanup.sh | zsh                   # apply
```

The inverse of the bootstrap, and scope-agnostic: it removes whatever is actually there, so it undoes a default install, a `--profiles-only` one, and a `--no-opencode` one without being told which. Out go the `ferry` CLI, `~/.config/ferry` (profile, lane profiles, snapshots, telemetry), the `~/.zshrc` wrapper block and `host-code` alias, and the guardrail files and the `using-the-goal-plugin` skill — under both the `skill/` and `skills/` spellings, since the two installers disagree.

It edits `~/.config/opencode/opencode.json` **surgically**: the provider ferry wrote (the `ferry` provider entry), the goal-plugin entry it appended to `plugin`, and the `/goal` command it merged into `command` are removed, the file is snapshotted to `.<UTC>.jsonc` first, and a config with nothing ferry-shaped in it is left byte-identical. Your own providers, MCP servers and commands survive.

The TUI half is taken out of `~/.config/opencode/tui.json` too — since v1.30.3 that entry is a `file://` URL to the ferry-managed copy under `~/.local/share/ferry/opencode-goal-plugin`, and the copy itself is deleted along with it; the file goes outright when the entry was all it held. Two things are ferry's to remove and two are not: a **local filesystem path** to your own fork of the plugin is never touched (ferry's copy carries a `.ferry-goal-plugin` marker; a path without one is yours), and a `/goal` command you edited stays exactly as you wrote it — only the verbatim one ferry wrote goes.

Two things it deliberately keeps: the `opencode` binary (not ferry's to uninstall) and `~/.local/share/opencode`, your session history — `--full --yes` is the only way to delete that, and `--full` without `--yes` is refused outright so a piped fat-finger can't wipe it.

## Reading what the clients sent

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

## Catching the host up

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

## Promoting a client into a host

A laptop that has been a **client** of someone else's ferry can become a **host** of its own with one command. `ferry migrate` is the reverse of `client-bootstrap.sh`:

```bash
ferry migrate --dry-run   # print every step, change nothing (run this first)
ferry migrate             # do it (asks once before it starts)
ferry migrate --full      # ...and reload the GPU lanes at the end
```

A client's `ferry` is a lone script in `~/.local/bin` with no repo behind it, so `ferry migrate` first makes sure a checkout exists — the one this CLI already lives in, or one it clones to `--dir` (default `~/gdev/llm-ferry`, else `~/llm-ferry`) — then runs the engine, [`client-to-host.sh`](../client-to-host.sh), from it. Run that script directly if you already have the repo. In order it: carries the client's front-door master key forward into `~/.config/ferry/secrets.env` (a key already there wins; the value is never printed), seeds `~/.config/ferry/litellm.yaml` from the template if absent, runs `ferry install` from the checkout (OS-aware: uv + litellm everywhere, plus mlx-vlm and the ~16.6GB default models on macOS only; installs the host's shell wrappers at `127.0.0.1` and symlinks `ferry` into the checkout), then **archives `~/.config/ferry/client.json` to `client.json.pre-migrate.<UTC>`** — that one move is what leaves `CLIENT_MODE` (ferry decides host-vs-client purely on that file) — and runs `host-reset.sh` to validate the route config, bounce the proxy and share server, and re-wire opencode/claude at `127.0.0.1`. Because `host-reset` reads that bearer from the `client.json` this step just archived, a keyed front door would leave the host's own tools sending the `local` placeholder, so the migration re-applies the opencode/claude wiring with the master key as a final pass.

It is reversible: `mv ~/.config/ferry/client.json.pre-migrate.<UTC> ~/.config/ferry/client.json` puts the machine back to being a client. The archive happens **after** the dependency install and **before** the reset, so a failed install leaves a recoverable client and `host-reset.sh` never sees the `client.json` it refuses to run beside. If a run stops between the archive and a healthy endpoint, re-running `ferry migrate` reports the box already left client mode and points at `host-reset.sh` to finish, rather than migrating again.

## Route config forensics

**Worker pool (load-balanced).** The template ships `domestic.flash` as one primary deployment (GPT-5.6 Luna through OpenRouter, with throughput-based provider routing); any deployments you add **sharing the `domestic.flash` model_name** form a pool: `usage-based-routing-v2` sends each call to the least-used one (proactive even split), and on a `429` it cools the dead deployment out (`cooldown_time`) and rolls traffic to another. If you pool Gemini on a **native** key instead, **widen it with model ids, never with keys.** Google says it plainly — *"Rate limits are applied per project, not per API key"* — so a second key in the same project shares one bucket and buys nothing, and a second *project* to multiply the limit is circumvention under Google APIs ToS §2.d (nine burst-created projects suspended in one night, 2026-08-25, and the account's OAuth APIs restricted). But the limit is per-project-**per-model**: every model id carries its own RPM/TPM/RPD bucket, so pooling `gemini-3.8-flash` with, say, `gemini-3.5-flash` on **one** key is two independent buckets and nothing to circumvent. Pick members that are interchangeable for the lane's *role*, so a caller cannot tell which one answered. The other sanctioned lever is raising the paid tier on that one project (Tier 3 = 20M TPM).

**Only lanes are advertised.** `/v1/models` lists the lanes you mark `model_info: {public: true}` and nothing else. `router_settings.fallbacks` is keyed by model group: lanes with error-only hops must keep those hops out of the catalogue, while `super-flash` deliberately has an explicit empty entry (`[]`) and no hidden hop. A client that picks a fallback hop out of a model list gets a single provider with **no failover at all**, which defeats the chain. litellm has no setting for this (`hidden` applies to `model_group_alias` entries only), so `ferry up` serves litellm's own app through a small ASGI filter (`front/ferry_front.py`) that trims the listing. It is not a second process and not a reverse proxy — every request that is not the model listing goes to litellm untouched, so nothing sits between a client and a streamed token. Hiding is not removing: an unadvertised hop is still callable by name if you ask for it. If the filter cannot start, ferry says so and serves litellm directly rather than leaving the endpoint down.

**The driver lane has one hop, a deliberate override of the older no-chain policy (2026-09-05).** `heavy` (and the legacy `orch`/`orchestrator` names, resolved to `heavy` by the front door since fleets, 2026-09-04 — see [Fleets](../README.md#fleets)) runs on the ChatGPT subscription via litellm's native `chatgpt/` provider (`chatgpt/responses/gpt-6-astra`, device-code login at `~/.config/litellm/chatgpt/auth.json` — not an API key) at `reasoning_effort: xhigh`, the top effort value litellm's chat→responses bridge actually forwards (it silently drops `max`). Its single `router_settings.fallbacks` hop is `heavy-sol` (`chatgpt/responses/gpt-5.6-sol`, the previous driver model, same bridge, same `xhigh`): a spill changes the model but not the posture. Because the hop draws on the same subscription bucket, it covers a model-specific outage or a bad rollout of Astra, not an account-level quota `429`. The older rule (a driver that fails should error rather than silently continue on a model the user never chose) still governs everything beyond that one hop. The ChatGPT backend is also **streaming-only** on litellm 1.99.0 — a non-streamed call `500`s — which a streaming client never notices but rules out serving `heavy` to a non-streaming caller at all.

**Every cloud lane has a fallback entry.** `flash` runs GPT-5.6 Luna at `xhigh` (since 2026-09-05; `chatgpt/responses/gpt-5.6-luna` on this host, `openrouter/openai/gpt-5.6-luna` in the example) and falls back to `flash-gemini` (`openrouter/~google/gemini-flash-latest`, currently resolving to Gemini 3.8 Flash, routed to the fastest-throughput OpenRouter provider, `reasoning.effort: xhigh`), then `flash-terra` (GPT-5.6 Terra, `xhigh`). `super-flash` is always `openrouter/~google/gemini-flash-latest`, with `reasoning.effort: minimal` and `provider.sort: throughput`, in domestic and international fleets; its entry is deliberately `super-flash: []`, so it has no non-Gemini fallback. Non-empty fallbacks fire only when their primary errors — a `429`, a `5xx`, or a hard quota `403` — and are never public.

**OpenRouter hops route to the fastest provider.** One OpenRouter model id is served by many providers — GLM 5.3 Flash by 22 on 2026-09-02, from 111 tok/s at the top to 17 at the bottom — and OpenRouter's default picks among the *cheapest* of them, weighted by inverse-square price. **The OpenRouter deployments in the template**, including the `flash` primary and its Gemini/Terra hops, therefore carry `extra_body: {provider: {sort: throughput}}`, which is [OpenRouter's own provider-routing object](https://openrouter.ai/docs/features/provider-routing) forwarded verbatim by litellm: every request is re-ranked by each provider's p50 tokens/s over a rolling 5-minute window, on OpenRouter's side. Nothing in ferry polls or pins a provider name, so a provider that is rate-limited *this minute* is simply not at the top this minute — pinning `order: ["Baseten"]` (the fastest on the page) returned `429 temporarily rate-limited upstream` while `sort: throughput` on the same model was served by Friendli and Fireworks at once (verified 2026-09-02 through `ferry_front.py`, with an unsorted control lane landing on Z.AI). The trade is price: throughput sort ignores it, so a model with a discounted provider may be served at full rate instead. Drop the block from any deployment you would rather run cheap than fast.

**The local lanes are deliberately outside every fallback chain.** The whole point of naming `local-orch` or `local-sub` is that the request stays on your machine — so a stopped GPU lane surfaces as an error rather than quietly spending a cloud quota. Cloud lanes can use only their configured cloud hops; `super-flash` deliberately has none.

**⚠ An alias has no fallback chain.** `router_settings.model_group_alias` looks like the way to keep an old client-facing name working, and it does resolve — for *deployment selection* only. litellm reads the fallbacks map with the **raw model string the client sent, before any alias is resolved**:

```
router.py:6411   model_group = kwargs.get("model")      # "orchestrator"
router.py:6345   get_fallback_model_group(fallbacks, model_group)
router.py:6357   fallback_model_group is None -> raise original_exception
```

Alias → target resolution lives at `router.py:9278`, on the deployment-selection path that lookup never reaches. So an aliased lane matches no `fallbacks:` entry, its primary's error goes straight to the client, and the entire chain is skipped — silently, because the config and `/v1/models` both look correct. Verified 2026-08-28 against a live stack whose primary was quota-blocked: the alias returned `500`; the real `model_name` returned `200` from the second hop.

**Duplicate the deployment instead.** To keep a legacy name alive, give it a real `model_name` of its own rather than an alias — one extra block. (`heavy`'s own legacy names, `orch`/`orchestrator`, are the one case that needs no block at all: the front door's `LEGACY_HEAVY` map resolves them to `heavy` since fleets, 2026-09-04 — see [Fleets](../README.md#fleets).) A worker lane like `flash` still needs its **own** `fallbacks:` entry — not an alias of it — to keep failing over to its hop; here's the pattern for a hypothetical `flash-v1` rename:

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
  fallbacks: [{"flash": ["flash-terra"]}, {"flash-v1": ["flash-terra"]}, {"super-flash": []}]
```

`or-gemini-flash-v1`, `or-gemini-flash`, and `or-terra-flash-fb` above are just `model_info.id` — metric labels litellm stamps onto each deployment's Grafana series, not something a client ever sends.

**Never let a real model id become the name clients type.** It is tempting to name a lane after the model currently behind it, and it goes wrong the first time you re-point that lane: clients keep sending a vendor's model name and get someone else's model back, and nothing in `/v1/models` reveals the discrepancy. Name the *role* instead — a role survives the model behind it changing, which is the entire reason clients address lanes. `ferry opencode` enforces the same rule from the client side: it writes only lane names, never a model id.

**Keeping a lane out of the catalogue is a separate control** — the one an alias only appeared to offer. Omit `model_info: {public: true}` and `front/ferry_front.py` leaves the lane out of `/v1/models` while it still routes *and still keeps its chain*. Use it sparingly: an unlisted lane is invisible to everything that reads the catalogue, including ferry's own `host-reset.sh` verifier, which will report it as not served while calls to it keep succeeding. `super-flash` — the compaction/title/summary lane `ferry opencode` points those agents at — is a real and *advertised* `model_name`, so clients can select the same inexpensive lane through `small_model`.

**Add models with Claude Code.** This repo bundles two skills — [`add-fallback-orchestrator`](../.claude/skills/add-fallback-orchestrator/SKILL.md) and [`add-worker-model`](../.claude/skills/add-worker-model/SKILL.md) — that walk Claude through editing your `litellm.yaml` correctly: the strict-failover-chain vs. load-balanced-pool distinction, the independent-capacity rule for fallbacks, and the per-project-quota gotcha **plus the Google ToS line a worker-key pool must not cross**. Just ask Claude Code to "add a fallback orchestrator" or "add another worker key."

## The opencode goal-plugin install internals

`plugin` is *appended* to, never replaced — the [OpenCode goal plugin](https://github.com/sblattj/OpenCode-goal-plugin) is added if it isn't already there, in exactly this form:

```
opencode-goal-plugin@https://github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.11.0.tar.gz
```

**The spelling is the whole feature.** On opencode 1.18.29 a bare `github:owner/repo#tag` spec (which is what ferry ≤ v1.29.4 wrote) *installs to disk and then never loads*: npm-package-arg returns no name for it, so `Npm.add` throws after a perfectly successful reify, the entry is dropped, and **nothing is logged anywhere** — the package cache fills up and the plugin has never once run. Prefixing the package name fixes that. On top of it, any *git* spec whose `package.json` declares a `build`/`prepack` script dies inside opencode's bundled installer (`git dep preparation failed`), so the pinned form is a **remote tarball**, which is fetched by a code path that never runs that step at all.

Everything around that entry follows from it:

- **Migration.** Every earlier spelling ferry (or you) could have written — bare npm name, `@prevalentware/…`, `github:…`, `github:…#ref`, a `git+https://` URL, a plain or name-prefixed tarball URL, the `["pkg", {opts}]` tuple form (options preserved) — is rewritten to the canonical spec on every run and de-duplicated to a single entry. A local filesystem path to your own fork still counts as present and is left alone.
- **`tui.json`.** The plugin ships two halves, and opencode reads TUI plugins **only** from `~/.config/opencode/tui.json` — never from `opencode.json`'s `plugin` array. The entry written there is **not** the tarball spec but a `file://` URL to a ferry-managed copy of the installed package at `${XDG_DATA_HOME:-~/.local/share}/ferry/opencode-goal-plugin`, refreshed from opencode's cache after the pre-install and marked with a `.ferry-goal-plugin` file (spec, tag, package name). The reason is a third opencode defect: its cache directory *is* the spec string, so the tarball form lives under `packages/opencode-goal-plugin@https:/github.com/…`, and Bun's runtime plugin runner splits any module path at the first `:` into `namespace:path` — the hook opencode uses to hand a TUI plugin its shared `solid-js` never sees the file, and the TUI half dies with `Cannot find package 'solid-js'`. The only trace is in the TUI's own console overlay (`ctrl+p` → "Toggle console"); nothing reaches stderr or `opencode.log`. Every `github:` and `name@https://…` spec has that colon; a plain path does not. The file is created with `"$schema": "https://opencode.ai/tui.json"` if absent, any previous one is snapshotted, every other key is left alone, and a `file://`/path entry without ferry's marker is treated as your own fork and left in place. **The two halves must move together:** since plugin v0.11.0 they share a `metadata.goal` payload version (`v: 2`), and a 0.10.x panel reading a v2 payload silently drops the turns stat. Ferry writes both from the same pinned copy in one run, so its own pair cannot skew — a fork left standing here while the server half advances can. Use `--tui-config PATH` to point it elsewhere or `--no-tui-config` to skip it. A `--config` outside `~/.config/opencode` (the ferry lane profiles) never gets one.
- **Cache hygiene.** The spec string *is* opencode's cache key (`~/.cache/opencode/packages/<spec>`), and opencode never invalidates it — no TTL, no version check. Ferry removes the per-spec directory of every entry it just migrated away from, plus the known-dead ones, and removes the canonical directory itself when it holds an *interrupted* install (an empty `node_modules/opencode-goal-plugin` is treated as installed forever). Only directories carrying the package name are ever touched; `--keep-cache` opts out.
- **Pre-install.** Writing a spec installs nothing, and a failure at opencode's next start is invisible. So after writing, ferry runs `opencode plugin '<spec>' --global` in a throwaway config sandbox (real package cache, 120 s cap), then verifies the cached `package.json` version and both `dist/goal-plugin.js` and `dist/goal-tui.js`, and refreshes the TUI copy under `~/.local/share/ferry` from it. Success prints the installed version and the copy's path; failure prints a warning with the real error and **still exits 0** — the config is correct either way, and an offline laptop shouldn't fail a bootstrap. `--no-install` skips it.
- **Skill.** Alongside the plugin, `ferry opencode` copies an OpenCode skill, `using-the-goal-plugin`, to `~/.config/opencode/skill/using-the-goal-plugin/SKILL.md` — the same run, the same singular `skill/` path the `spawning-subagents` guardrail already uses. It teaches the model what the plugin's injected turn mechanics never explain: how to size and decompose a plan, work it under the Claim → Evidence → Verdict rule, finish with the exact `[goal:evidence]`/`[goal:complete]` grammar, respect budgets and pauses, and treat a `/goal` control turn as read-only. The copy is refreshed from the repo's `opencode/skills/using-the-goal-plugin/SKILL.md` on every run that writes this entry — `--no-default` and a pre-existing local fork of the plugin leave the entry alone and skip the skill with it — so an edit there reaches an already-wired host on the next `ferry opencode`. The destination is ferry's, not yours: a hand-edited copy there is overwritten rather than merged, so customise the repo file (or fork it under a different skill name) instead. `ferry install` and `host-reset.sh` install it as well (the latter unconditionally, for the host running a local fork), and on a client, which has no checkout, a direct `ferry opencode` says so instead of going silent — the client's copy rides in `client-bootstrap.sh`'s default scope. The three scripts that drive the takeover run `ferry opencode` once per config target and pass `FERRY_GOAL_SKILL_QUIET=1`, which silences that one report line (never the copy) because they report the skill themselves, once per run.

Ferry also merges a top-level `command.goal` entry into the config (never overwriting one you wrote) so the plugin's `/goal` slash command actually appears.

Verify an install at any time:

```bash
find "${XDG_CACHE_HOME:-$HOME/.cache}/opencode/packages" \
  -path '*/node_modules/opencode-goal-plugin/package.json' -exec grep -m1 '"version"' {} +
```

`find`, not a glob: a spec's slashes become directories, so the tarball form lands eight levels down at `packages/opencode-goal-plugin@https:/github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.11.0.tar.gz/` — a single-level `packages/*opencode-goal-plugin*/…` pattern matches only the older `github:`-shaped directories and reports a healthy install as missing.

And the TUI half, which loads from ferry's copy rather than the cache:

```bash
cat ~/.local/share/ferry/opencode-goal-plugin/.ferry-goal-plugin      # spec, tag, package name
grep -m1 '"version"' ~/.local/share/ferry/opencode-goal-plugin/package.json
```

And the skill, discovered like any other OpenCode skill rather than read off disk:

```bash
opencode debug skill > /tmp/oc-skills.json   # a pipe truncates at 64 KiB
python3 -c 'import json;print([s["location"] for s in json.load(open("/tmp/oc-skills.json")) if s["name"]=="using-the-goal-plugin"])'
```

prints the installed path; before v1.32.0 it prints `[]`.

Then, in a terminal wider than 120 columns, `ctrl+p` → **Plugins** lists an *External* entry for `opencode-goal-plugin` at that path, and `/goal <objective>` puts a **Goal** panel in the sidebar under LSP.

## Fleets — configuration detail

The route template provisions the domestic fleet only. Its commented
`international.medium` guidance uses Z.ai `glm-5.3` with thinking enabled at
`high`, then an OpenRouter `z-ai/glm-5.3` fallback at `high`, using the native
`GLM_API_KEY` for the Z.ai route. If you configure a complete international fleet,
its `super-flash` lane must use `openrouter/~google/gemini-flash-latest` at minimal
reasoning with throughput routing and an explicit empty fallback list; this template
does not provision those international lanes.
GLM-5.3 is text-only, so the shared opencode `medium` entry does not advertise
image or PDF input across either fleet, even when domestic Terra could accept it.
The Z.ai coding subscription and the ChatGPT subscription have their own shared
limits; OpenRouter fallback usage is separately billed. These route descriptions
are configuration guidance, not a claim that one lane benchmarks above another.

The commands below use `international` as an example of a second fleet you have configured.

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
[Remote access](../README.md#remote-access-tailscale)) arrives at the front door from loopback with no
`X-Ferry-Client` header unless it has re-run bootstrap or reset since this release, so it
resolves as `host` — the HOST's own fleet, not its own sticky selection — until it
regenerates its config. This is documented behavior, not a bug to work around.

## Event tap, schema repair & attribution

The proxy access log records **no model at all** — measured over 13,182 real records — so nothing derived from it can say which deployment served a request. The tap is what supplies that: an ASGI middleware on the inference path reads LiteLLM response attribution headers, observes timing and usage, and appends one NDJSON line per request. It is **off by default**, forwards every message unmodified, and drops rather than ever blocking a response.

It reads a request body once — the same buffered read the fleet rewrite already does — and **patches the tool schemas** a provider is known to reject *without an error*, recording what it found as **`schema_warnings`**. The one rule so far is `array_without_items` → `items: {}`: Gemini function declarations reject an array property with no `items`, and through OpenRouter the request then hangs with **no response headers** until the deployment timeout. That shape took every opencode flash call to a 499 for hours on 2026-09-04 while curl and every other client sailed through, and nothing in the record could say why — the hang was booked against the deployment. Verified live 2026-09-04: with `items: {}` the identical request is served by Gemini directly with zero fallbacks; without it, it falls back. Now the record names the payload *and* says it was repaired:

```bash
jq -c 'select(.schema_warnings != []) | {t, lane, client_ip, schema_warnings}' "$TMPDIR/ferry-logs/ferry-events.ndjson"
# {"t":"…","lane":"domestic.flash","client_ip":"192.168.0.9","schema_warnings":[{"tool":"cdp-toolkit_evaluate_script","path":"args","rule":"array_without_items","fixed":true}]}
```

The patch runs on **every** chat request, not just the Gemini lanes and not just with the tap on: the front cannot know which fallback hop litellm will pick, and `FERRY_EVENTS` is off by default. It touches nothing but `tools[].function.parameters`, only through a named rule, and the schema-repair pass leaves a request with nothing to fix byte-identical; a repair pass that raises sends its input bytes. Separately, when events are enabled, OpenAI chat streams request usage through `stream_options.include_usage=true`. `"fixed": true` marks an entry the registry knew how to repair — a detect-only rule is reported without it. Findings are capped at 20 per request. The registry is `SCHEMA_RULES` (name → description) plus `SCHEMA_FIXES` (name → in-place fix) in [`lib/ferry_events.py`](../lib/ferry_events.py): that pair is where a newly discovered silent rejection gets both its detection and its repair.

With it on, `ferry dash` gains a **Live traffic** panel — every public lane drawn as its chain of hops, the served hop lit green, the hops it walked past lit red with the status code that pushed it on, per-deployment health, and a feed of the last 200 requests — and Grafana gains a **Ferry — Lanes & Fallbacks** dashboard plus three alerts, including `Lane chain exhausted`: *every* deployment in a lane is down at once, which is the outage the old metrics could not name.

Each request shows **First text**, **Total**, **streaming/nonstreaming**, and provider-reported token counts **In / Out / Reasoning**. Hover the metrics for response-start time, cached-input count, and completion status. First text measures the first output text observed at the gateway; reasoning and tool-call-only output do not fabricate a text latency. For nonstreaming responses it measures when the completed text response arrives, not when the provider internally began generating. Total runs through the final response body; interrupted responses are marked **incomplete** and may have partial timing/usage.

**`—` means unknown, not zero.** Older events and providers that omit usage retain unknown values; explicit zero stays zero. Reasoning is a subset of output tokens and is never added to Out. Input follows the wire API: OpenAI input includes cached tokens, while Anthropic input excludes them; cached-input reads are reported separately. The tap passively inspects bounded response fragments to extract timing and numeric usage without persisting response payloads or changing the forwarded bytes. Oversized or unsupported payloads can therefore leave metrics unknown. This observation costs no extra inference calls: OpenAI chat streams request usage on the existing response, and a synchronous LiteLLM adapter hook preserves Anthropic reasoning detail before it is dropped. Anthropic synthetic zero/zero-only usage remains unknown unless the observer has evidence of provider-reported usage, even if the response completes. Deployment median latency and bytes/s now use the measured **total duration**, never LiteLLM's legacy response-header `duration_ms` (which can arrive long before a stream finishes). Bytes/s remains a byte throughput measure, not tokens/s. See the [request metric contract](../observ/CONTRACT.md#request-timing-and-token-fields).

Per-deployment health (`rate_limited` / `quota_exhausted` / `auth_dead` / `unreachable`) is inferred against a classifier table you own: copy [`event-rules.example.json`](../event-rules.example.json) to `~/.config/ferry/event-rules.json` and fill in what your providers actually say. **With no table every failure reads `unknown`** — visible, never silently `healthy`.

## Encrypted drop — crypto detail

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

## Reverse expose — how the bytes move

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

### VNC detail

`ferry expose-vnc` is `ferry expose` with two differences: it reads up to 12
bytes back from `127.0.0.1:<local>` first and refuses to publish anything that
doesn't greet `RFB …` (a dead port never reaches the relay), and it registers
with `"kind": "vnc"` instead of `"tcp"`, so the host can tell a screen from a
plain tunnel. `ferry serve-vnc` reads that same relay state: `/ws/<port>`
bridges a WebSocket to `127.0.0.1:<port>` only for a port the relay currently
lists as `vnc` — a port that isn't published, or is published as plain `tcp`,
is `403`. The first run needs `--fetch` to pull the pinned noVNC 1.7.0 release
(checksum-verified) into `~/.config/ferry/novnc/`.

**Security.** The relay token authenticates the publisher, same as `expose`;
it says nothing about who reaches the screen afterward, so the VNC server's
own password is what gates the screen. The viewer serves plain HTTP on the
LAN like every other ferry port — pass `--bind 127.0.0.1` to `ferry serve-vnc`
to keep it off the LAN entirely.

## Local model known issues

Both lanes run at these settings, so the stack keeps ~33GB of weights resident and two simultaneously-busy deep-context lanes can approach the ~90-100GB wired ceiling. If that bites, shrink the subagent lane first — `LOCAL_SUB_MAX_KV=65536` — since fan-out work rarely needs 128k of context.

**Known issue on the `local-orch` (Qwen) lane (measured 2026-08-25):** deep-context **streaming** requests can die mid-prefill. The mlx-vlm server raises `RuntimeError: There is no Stream(gpu, 1) in current thread` (observed ~40s into a ~44k-token prefill), litellm surfaces it as `MidStreamFallbackError` / `APIConnectionError: An error occurred during streaming`, and the client sees a dropped stream. The server **self-recovers** — subsequent requests succeed, and non-streaming requests were unaffected — so just retry the turn. No cloud fallback is wired for this by design (a dead GPU lane must error, not silently bill a cloud lane).

**MTP draft + quantized KV crashes the qwen3_5 verify path (diagnosed 2026-08-25):** with `--kv-bits` *and* an MTP draft model, the draft-verify branch of mlx-vlm's qwen3_5 attention crashed on **every cache-hit request** (turn 2+ of any conversation): any quantized cache (`BatchQuantizedKVCache`, regardless of 4/8 bits) returns keys as a tuple of `(packed, scales, biases)` arrays, and `prefix_len = keys.shape[-2]` raised `AttributeError` → 500 `APIConnectionError`. Deterministic repro: the same request twice → 200 then 500. **The shipped `local-orch` config is MTP draft + UNquantized KV capped at 64k** (16GB KV budget): stable (3/3 cache-hit requests 200) and ~53% faster decode (37.8 vs 24.8 tok/s). `local-sub` (no drafter, 4-bit KV) is unaffected.

**Compacting big sessions on `opencode-local` produces empty summaries (measured 2026-08-25):** a `/compact` sends the whole transcript (~43k-72k tokens) to the driving lane; on `local-orch` (Qwen 3.8 nvfp4) both observed compacts completed a full prefill and then generated **3 tokens** and stopped — an effectively empty summary. Compacting large sessions via `opencode-cloud` (or at least the first compact of a huge session) is the workaround.

**Known issues on the `local-sub` (Nemotron) lane (measured 2026-08-25):**

- **`nemotron_h` continuous-batching crash (mlx-vlm) — patched automatically.** The batching engine passes both `input_ids` and `inputs_embeds`; the `nemotron_h` `LanguageModel.__call__` forwards both to a backbone that requires exactly one → `ValueError: Provide exactly one of inputs or inputs_embeds` on **every** request. `ferry install` and `host-bootstrap.sh` now apply the two-line fix to `.../site-packages/mlx_vlm/models/nemotron_h/language.py` after installing mlx-vlm. The patch is idempotent and no-ops once upstream fixes the call site — but note that **any manual `uv tool install mlx-vlm --force` wipes it**, so re-run `ferry install` after upgrading mlx-vlm yourself.
- **Flaky `task`-tool calls.** Nemotron frequently emits malformed task calls (hallucinated `task_id`, missing `description`) that opencode rejects *before the tool runs* — the model then silently retries the identical broken call (measured: 444 consecutive errors; also 22 identical 38-token retries). Fix shipped: `client-bootstrap.sh` installs a `/fan-out` command and a `spawning-subagents` skill into `~/.config/opencode/` (in the default scope; a `--profiles-only` / `--no-opencode` client opts in with `--with-guardrails`). The recipe must sit in the **user message** (`/fan-out` does this); placing it in system instructions made failures worse. With it: 3/3 valid parallel task calls, zero schema errors. This matters less now that Nemotron is the *subagent* lane rather than the driver — but it still applies to whatever small local model is driving.
- **Residual model limits.** Bare tool calls (read/write/bash) are reliable; single delegation works. Complex multi-brief orchestration exceeds the 30B model — it duplicates briefs or stops to ask clarifying questions instead of integrating. This is exactly why it sits on `local-sub` and `local-orch` (Qwen) drives.
- **Headless-run doom signature.** Watch the *server* log (opencode's `--format json` stream lags and misses in-flight loops): 3+ consecutive requests with identical generated-token counts and `finish_reason=tool_calls` = kill it.

## Ports

| Port | Purpose | Started by |
|---|---|---|
| **8090** | The endpoint — every lane, for every client | `ferry up` |
| **8091** | Live route-proxy dashboard (localhost only) | `ferry dash` |
| **8092** | `local-orch` MLX backend (**internal** — clients use 8090) | `ferry up` |
| **8093** | `local-sub` MLX backend (**internal** — clients use 8090) | `ferry up` |
| **8094** | Dedicated `schematron` extraction door — ONLY that lane, served beside the main endpoint | `ferry up --schematron` |
| **8095** | LAN share server — client bootstrap, model/file ferry routes, client telemetry | `ferry share` |
| **8096** | HuggingFace pass-through proxy (experimental) | `ferry serve-hf` |
| **8097** | General HTTP(S) download forward proxy | `ferry serve-proxy` |
| **8098** | Reverse-relay control port — a client dials this to register, then publishes one of its own local ports through the host | `ferry relay` |
| **8099** | Browser VNC viewer + WebSocket bridge onto ports published with `ferry expose-vnc` | `ferry serve-vnc` |
| **8100** | `local-schematron` MLX backend (**internal** — clients use 8090, or the 8094 door). 8100 and not a gap in 8090-8099 because that block is full | `ferry up`, `ferry up --schematron` |
| **9099** | Default netcat port for direct `ferry send` / `ferry receive` | `ferry send` / `ferry receive` |
| **3001 / 8429 / 9428 / 9092** | Grafana / VictoriaMetrics / VictoriaLogs / metrics exporter (localhost only) | `ferry dash --grafana` |

## Test suite detail

Run every Python suite, the dashboard JavaScript checks, and the generated CLI guard from the repository root. Python tests use stdlib `unittest`; integration cases can invoke the project's command-line dependencies such as zsh and OpenSSL. The UI checks use Node.js.

```bash
for suite in lib/*.test.py observ/*.test.py; do
  python3 "$suite" || exit 1
done
node lib/ferry-dashui.test.mjs
zsh build.zsh --check
```

Each Python suite is also runnable on its own. The ChatGPT compatibility and usage-hook suites exercise installed LiteLLM adapters when available; their installed-adapter cases skip when LiteLLM is absent. Optionally rerun both with the host's LiteLLM Python environment:

```bash
"$(uv tool dir)/litellm/bin/python" lib/ferry-chatgpt-compat.test.py
"$(uv tool dir)/litellm/bin/python" lib/ferry-usage-hook.test.py
```

The share and host-reset suites deliberately run the **real** embedded Python — extracted out of the built `ferry` and out of `host-reset.sh` — rather than a reimplementation, so an edit that breaks the shipped behaviour fails in the suite instead of on a laptop.

`python3 lib/ferry-front.test.py` now also covers the `CHATGPT_DEFAULT_INSTRUCTIONS` resolver — the env override, the `FERRY_CHATGPT_INSTRUCTIONS` path and `off` cases, the user config file, and the shipped fallback — alongside its existing `/v1/models` filtering suite.

The client-scope suite goes further: it runs `client-bootstrap.sh`, `client-reset.sh` and `client-cleanup.sh` end-to-end against a throwaway `$HOME` and a stub host that serves `/v1/models` and the repo's own `ferry`. The property it defends is an *absence* — that the narrow scopes never create `~/.config/opencode`, and that cleanup leaves everything that isn't ferry's standing — and an absence is only proved by looking. The client-bootstrap and host-reset suites cover the `using-the-goal-plugin` skill the same way they already cover the `spawning-subagents` guardrail.
