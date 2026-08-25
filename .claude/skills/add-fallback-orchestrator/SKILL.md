---
name: add-fallback-orchestrator
description: Use when adding a fallback/backup model to the orch lane in llm-ferry's route proxy (~/.config/ferry/litellm.yaml) so it fails over to an independent model on error. Covers strict-failover wiring (separate model_name, router_settings.fallbacks, chain order), the independent-capacity rule, and router tuning. For the load-balanced worker pool instead, use add-worker-model.
---

# Add a fallback orchestrator to the ferry route proxy

The **`orch` lane** is the big planning/driving model. A **fallback orchestrator** is an
INDEPENDENT model litellm reroutes to when `orch` errors. You edit
`~/.config/ferry/litellm.yaml` (seeded from `litellm-route-example.yaml`).

Ferry serves four lanes: `orch` and `flash` (cloud) plus `local-orch` and `local-sub`
(host GPU). Only `orch` gets a failover chain. The two LOCAL lanes are deliberately
OUTSIDE every chain — the point of naming a local lane is that the request stays on the
machine, so a dead GPU lane must error rather than silently spend a cloud quota. Do not
"helpfully" add fallbacks for them.

## Strict failover vs. the worker pool — pick the right skill
- **This skill (fallback orchestrator):** STRICT FAILOVER. A SEPARATE `model_name`, reached
  ONLY on error, via `router_settings.fallbacks`, in the order you list. Order matters.
- **`add-worker-model` (worker pool):** LOAD-BALANCED. IDENTICAL `model_name` deployments
  that `usage-based-routing-v2` splits across proactively (even split), not on error.

A fallback is NOT another deployment reusing the primary's `orch` name. It is a new name
(`orch-fallback`, `orch-deepseek`, …) wired into `fallbacks`.

## Checklist
1. **Capacity MUST be independent of the primary** (the #1 mistake). A fallback sharing a
   rate-limit bucket with the primary — OR with your own interactive use of that same
   account — will `429` exactly when you need it. Prefer a DIFFERENT provider/account. A
   pay-per-token API (e.g. Fireworks) has its OWN capacity and only bills when the
   fallback actually fires.
   **Do not use a Claude Max/Pro SUBSCRIPTION token (`sk-ant-oat…`) as a fallback.** It
   isn't just bucket-sharing: Anthropic restricts those OAuth tokens to Claude Code
   itself and rejects other clients' traffic with an opaque `429 rate_limit_error`
   (even with the quota untouched — and `/v1/models` still returns 200, so that check
   proves nothing). litellm's `sk-ant-oat` Bearer handling does not get around this.
   For a Claude fallback use a pay-per-token **API key** (`ANTHROPIC_API_KEY`,
   `sk-ant-api…`) — independent bucket, supported, bills only when the hop fires.
2. **Add a new `model_name` block** per fallback hop in `model_list` (NOT under the
   `orch` name, NOT in any pool).
3. **Chain order: fast first, slow/cheap last.** The example chains DeepSeek V4 Pro (fast,
   carries the real work) first and GLM 5.2 (slower) as last resort. Multiple hops allowed.
4. **Wire it into `router_settings.fallbacks`.** Only `orch` gets a failover chain; the
   worker pool and both local lanes are untouched.
5. **Provider format:** for an Anthropic-format endpoint, litellm appends `/v1/messages` to
   `api_base` (so `api_base` OMITS it). For a plain OpenAI-compatible provider, DROP
   `api_base` and use `model: openai/<id>` with `api_key: os.environ/<VAR>`.
6. **Tune the router for fallbacks** (see below).
7. **Apply + verify** (see below).

## Before / after (matches `litellm-route-example.yaml`)

Primary orchestrator already present:
```yaml
model_list:
  - model_name: orch
    litellm_params:
      model: anthropic/k3-256k
      api_key: os.environ/KIMI_API_KEY
      api_base: https://api.kimi.com/coding   # -> .../coding/v1/messages
      timeout: 600
```

ADD the fallback block(s) — separate names, on an INDEPENDENT account:
```yaml
  - model_name: orch-fallback          # 1st fallback: fast, carries the work
    litellm_params:
      model: fireworks_ai/accounts/fireworks/models/deepseek-v4-pro-0813
      api_key: os.environ/FIREWORKS_API_KEY    # pay-per-token: own capacity, no contention
      timeout: 600
  - model_name: orch-fallback-2        # last resort: slower/cheaper
    litellm_params:
      model: fireworks_ai/accounts/fireworks/models/glm-5p2
      api_key: os.environ/FIREWORKS_API_KEY
      timeout: 600
```

For a plain OpenAI-compatible fallback instead (no `api_base`):
```yaml
  - model_name: orch-fallback
    litellm_params:
      model: openai/<id>
      api_key: os.environ/<VAR>
      timeout: 600
```

ADD the mapping under `router_settings` (only `orch` is remapped):
```yaml
router_settings:
  fallbacks: [{"orch": ["orch-fallback", "orch-fallback-2"]}]
```

## ChatGPT subscription as a fallback (litellm `chatgpt/` provider)

Unlike a Claude subscription token (Anthropic gates those to Claude Code — see step 1), a
**ChatGPT Plus/Pro subscription** CAN back a fallback, via litellm's native `chatgpt/`
provider (it talks to `chatgpt.com/backend-api/codex`, the Responses API Codex uses).

1. **Log in once (device code).** litellm ships a ChatGPT authenticator that writes a token
   to `~/.config/litellm/chatgpt/auth.json`. Auth is THAT file, NOT `OPENAI_API_KEY` (which
   is pay-per-token OpenAI billing on a different bucket). The deployment's `api_key` is a
   required-but-ignored placeholder.
2. **Deployment shape** — the `responses/` segment scopes litellm's chat→responses bridge:
   ```yaml
     - model_name: orch-chatgpt
       litellm_params:
         model: chatgpt/responses/gpt-5.6-sol   # gpt-5.6-sol / gpt-5.4 work; codex-* ids are rejected
         api_key: "chatgpt-oauth"               # placeholder; provider reads auth.json
         reasoning_effort: max                  # backend accepts "max"
         timeout: 600
   ```
3. **STREAMING-ONLY (litellm 1.97.0).** The Codex backend always streams and litellm only
   reassembles the reply on the STREAMING path. A STREAMED `/v1/chat/completions` returns 200
   (opencode + every agentic client streams — the real path). A NON-STREAMED call hits a
   litellm bridge bug (`ChatgptException - Unknown items in responses API response: []`, HTTP
   500). That is benign in a fallback chain — a non-streaming caller that reaches this hop
   just rolls on to the next fallback. Don't make it the sole/last hop if non-streaming
   callers must succeed there, and don't chase the 500 as a config error.

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
ferry down && ferry up             # reload ~/.config/ferry/litellm.yaml
ferry dash                          # then click "Test backends"
```
"Test backends" actively pings each backend and reports WHICH fallback hop served + latency
(the dashboard also renders the `orch` topology: primary -> the `fallbacks` chain, and probes all four lanes).
A `rate_limit_error` from the primary is EXPECTED/NORMAL when the fallback is doing its job
— it is NOT an auth failure. Make sure the fallback's key is exported (shell or
`~/.config/ferry/secrets.env`); never commit real keys.
