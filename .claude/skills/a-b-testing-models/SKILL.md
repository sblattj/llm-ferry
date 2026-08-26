---
name: a-b-testing-models
description: Use when comparing two models, providers, or ferry lanes on speed, cost, or behavior before switching a lane — e.g. "is X faster than Y", "compare GLM vs Gemini", "what is this lane costing", "benchmark the flash lane". Covers the thinking-token traps that make naive benchmarks lie (reasoning eats max_tokens, mandatory-reasoning surfaces, ignored thinking budgets, chunk-burst decode rates), the streaming harness at scripts/bench-models.py, real-traffic A/B from the observability stack, and the price x volume cost model.
---

# A/B testing models for a ferry lane

Deciding a lane swap (model, provider, or surface) needs three independent
answers: **speed** (synthetic bench), **cost** (real token volume x price), and
**behavior** (thinking policy, tool calls). Naive pings lie for all three on
modern reasoning models. This skill is the checklist that doesn't.

## The traps (each one bit during the 2026-08-26 GLM-vs-Gemini comparison)

1. **Reasoning tokens eat `max_tokens`.** A `max_tokens: 20` ping on a thinking
   model burns the whole budget on thoughts: `finish_reason: "length"`,
   `content: null` — looks like a dead lane, isn't. Any probe of a thinking
   model needs **>= 600 tokens** of budget, and you must read
   `completion_tokens_details.reasoning_tokens` to know what you measured.
2. **Streams omit `usage` by default.** Without
   `"stream_options": {"include_usage": true}` every streamed benchmark reports
   zero tokens and tok/s of 0. (Non-streaming calls always carry usage.)
3. **Some surfaces make reasoning MANDATORY.** OpenRouter rejects every disable
   spelling for these models (`reasoning.enabled: false`,
   `reasoning.effort: "none"` -> 400 "Reasoning is mandatory for this endpoint");
   a top-level `effort: "none"` is *accepted but ignored* — the model still
   thinks. The direct APIs differ: Z.ai takes `thinking: {"type": "disabled"}`
   (works, verified); Gemini takes `thinkingConfig.thinkingBudget: 0` — which is
   **honored inconsistently** (0 thoughts on a one-liner, 732-980 thoughts on a
   paragraph prompt despite budget 0).
4. **Verify thoughts in the RESPONSE, never trust the request knob.** Check
   `usageMetadata.thoughtsTokenCount` (Gemini) / `reasoning_tokens` (OpenAI-ish)
   on every run. A "thinking disabled" arm that thought 900 tokens is not the
   arm you configured.
5. **TTFT is ambiguous on thinking models.** Time-to-first-delta is
   time-to-first-*thought* when thinking is on. Record text-TTFT and
   reasoning-TTFT separately; they answer different questions (worker latency =
   text-TTFT + decode; user-perceived first sign of life = reasoning-TTFT).
6. **Decode tok/s from SSE chunk timing requires per-token deltas.** Gemini's
   native SSE bursts multi-token chunks (a 181-token answer measured "717
   tok/s" — a chunk-burst artifact). When deltas aren't per-token, report
   **e2e tok/s** (tokens / total wall time) instead and say so.
7. **Compare the config you will actually run.** Thinking-on-via-OpenRouter and
   thinking-off-direct are different products; a bench of one doesn't transfer
   to the other. Bench the exact surface the lane will use (through the ferry
   lane name when it exists).
8. **Pin the generation.** `temperature: 0` + `seed: 0`, >= 3 reps, interleave
   arms (round-robin, alternate order per round), report **medians**.
9. **Agentic lanes need a tool-call probe too.** Speed on prose != tool-call
   correctness. One `get_weather`-style forced tool call must round-trip
   (name + JSON args) before a lane swap.
10. **OpenRouter `/api/v1/stats` returns HTML now** — do not script against it.
    `/api/v1/models` is still JSON and is the pricing source of truth
    (`pricing.prompt` / `pricing.completion` / `pricing.input_cache_read`, USD
    per token — multiply by 1e6 for per-M).

## Speed: the harness

`scripts/bench-models.py` (stdlib only) implements all of the above. Arms are
OpenAI-compatible endpoints (a ferry lane, OpenRouter direct, Z.ai direct) or
Gemini native SSE. It prints per-rep lines and per-arm medians for text-TTFT,
reasoning-TTFT, e2e, text/reasoning token split, and decode/e2e tok/s.

```bash
python3 scripts/bench-models.py            # default arms: the flash lane's surfaces
python3 scripts/bench-models.py --rounds 5 # more reps
```

Read its ARMS dict before relying on it — edit arms to the two configurations
being compared (model ids, api bases, thinking knobs, keys from
`~/.config/ferry/secrets.env`). Keys are read from the environment, never
hardcoded.

## Cost: real volume x price, not list-price intuition

1. Pull the lane's actual token history from VictoriaMetrics (`:8429`, 12-day
   retention) — real cache-hit rates dominate flash-lane cost:

```bash
for m in input output input_cached; do
  curl -s http://127.0.0.1:8429/api/v1/query \
    --data-urlencode "query=sum by (model) (increase(litellm_${m}_tokens_metric_total[12d]))" \
  | python3 -c "import json,sys; [print(r['metric']['model'], f\"{float(r['value'][1]):,.0f}\") for r in json.load(sys.stdin)['data']['result']]"
done
```

2. Split into uncached-input, cached-input, output. Multiply each by the
   provider's per-M price (OpenRouter `/api/v1/models`; Gemini list at
   ai.google.dev/gemini-api/docs/pricing — note OpenRouter's Gemini price is
   often HALF Google direct list, and Google's list DOUBLES at announced dates,
   e.g. 2027-01-01 for 3.7 Flash).
3. Cross-check against litellm's own tracker:
   `increase(litellm_spend_metric_total[12d])` (it uses its price table; a
   large divergence means a price table is stale — investigate, don't average).
4. For subscription surfaces (Z.ai coding plan), price in **credits**:
   per-model multipliers differ ~3x within one plan (GLM-5.3 is
   6.9/1.7/24 per 1M in/cached/out; GLM-5.3-Flash exactly 1/3: 2.3/0.56/8;
   off-peak Mon-Fri 14:00-18:00 SGT = 50% credits). Weekly allowance (Lite 10k /
   Pro 60k / Max 140k) is the budget, not dollars.

## Speed: real-traffic A/B when history exists

Synthetic benches miss queueing, cache state, and fan-out concurrency. The
observability stack already recorded 12 days of live traffic — use it before
(re)running anything synthetic:

```bash
curl -s http://127.0.0.1:8429/api/v1/query \
  --data-urlencode "query=sum by (model) (litellm_request_total_latency_metric_sum) / sum by (model) (litellm_request_total_latency_metric_count)"
```

Same for `litellm_llm_api_time_to_first_token_metric_*` (TTFT). If the old lane
has history, its real numbers beat a fresh synthetic run of it.

## Recording the verdict

Write the comparison INTO the lane's comment block in
`~/.config/ferry/litellm.yaml` (see the `flash` header for the format: date,
price basis, measured tok/s, why the winner won) so the next session inherits
the decision instead of re-deriving it. If the swap changes committed guidance
(example yaml / README / skills), bump VERSION and say what changed.

## Worked example (2026-08-26, flash lane, 3 reps each, temp 0)

| Surface | Thinking | text-TTFT | decode | e2e |
|---|---|---|---|---|
| Gemini 3.7 Flash via OpenRouter | mandatory | ~2.2s | ~256 tok/s | 4.5s / 600 tok |
| GLM 5.3 Flash via OpenRouter | mandatory | ~2.4s | ~80 tok/s | 9.7s / 600 tok |
| GLM 5.3 Flash direct (Z.ai coding, disabled) | off (0 thoughts) | ~0.9s | ~54 tok/s | 4.3s / 184 tok |
| Gemini 3.7 Flash direct (budget 0) | **budget ignored** (732-980 thoughts) | ~3.3s (text) | chunk-burst, unmeasurable | 3.6s / 181 tok |

Verdict recorded: Gemini is ~3x faster at decode; GLM flash is ~7x cheaper at
observed volume ($2.81 vs $20.20 / 12d). Lane went to GLM on cost, with Gemini
as fallback hops — and thinking stays ON in practice everywhere, because the
mandatory-reasoning surfaces are the ones the lane actually uses.
