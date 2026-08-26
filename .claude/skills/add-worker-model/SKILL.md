---
name: add-worker-model
description: Use when adding a worker lane or more worker keys to llm-ferry's load-balanced pool in ~/.config/ferry/litellm.yaml. Covers pool-vs-fallback, the per-project quota rule and the Google ToS line you must NOT cross, and lane naming/aliasing. For the orch lane's strict failover chain instead, use add-fallback-orchestrator; the local GPU lanes take neither.
---

# Add a worker model to llm-ferry's route proxy

Workers are the cheap, high-volume lanes (the shipped one is `flash`) that serve the bulk of traffic. They live in a **load-balanced pool** in `~/.config/ferry/litellm.yaml` and are served by `ferry up` (or `ferry up --route` for the cloud lanes alone).

**Lane names are the contract.** Clients bind to a name (`orch`, `flash`, `local-orch`, `local-sub`), not to a model id — so prefer re-pointing an existing lane over minting a new name whenever the ROLE is unchanged. If you must rename one, alias the old name with `router_settings.model_group_alias: {old: {model: "new", hidden: true}}` so wired clients keep working while `/v1/models` shows only the real lanes.

**Pool vs. fallback — pick the right skill:**
- **Worker pool (this skill):** multiple **identical** `model_name` deployments. `usage-based-routing-v2` proactively splits calls to the **least-used** key (an even split, not just error-triggered). Order does not matter; no `fallbacks:` entry.
- **Orchestrator fallback (`add-fallback-orchestrator`):** **separate** `model_name`s wired into `router_settings.fallbacks`, reached **only on error**, in strict order. Use that skill for the `orch` lane's failover chain.

## Two things you might be doing
- **A) Grow an existing pool** — add another key to a lane you already serve. Legitimate ONLY when the key represents a genuinely separate account or provider (see the ToS rule below). Append an identical-`model_name` deployment.
- **B) Add a distinct worker lane** — a brand-new `model_name` clients can select via `/v1/models`. Single deployment, or its own multi-key pool. Name it for its ROLE, matching the existing short lane names.

## Checklist
1. Mint/obtain the key. Extra headroom must come from a genuinely **separate provider or account** — NEVER from a second Google Cloud project under the same Google account (see the ToS rule below).
2. Export it under a distinctive env var (shell or `~/.config/ferry/secrets.env`). Never commit real keys.
3. Edit `~/.config/ferry/litellm.yaml` — append a deployment block (A) or a new `model_name` (B).
4. **Do NOT** add worker `model_name`s to `router_settings.fallbacks`. The pool self-balances. (The shipped config gives `flash` ONE fallback — pool-exhaustion overflow to `orch` — which is a spillover valve, not a failover chain.)
5. Apply: `ferry down && ferry up`.
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

## Env var convention
One env var per key, named for its provider: `GEMINI_API_KEY`, `FIREWORKS_API_KEY`, … Use `_N` suffixes only when you hold multiple keys that are **genuinely independent** — separate accounts or providers:

```bash
export GEMINI_API_KEY="..."          # the ONE Google project's key
export FIREWORKS_API_KEY="..."       # a different provider — real extra capacity
export GEMINI_API_KEY_2="..."        # ONLY if this is a DIFFERENT Google account
```

## A) Grow the pool — identical `model_name`, different (independent) key
A pool is simply **repeated deployment blocks with the SAME `model_name`**. Add one block per genuinely independent key. On a 429/error litellm retries on another key and cools the failed one out for `cooldown_time`.

```yaml
model_list:
  # -- Worker pool: the `flash` lane across INDEPENDENT keys/accounts --
  # (same-queue identical models; each key a separate provider or account)
  - model_name: flash
    litellm_params:
      model: gemini/gemini-3.7-flash
      api_key: os.environ/GEMINI_API_KEY        # the one Google project

  - model_name: flash
    litellm_params:
      model: gemini/gemini-3.7-flash
      api_key: os.environ/GEMINI_API_KEY_2      # a DIFFERENT Google account
```

`router_settings.routing_strategy: usage-based-routing-v2` handles the even split; nothing else to wire.

## B) Add a distinct worker model — a new `model_name`
Give it a fresh `model_name` (single deployment shown; repeat the block with more independent keys for its own pool). Clients then select it by name via `/v1/models`.

```yaml
  - model_name: flash-lite
    litellm_params:
      model: gemini/gemini-3.7-flash-lite
      api_key: os.environ/GEMINI_API_KEY
```

Still **no** `fallbacks:` entry — workers are never part of the `orch` lane's failover chain.

## Apply + verify
```bash
ferry down && ferry up
```
Verify **each key independently** with a direct provider models-list call (HTTP 200 = key is live), one at a time:
```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"
```
Or watch the pool live with `ferry dash`.

**Warning:** hammering a fresh key with a burst can trip per-project RPM limits — that returns a **RateLimitError (429), NOT an auth failure**. The key is fine; you're just over the per-minute rate. Spread verification out rather than firing all keys at once.
