# Worker-lane evaluation, Aug 2026: GLM 5.3 Flash vs Gemini 3.7 Flash

> Why the `flash` worker lane moved from Gemini 3.7 Flash (Google direct) to
> GLM 5.3 Flash (OpenRouter), what it costs, how fast each is, and the
> benchmarking traps that had to be solved to get honest numbers. Method is
> codified in the [`a-b-testing-models` skill](../.claude/skills/a-b-testing-models/SKILL.md)
> + [`scripts/bench-models.py`](../scripts/bench-models.py); reproduce with
> `python3 scripts/bench-models.py`.

## Background

The lane had been a multi-project Gemini key pool. That pattern — spreading
load across projects to multiply per-project quota — is circumvention under
[Google APIs ToS §2.d](https://developers.google.com/terms) and was dismantled
after an enforcement action (see the v1.8.0 changelog). The replacement had to
be both ToS-clean and cheaper at real volume, since the lane's whole job is
cheap high-throughput subagent fan-out.

## Cost at real volume (12 days of measured traffic)

Observed lane volume from the observability stack (litellm → VictoriaMetrics,
12-day retention): **128.7M input · 0.69M output · 117.0M cached input — a 91%
cache-hit rate**, peak day 103M input. Prices per 1M tokens:

| Surface | uncached in | cached in | out |
|---|---|---|---|
| Gemini 3.7 Flash, Google direct list | $0.75 | $0.075 | $3.75 |
| Gemini 3.7 Flash, OpenRouter | $0.375 | $0.0375 | $1.875 |
| GLM 5.3 Flash, OpenRouter (promo) | $0.075 | $0.015 | $0.25 |
| GLM 5.3 Flash, OpenRouter (normal) | $0.15 | $0.03 | $0.50 |

| Same 12-day volume on each | cost |
|---|---|
| Google direct | **$20.20** |
| OpenRouter Gemini | $10.11 |
| OpenRouter GLM flash (promo) | **$2.81** |
| OpenRouter GLM flash (normal) | $5.61 |

Notes:
- Google direct list for Gemini 3.7 Flash **doubles on 2027-01-01** (to
  $1.50 / $0.15 / $7.50), widening the gap to ~14×.
- litellm's own spend metric independently estimated ~$21 for the period —
  use it as a cross-check, never the only source.
- The cache-hit rate dominates: 91% of input tokens bill at the cached price.
  A comparison that ignores cache pricing answers a different question.

## Speed (streaming bench, temp 0, seed 0, 3 reps, medians)

| Surface | text-TTFT | e2e throughput | thinking policy |
|---|---|---|---|
| Gemini 3.7 Flash via OpenRouter | ~2.2s | ~256 tok/s decode / 4.5s per 600 tok | mandatory (OR rejects all disable spellings) |
| GLM 5.3 Flash via OpenRouter | ~2.4s | ~80 tok/s decode / 9.7s per 600 tok | mandatory |
| GLM 5.3 Flash direct (Z.ai coding plan) | **~0.9s** | ~54 tok/s decode / 4.3s per 184 tok | genuinely off (`thinking: {type: disabled}`) |
| Gemini 3.7 Flash direct (native API) | ~3.3s | unmeasurable (chunk-burst SSE) | `thinkingBudget: 0` **ignored** on longer prompts (732–980 thoughts) |

**Gemini 3.7 Flash decodes ~3× faster** (~256 vs ~80 tok/s via OpenRouter;
~2× on e2e throughput in the harness run: 129.6 vs 64.2 tok/s). GLM flash's
only latency win is TTFT with thinking truly off, which only the direct Z.ai
surface provides.

## The traps (why naive benchmarks lied)

1. **Reasoning tokens eat `max_tokens`** — a 20-token ping on a thinking model
   returns `content: null`, `finish_reason: "length"` and looks like a dead
   lane. Budget ≥600 and read `reasoning_tokens`.
2. **Streams omit `usage` by default** — `stream_options: {include_usage: true}`
   or every tok/s is zero.
3. **OpenRouter makes reasoning mandatory** for both models — every disable
   spelling 400s or is silently ignored. Thinking-off requires the direct API.
4. **Verify thoughts in the response** — Gemini honored `thinkingBudget: 0` on
   a one-liner and ignored it on a paragraph prompt.
5. **TTFT is ambiguous** on thinking models — record text-TTFT and
   reasoning-TTFT separately.
6. **Decode tok/s needs per-token SSE deltas** — Gemini's native SSE bursts
   multi-token chunks (measured "717 tok/s" once — an artifact); report e2e
   tok/s when deltas aren't per-token.
7. **Agentic lanes need a tool-call probe** — prose speed ≠ tool-call
   correctness; one forced `get_weather` call must round-trip before a swap.

## Decision

```
flash (OR GLM 5.3 Flash, $0.075/$0.25 promo)
  → flash-glm (Z.ai coding plan; 1/3 the credits of glm-5.3, ~0 off-peak; overflow-only — plan policy limits use to supported tools)
  → flash-gem (native Google key, single project)
  → flash-or  (OR Gemini)
  → orch      (last resort)
```

Routine volume rides the cheapest surface (~7× cheaper than the old lane);
Gemini's superior decode speed is retained as overflow capacity. The native
Google key now sees only overflow traffic, so its 3M TPM Tier-2 ceiling is
ample and a tier raise is optional, not load-bearing.
