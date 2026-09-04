---
name: add-worker-model
description: Use when adding a worker lane or more worker keys to llm-ferry's load-balanced pool in ~/.config/ferry/litellm.yaml. Covers pool-vs-fallback, the per-project quota rule and the Google ToS line you must NOT cross, and lane naming/aliasing. For the heavy driver lane's strict failover chain instead, use add-fallback-orchestrator; the local GPU lanes take neither.
---

# Add a worker model to llm-ferry's route proxy

Workers are the cheap, high-volume lanes that serve the bulk of traffic. The shipped ones
are **`flash`** and **`super-flash`** — both single OpenRouter Gemini 3.8 Flash deployments,
routed to whichever upstream provider is fastest right now, each carrying one Luna fallback
hop. They live in a **load-balanced pool** in `~/.config/ferry/litellm.yaml` and are served
by `ferry up` (or `ferry up --route` for the cloud lanes alone).

Since fleets (2026-09-04) lane names are `<fleet>.<lane>`; chains never cross a fleet.

**Lane names are the contract.** Clients bind to a name (`heavy`, `flash`, `super-flash`,
`local-orch`, `local-sub`), not to a model id — so prefer re-pointing an existing lane over
minting a new name whenever the ROLE is unchanged. The current config has NO
`model_group_alias` entries, and for good reason: an alias silently loses its whole fallback
chain (a client that hits the alias never reaches the aliased lane's fallback hops). If you
must rename a lane, keep it a REAL `model_name` — don't alias — and hide it from the public
catalog with `public: false` instead; that controls `/v1/models` visibility without touching
routing.

**Pool vs. fallback — pick the right skill:**
- **Worker pool (this skill):** multiple **identical** `model_name` deployments. `usage-based-routing-v2` proactively splits calls to the **least-used** key (an even split, not just error-triggered). Order does not matter; no `fallbacks:` entry.
- **Orchestrator fallback (`add-fallback-orchestrator`):** **separate** `model_name`s wired into `router_settings.fallbacks`, reached **only on error**, in strict order. Use that skill for a lane's failover hop — the worker lanes' single Luna hop, or the driver's chain if you decide to add one.

## Two things you might be doing
- **A) Grow an existing pool** — add another key to a lane you already serve. Legitimate ONLY when the key represents a genuinely separate account or provider (see the ToS rule below). Append an identical-`model_name` deployment.
- **B) Add a distinct worker lane** — a brand-new `model_name` clients can select via `/v1/models`. Single deployment, or its own multi-key pool. Name it for its ROLE, matching the existing short lane names.

## Checklist
1. Mint/obtain the key. Extra headroom must come from a genuinely **separate provider or account** — NEVER from a second Google Cloud project under the same Google account (see the ToS rule below).
2. Export it under a distinctive env var (shell or `~/.config/ferry/secrets.env`). Never commit real keys.
3. Edit `~/.config/ferry/litellm.yaml` — append a deployment block (A) or a new `model_name` (B).
4. **Do NOT** add a worker's OWN pool members (repeated identical `model_name` blocks) into `router_settings.fallbacks` — the pool self-balances via `usage-based-routing-v2`. The shipped config DOES give `flash` and `super-flash` each ONE outbound entry in `fallbacks` (to `flash-luna` / `super-flash-luna`), but that's the lane's failover hop, not pool routing — see `add-fallback-orchestrator` for how that hop is wired and why it's a single hop rather than another pool member.
5. Apply: `ferry reload` (config-only). Use `ferry down && ferry up` for a full restart.
6. Verify each key independently (below), spread out so you don't trip fresh-key rate limits.

## The quota rule AND the ToS line (READ THIS)

For **Gemini, quota is per Google Cloud PROJECT, not per key.** Two keys minted in the **same** project share **one** quota bucket — a same-project second key buys failover redundancy but **zero extra headroom**. And N keys in N projects really WOULD multiply throughput…

**…and that is exactly why it is prohibited. Do not do it.** Spreading load across multiple projects to exceed a per-project rate limit is circumvention under the **Google APIs Terms of Service §2.d** ("Google sets and enforces limits on your use of the APIs … you will not attempt to circumvent such limitations"; use beyond limits requires Google's *express consent*), reinforced by §2.b ("You will not violate any other terms of service with Google"). This is not theoretical: on **2026-08-25** Google Cloud Trust & Safety suspended **nine** burst-created projects in one night, deleted them, and **restricted the account's OAuth APIs** — the ten-key pool this stack used to ship was dismantled the next day.

**The sanctioned ways to get more Gemini throughput, in order:**
1. **Raise the paid tier on the ONE project.** Tier 3 lifts Gemini 3.7 Flash from 3M to **20M TPM** with unlimited RPD — comfortably above the ~7.3M TPM peak the old ten-project pool actually hit. (Tiers move on billing history / support request, not a dashboard button.)
2. **Ask Google** for express consent / a quota increase (ToS §2.d's own escape hatch) via Cloud support.
3. **Add capacity elsewhere**: a genuinely different provider or a different Google *account* (e.g. work + personal) — a second *billing account* under the same account buys nothing; limits are per-project.

A multi-key pool IS still legitimate when the keys represent **genuinely separate accounts or providers** (a Fireworks key beside a Gemini key; two different Google accounts), or for **failover redundancy** rather than headroom.

Generalize it: **before assuming N keys = N× throughput, check (a) whether your provider meters by key or by account/project, and (b) whether pooling keys to multiply that meter is allowed at all.** Both questions gate how many deployments are worth adding.

## The shipped shape: a single OpenRouter deployment, not a pool

`flash` and `super-flash` are each ONE deployment today — no pool — because OpenRouter's
`provider.sort: throughput` already routes each call to whichever upstream is fastest, so
there's no per-key headroom to buy by pooling here. This is the shape to copy for a new
single-deployment worker lane:

```yaml
model_list:
  - model_name: flash
    litellm_params:
      model: openrouter/google/gemini-3.8-flash
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body:
        provider:
          sort: throughput
      timeout: 600

  - model_name: super-flash
    litellm_params:
      model: openrouter/google/gemini-3.8-flash
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body:
        provider:
          sort: throughput
        reasoning:
          effort: minimal          # Gemini 3.8 Flash via OpenRouter refuses to disable reasoning entirely — `minimal` is the floor
      timeout: 600
```

Each carries its own one-hop Luna fallback (`flash-luna` / `super-flash-luna`) — wiring
that hop is `add-fallback-orchestrator`'s job, not this skill's; don't duplicate it here.

## When a pool IS the right shape (illustration)

Not the shape of `flash`/`super-flash` above — this is what a legitimate multi-key pool
looks like when the provider actually meters per key/account (unlike OpenRouter's
throughput routing). Direct Gemini API access is the clearest example, and it's exactly
where the quota-and-ToS rule above matters most:

```yaml
model_list:
  # -- Worker pool: identical deployments across INDEPENDENT keys/accounts --
  # (same-queue identical models; each key a separate provider or account)
  - model_name: flash-direct
    litellm_params:
      model: gemini/gemini-3.8-flash
      api_key: os.environ/GEMINI_API_KEY        # the one Google project

  - model_name: flash-direct
    litellm_params:
      model: gemini/gemini-3.8-flash
      api_key: os.environ/GEMINI_API_KEY_2      # a DIFFERENT Google account
```

`router_settings.routing_strategy: usage-based-routing-v2` handles the even split; nothing else to wire.

## B) Add a distinct worker model — a new `model_name`
Give it a fresh `model_name` (single deployment shown; repeat the block with more independent keys for its own pool, if the provider's quota model actually justifies one — see above). Clients then select it by name via `/v1/models`.

```yaml
  - model_name: flash-lite
    litellm_params:
      model: gemini/gemini-3.5-flash-lite
      api_key: os.environ/GEMINI_API_KEY
```

Still **no** `fallbacks:` entry pointing INTO this lane from elsewhere — workers are never
part of the driver's (`heavy`) failover chain. Give the new lane its own hop, if it needs
one, via `add-fallback-orchestrator`.

## Env var convention
One env var per key, named for its provider: `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, … Use `_N` suffixes only when you hold multiple keys that are **genuinely independent** — separate accounts or providers:

```bash
export OPENROUTER_API_KEY="..."      # flash / super-flash and their Luna hops
export GEMINI_API_KEY="..."          # only if you're building the direct-Gemini pool illustration above
export GEMINI_API_KEY_2="..."        # ONLY if this is a DIFFERENT Google account
```

## Apply + verify
```bash
ferry reload                        # config-only: re-reads litellm.yaml
ferry down && ferry up              # full restart — use when ferry reload isn't enough
```
Verify **each key independently**, one at a time:

OpenRouter (the shipped `flash`/`super-flash` shape):
```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  https://openrouter.ai/api/v1/models
```

Gemini direct API — only relevant if you built the pool illustration above:
```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"
```

Or watch the pool live with `ferry dash`.

**Gemini 3.8 Flash via OpenRouter spends its mandatory reasoning tokens out of the SAME
`max_tokens` budget as the visible reply.** A probe with a tiny budget (e.g. `max_tokens:
50`) burns the whole budget on reasoning and comes back with `content: null` and
`finish_reason: length` — that LOOKS like a broken lane but the lane is healthy, you just
starved it. Probe with `max_tokens: 400` or more to get an actual reply back before
concluding anything is wrong.

**Warning:** hammering a fresh key with a burst can trip per-project RPM limits — that returns a **RateLimitError (429), NOT an auth failure**. The key is fine; you're just over the per-minute rate. Spread verification out rather than firing all keys at once.
