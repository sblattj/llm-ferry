---
name: add-fallback-orchestrator
description: Use when adding a fallback hop to a lane in llm-ferry's route proxy (~/.config/ferry/litellm.yaml) so it fails over to an independent model on error — the worker lanes' single Luna hop (flash/super-flash) is the live, worked example; wiring a chain onto the heavy driver lane (legacy orch/orchestrator) is the "if you decide it needs one" case, against current house policy. Covers strict-failover wiring (separate model_name, router_settings.fallbacks, chain order), the independent-capacity rule, and router tuning. For the load-balanced worker pool instead, use add-worker-model.
---

# Add a fallback hop to a ferry lane

Ferry's house shape (since 2026-09-04): **`heavy`** (legacy `orch`/`orchestrator`) is the
big driver — `chatgpt/responses/gpt-5.6-sol` via the ChatGPT subscription bridge,
`reasoning_effort: xhigh`. **`flash`** and **`super-flash`** are the worker lanes — both
OpenRouter Gemini 3.8 Flash. **`local-orch`**/**`local-sub`** are the host GPU lanes. You
edit `~/.config/ferry/litellm.yaml` (seeded from `litellm-route-example.yaml`).

**House policy on chains, stated explicitly: the DRIVER carries NO fallback chain; each
WORKER lane carries exactly ONE hop.** `heavy` must error rather than silently move a
session onto a different model mid-flight — that's a deliberate choice, not an oversight,
so don't "helpfully" add one without consciously deciding to override the policy (see the
driver case below). Each worker lane spills to one independent Luna deployment on error:
`flash` -> `flash-luna`, `super-flash` -> `super-flash-luna`. The two local lanes get
neither — the point of naming a local lane is that the request stays on the machine, so a
dead GPU lane must error rather than silently spend a cloud quota. Do not "helpfully" add
fallbacks for them either.

This skill covers wiring a fallback hop onto ANY lane. The **worker -> Luna hop is the
primary worked example**, because that's what's actually live today. Giving the **driver**
a chain is the secondary case, covered near the end, for if you decide `heavy` needs one
badly enough to override house policy.

## Strict failover vs. the worker pool — pick the right skill
- **This skill (fallback hop):** STRICT FAILOVER. A SEPARATE `model_name`, reached
  ONLY on error, via `router_settings.fallbacks`, in the order you list. Order matters.
- **`add-worker-model` (worker pool):** LOAD-BALANCED. IDENTICAL `model_name` deployments
  that `usage-based-routing-v2` splits across proactively (even split), not on error.

A fallback is NOT another deployment reusing the primary's `model_name`. It is a new name
(`flash-luna`, `heavy-fallback`, …) wired into `fallbacks`.

## Checklist
1. **Capacity MUST be independent of the primary** (the #1 mistake). A fallback sharing a
   rate-limit bucket with the primary — OR with your own interactive use of that same
   account — will `429` exactly when you need it. Prefer a DIFFERENT provider/account. A
   pay-per-token API (e.g. OpenRouter) has its OWN capacity and only bills when the
   fallback actually fires.
   **Do not use a Claude Max/Pro SUBSCRIPTION token (`sk-ant-oat…`) as a fallback.** It
   isn't just bucket-sharing: Anthropic restricts those OAuth tokens to Claude Code
   itself and rejects other clients' traffic with an opaque `429 rate_limit_error`
   (even with the quota untouched — and `/v1/models` still returns 200, so that check
   proves nothing). litellm's `sk-ant-oat` Bearer handling does not get around this.
   For a Claude fallback use a pay-per-token **API key** (`ANTHROPIC_API_KEY`,
   `sk-ant-api…`) — independent bucket, supported, bills only when the hop fires.
2. **Add a new `model_name` block** per fallback hop in `model_list` (NOT under the
   primary lane's own name, e.g. not under `flash` or `heavy`, and NOT in any pool).
3. **Chain order: fast first, slow/cheap last**, when a lane has more than one hop. The
   worker lanes today each have exactly one hop (`flash-luna`, `super-flash-luna`), so this
   mostly matters if you build a multi-hop chain for the driver (below).
4. **Wire it into `router_settings.fallbacks`.** Under the current house shape:
   `flash` -> `[flash-luna]`, `super-flash` -> `[super-flash-luna]`. `heavy` and both local
   lanes are untouched unless you're deliberately adding the driver-chain case.
5. **Provider format:** for an Anthropic-format endpoint, litellm appends `/v1/messages` to
   `api_base` (so `api_base` OMITS it). For a plain OpenAI-compatible provider, DROP
   `api_base` and use `model: openai/<id>` with `api_key: os.environ/<VAR>`.
6. **Tune the router for fallbacks** (see below).
7. **Apply + verify** (see below).

## Before / after — the worker -> Luna hop (the live shape)

Primary worker already present:
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
```

ADD the fallback block — separate name, independent-capacity pay-per-token provider:
```yaml
  - model_name: flash-luna
    litellm_params:
      model: openrouter/openai/gpt-5.6-luna
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body:
        reasoning:
          effort: high
      timeout: 600
```

ADD the mapping under `router_settings` (only `flash` is remapped here):
```yaml
router_settings:
  fallbacks: [{"flash": ["flash-luna"]}]
```

`super-flash` -> `super-flash-luna` is wired the same way, with `reasoning.effort: none` on
the Luna hop (matching the driver's low-latency posture) instead of `high`.

For a plain OpenAI-compatible fallback instead (no `api_base`):
```yaml
  - model_name: <lane>-fallback
    litellm_params:
      model: openai/<id>
      api_key: os.environ/<VAR>
      timeout: 600
```

## If you decide the driver needs a chain (overriding house policy)

House policy is that `heavy` has no fallback — a driver that silently reroutes mid-session
onto a different model is worse than one that errors loudly. If you've decided to override
that for a specific reason, the mechanics are identical to the worker case: a new
`model_name` per hop, independent capacity (rule 1 above — this is exactly where the Claude
subscription-token trap tends to bite, since a Claude model is the obvious first thing
people reach for as a `heavy` fallback), ordered fast-first in `fallbacks`, e.g.:

```yaml
router_settings:
  fallbacks: [{"heavy": ["heavy-fallback", "heavy-fallback-2"]}]
```

Document WHY you overrode the no-chain policy next to the change — the next person editing
this file needs to know it was a deliberate call, not a lapse.

## The ChatGPT-subscription lane (`heavy`, litellm's `chatgpt/` provider)

`heavy` already runs on a ChatGPT Plus/Pro subscription via litellm's native `chatgpt/`
provider (it talks to `chatgpt.com/backend-api/codex`, the Responses API Codex uses) — not
an API key. These traps apply whether you're diagnosing `heavy` directly or pointing
another hop at the same subscription (e.g. the driver-chain case above). Verified still
true on litellm 1.99.0 (2026-09-04):

1. **Log in once (device code).** litellm ships a ChatGPT authenticator that writes a token
   to `~/.config/litellm/chatgpt/auth.json`. Auth is THAT file, NOT `OPENAI_API_KEY` (which
   is pay-per-token OpenAI billing on a different bucket). The deployment's `api_key` is a
   required-but-ignored placeholder.
2. **Deployment shape** — the `responses/` segment scopes litellm's chat->responses bridge:
   ```yaml
     - model_name: heavy
       litellm_params:
         model: chatgpt/responses/gpt-5.6-sol   # gpt-5.6-sol / gpt-5.4 work; codex-* ids are rejected
         api_key: "chatgpt-oauth"               # placeholder; provider reads auth.json
         reasoning_effort: xhigh                # the TOP value that reaches the backend
         timeout: 600
   ```
   **`max` does NOT work here (verified litellm 1.99.0, 2026-09-04):** the chat->responses
   bridge's `_map_reasoning_effort` knows only `none/minimal/low/medium/high/xhigh` and
   returns `None` for anything else, so `max` (or a typo) is silently dropped and the lane
   runs at the backend default. Nothing errors and nothing logs it.
3. **STREAMING-ONLY, still true on litellm 1.99.0 (2026-09-04).** The Codex backend always
   streams and litellm only reassembles the reply on the STREAMING path. A STREAMED
   `/v1/chat/completions` returns 200 (opencode + every agentic client streams — the real
   path). A NON-STREAMED call hits a litellm bridge bug (`ChatgptException - Unknown items
   in responses API response: []`, HTTP 500). That's benign in a fallback chain — a
   non-streaming caller that reaches this hop just rolls on to the next fallback — but on
   `heavy` (no chain) it's a hard failure for any non-streaming caller. Don't chase the 500
   as a config error.
4. **The token-refresh trap.** litellm trusts `expires_at` inside
   `~/.config/litellm/chatgpt/auth.json` and does NOT refresh on a server-side 401
   `token_expired` — a stale token just fails every call until something else refreshes it.
   Force a refresh through litellm's own Authenticator rather than hand-rolling one:
   `_refresh_tokens(...)` (refresh tokens ROTATE on each use, so the new one must be saved
   back), invoked with a GENERIC interpreter path such as `"$(uv tool dir)/litellm/bin/python"`
   — never a hardcoded `/Users/<name>/...` path, which breaks for anyone else running the
   same command.

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
ferry reload                        # config-only: re-reads litellm.yaml, leaves the GPU lanes alone (v1.24.0+)
ferry down && ferry up              # full restart — use when ferry reload isn't enough
ferry dash                          # then click "Test backends"
```
"Test backends" actively pings each backend and reports WHICH fallback hop served + latency
(the dashboard also renders each lane's topology: primary -> its `fallbacks` chain, and
probes all live lanes). Probe `heavy` (and any other `chatgpt/`-backed hop) with
`"stream": true` — the Codex backend is streaming-only (see above); a non-streamed probe
500s and tells you nothing about the lane's health.

A `rate_limit_error` from the primary is EXPECTED/NORMAL when the fallback is doing its job
— it is NOT an auth failure. Make sure the fallback's key is exported (shell or
`~/.config/ferry/secrets.env`); never commit real keys.

**To PROVE a chain actually fires** (rather than inferring it from a dashboard probe):
1. Temporarily set `general_settings.dangerously_allow_mock_testing_request_params: true`
   in `litellm.yaml` and `ferry reload`.
2. Send a normal request with `"mock_testing_fallbacks": true` in the body.
3. Read the response headers `x-litellm-model-id` and `x-litellm-attempted-fallbacks` — they
   show which deployment actually served the request and which hops were tried, on a
   loopback request (no real backend call needed).
4. Remove the flag and `ferry reload` again — the proxy 400s `mock_testing_fallbacks` while
   the flag is off, so leaving it set is itself a signal something didn't get cleaned up.
