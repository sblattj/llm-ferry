---
name: add-fallback-orchestrator
description: Use when adding a fallback/backup orchestrator model to llm-ferry's route proxy (~/.config/ferry/litellm.yaml) so the primary orchestrator fails over to an independent model on error. Covers strict-failover wiring (separate model_name, router_settings.fallbacks, chain order), the independent-capacity rule, and router tuning. For the load-balanced worker pool instead, use add-worker-model.
---

# Add a fallback orchestrator to the ferry route proxy

The **orchestrator** is the big planning/driving model (e.g. Kimi K3). A **fallback
orchestrator** is an INDEPENDENT model litellm reroutes to when the primary orchestrator
errors. You edit `~/.config/ferry/litellm.yaml` (seeded from `litellm-route-example.yaml`).

## Strict failover vs. the worker pool — pick the right skill
- **This skill (fallback orchestrator):** STRICT FAILOVER. A SEPARATE `model_name`, reached
  ONLY on error, via `router_settings.fallbacks`, in the order you list. Order matters.
- **`add-worker-model` (worker pool):** LOAD-BALANCED. IDENTICAL `model_name` deployments
  that `usage-based-routing-v2` splits across proactively (even split), not on error.

A fallback is NOT another deployment reusing the primary's `orchestrator` name. It is a
new name (`orchestrator-fallback`, `orchestrator-fallback-2`) wired into `fallbacks`.

## Checklist
1. **Capacity MUST be independent of the primary** (the #1 mistake). A fallback sharing a
   rate-limit bucket with the primary — OR with your own interactive use of that same
   account (classic trap: pointing the fallback at a Claude Max/Pro subscription your
   interactive Claude Code sessions already drain) — will `429` exactly when you need it.
   Prefer a DIFFERENT provider/account. A pay-per-token API (e.g. Fireworks) has its OWN
   capacity and only bills when the fallback actually fires.
2. **Add a new `model_name` block** per fallback hop in `model_list` (NOT under the
   `orchestrator` name, NOT in any pool).
3. **Chain order: fast first, slow/cheap last.** The example chains DeepSeek V4 Pro (fast,
   carries the real work) first and GLM 5.2 (slower) as last resort. Multiple hops allowed.
4. **Wire it into `router_settings.fallbacks`.** Only the orchestrator gets a fallback; the
   worker pool is untouched.
5. **Provider format:** for an Anthropic-format endpoint, litellm appends `/v1/messages` to
   `api_base` (so `api_base` OMITS it). For a plain OpenAI-compatible provider, DROP
   `api_base` and use `model: openai/<id>` with `api_key: os.environ/<VAR>`.
6. **Tune the router for fallbacks** (see below).
7. **Apply + verify** (see below).

## Before / after (matches `litellm-route-example.yaml`)

Primary orchestrator already present:
```yaml
model_list:
  - model_name: orchestrator
    litellm_params:
      model: anthropic/k3-256k
      api_key: os.environ/KIMI_API_KEY
      api_base: https://api.kimi.com/coding   # -> .../coding/v1/messages
      timeout: 600
```

ADD the fallback block(s) — separate names, on an INDEPENDENT account:
```yaml
  - model_name: orchestrator-fallback          # 1st fallback: fast, carries the work
    litellm_params:
      model: fireworks_ai/accounts/fireworks/models/deepseek-v4-pro-0813
      api_key: os.environ/FIREWORKS_API_KEY    # pay-per-token: own capacity, no contention
      timeout: 600
  - model_name: orchestrator-fallback-2        # last resort: slower/cheaper
    litellm_params:
      model: fireworks_ai/accounts/fireworks/models/glm-5p2
      api_key: os.environ/FIREWORKS_API_KEY
      timeout: 600
```

For a plain OpenAI-compatible fallback instead (no `api_base`):
```yaml
  - model_name: orchestrator-fallback
    litellm_params:
      model: openai/<id>
      api_key: os.environ/<VAR>
      timeout: 600
```

ADD the mapping under `router_settings` (only `orchestrator` is remapped):
```yaml
router_settings:
  fallbacks: [{"orchestrator": ["orchestrator-fallback", "orchestrator-fallback-2"]}]
```

## Router tuning for fallbacks
```yaml
router_settings:
  allowed_fails: 3      # tolerate a few 429s before cooling a deployment out — matters for
                        # a SOLE fallback with no sibling to roll to
  cooldown_time: 5      # short cooldown so a cooled hop recovers fast; one 429 can't lock
                        # out the only path for a full minute
  num_retries: 1        # retry once, THEN fall back
```
Keep `num_retries` LOW. A hard-down primary often returns an error litellm maps to a
RETRYABLE class (e.g. a quota `403` -> `APIConnectionError`), so a high `num_retries` adds
retry-backoff latency BEFORE the fallback fires. `1` stays resilient to a blip without the
penalty. (`litellm_settings.num_retries` may be higher for the pool; the `router_settings`
value governs failover.)

## Apply + verify
```bash
ferry down && ferry up --route      # reload ~/.config/ferry/litellm.yaml
ferry dash                          # then click "Test backends"
```
"Test backends" actively pings each backend and reports WHICH fallback hop served + latency
(the dashboard also renders the orchestrator topology: primary -> the `fallbacks` chain).
A `rate_limit_error` from the primary is EXPECTED/NORMAL when the fallback is doing its job
— it is NOT an auth failure. Make sure the fallback's key is exported (shell or
`~/.config/ferry/secrets.env`); never commit real keys.
