---
name: add-worker-model
description: Use when adding a worker lane or more worker API keys (e.g. extra Gemini keys) to llm-ferry's load-balanced pool in ~/.config/ferry/litellm.yaml. Covers pool-vs-fallback, the per-project quota rule, and lane naming/aliasing. For the orch lane's strict failover chain instead, use add-fallback-orchestrator; the local GPU lanes take neither.
---

# Add a worker model to llm-ferry's route proxy

Workers are the cheap, high-volume lanes (the shipped one is `flash`) that serve the bulk of traffic. They live in a **load-balanced pool** in `~/.config/ferry/litellm.yaml` and are served by `ferry up` (or `ferry up --route` for the cloud lanes alone).

**Lane names are the contract.** Clients bind to a name (`orch`, `flash`, `local-orch`, `local-sub`), not to a model id — so prefer re-pointing an existing lane over minting a new name whenever the ROLE is unchanged. If you must rename one, alias the old name with `router_settings.model_group_alias: {old: {model: "new", hidden: true}}` so wired clients keep working while `/v1/models` shows only the real lanes.

**Pool vs. fallback — pick the right skill:**
- **Worker pool (this skill):** multiple **identical** `model_name` deployments. `usage-based-routing-v2` proactively splits calls to the **least-used** key (an even split, not just error-triggered). Order does not matter; no `fallbacks:` entry.
- **Orchestrator fallback (`add-fallback-orchestrator`):** **separate** `model_name`s wired into `router_settings.fallbacks`, reached **only on error**, in strict order. Use that skill for the `orch` lane's failover chain.

## Two things you might be doing
- **A) Grow an existing pool** — add another API key to a lane you already serve (most common: more Gemini keys behind `flash`). Append an identical-`model_name` deployment.
- **B) Add a distinct worker lane** — a brand-new `model_name` clients can select via `/v1/models`. Single deployment, or its own multi-key pool. Name it for its ROLE, matching the existing short lane names.

## Checklist
1. Mint/obtain the key. **For real throughput scaling, put each key in its OWN provider project** (see quota note below).
2. Export it under the next `_N` env var (shell or `~/.config/ferry/secrets.env`). Never commit real keys.
3. Edit `~/.config/ferry/litellm.yaml` — append a deployment block (A) or a new `model_name` (B).
4. **Do NOT** add worker `model_name`s to `router_settings.fallbacks`. The pool self-balances. (The shipped config gives `flash` ONE fallback — pool-exhaustion overflow to `orch` — which is a spillover valve, not a failover chain.)
5. Apply: `ferry down && ferry up`.
6. Verify each key independently (below), spread out so you don't trip fresh-key rate limits.

## The per-project quota nuance (READ THIS)
For **Gemini, quota is per Google Cloud PROJECT, not per key.** Two keys minted in the **same** project share **one** quota bucket — a same-project second key buys you failover redundancy but **zero extra headroom**. For genuine horizontal scaling, **each key must live in its own Google Cloud project.**

Generalize it: **before assuming N keys = N× throughput, check whether your provider meters by key or by account/project.** Adjust how many pool deployments are worth adding accordingly.

## Env var convention
`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3`, … `GEMINI_API_KEY_N` — each referenced as `api_key: os.environ/GEMINI_API_KEY_N`. Export in your shell or drop them in `~/.config/ferry/secrets.env`:

```bash
export GEMINI_API_KEY="..."          # project #1
export GEMINI_API_KEY_2="..."        # project #2  (own GCP project = real headroom)
export GEMINI_API_KEY_3="..."        # project #3
```

## A) Grow the pool — identical `model_name`, different key
A pool is simply **repeated deployment blocks with the SAME `model_name`**. Add one block per key. On a 429/error litellm retries on another key and cools the failed one out for `cooldown_time`.

```yaml
model_list:
  # -- Worker pool: the `flash` lane across N keys (each in its OWN project) --
  - model_name: flash
    litellm_params:
      model: gemini/gemini-3.7-flash
      api_key: os.environ/GEMINI_API_KEY        # project #1

  - model_name: flash
    litellm_params:
      model: gemini/gemini-3.7-flash
      api_key: os.environ/GEMINI_API_KEY_2      # project #2

  - model_name: flash                           # <- the block you're adding
    litellm_params:
      model: gemini/gemini-3.7-flash
      api_key: os.environ/GEMINI_API_KEY_3      # project #3
```

Scale to however many independent-project keys you have — add/delete blocks to match. `router_settings.routing_strategy: usage-based-routing-v2` handles the even split; nothing else to wire.

## B) Add a distinct worker model — a new `model_name`
Give it a fresh `model_name` (single deployment shown; repeat the block with more keys for its own pool). Clients then select it by name via `/v1/models`.

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
  "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY_3"
```
Or watch the pool live with `ferry dash`.

**Warning:** hammering a fresh free-tier key with a burst can trip per-project RPM limits — that returns a **RateLimitError (429), NOT an auth failure**. The key is fine; you're just over the per-minute rate. Spread verification out rather than firing all keys at once.
