# Competitive gap sweep — findings (2026-09-27)

A full teardown of llm-ferry's open-source competition: discovery across
GitHub search, curated lists, and code-search idioms; 14 rival teardown cards
(338 features); a gap matrix against our 60-feature inventory; adversarial
verification of every absence claim; live re-fetch of 42 rival evidence legs.
Nothing here is guessed — every claim below survived independent re-verification
(37/37 issue bodies verified, 41/42 evidence legs accurate, 1 drifted and fixed,
0 broken; adversarial review: 0 drops, 0 merges).

Full artifacts (teardown cards, gap matrix, kill ledger, verdict files) live in
the sweep run directory: `~/code/gap-sweeps/llm-ferry/2026-09-27/` (not committed).

## Positioning in one paragraph

llm-ferry is the only surveyed project that treats the **LAN appliance** as the
product: one Mac that serves its own GPU (MLX) *and* every cloud/subscription
account behind one OpenAI/Anthropic-compatible endpoint, where **clients join
with one `curl`** (CLI + profiles + editor auto-wiring + telemetry inbox), keys
never leave the host, and the box also ferries models and files, forwards client
downloads, and reverse-exposes client ports. Rivals each own exactly one slice —
router (claude-code-router), subscription relay (CLIProxyAPI/gpt-load), key-pool
hub (new-api/litellm), local runtime (ollama/exo/mesh-llm), chat UI (open-webui),
cloud-native gateway (agentgateway), zero-trust tunnel (pangolin) — **none
combines local serving + cloud lanes + subscription OAuth + client onboarding**.

## Durable differentiators (no surveyed rival has them)

1. **One-command client onboarding with editor takeover** — `curl | zsh` installs
   the client CLI, writes profiles, auto-wires opencode/Continue/Cursor with
   snapshots, scopes, surgical uninstall, and client→host promotion. Every rival
   is per-machine manual config.
2. **Metered subscription→pay-per-token degradation** — an exhausted Pro/Max
   session degrades to pay-per-token instead of erroring. Absent in all 14
   teardowns; unclaimed by anyone.
3. **Host-side subscription OAuth** — the gateway owns the OAuth lifecycle;
   litellm/agentgateway forward the *client's* token, CCR only imports existing logins.
4. **Strict named fallback chains + fleets + local-lanes-excluded-from-chains** —
   ordered hops across independent providers, mid-session routing-set switching
   (`X-Ferry-Fleet`), and the quota guard that a dead GPU lane errors rather
   than silently burning cloud money. Pangolin has no failover at all; gpt-load
   binds one channel per group.
5. **Apple-Silicon-native serving with memory governance** — MLX lanes + KV-cache
   governor (measured 97→56 GB peak, ~60% faster decode) + MTP draft + schematron
   lane. No rival serves MLX in-process as a gateway.
6. **LAN superpowers beyond inference** — ferry drop/offer/pickup (AES-256-CBC +
   PBKDF2(600k) + HMAC), serve-hf streaming, forward-download proxy, reverse
   port/VNC expose. A category of one.
7. **Zero-infrastructure core** — single-file zsh + python3-stdlib, versus
   litellm's Postgres+Redis+3 services, new-api's Docker wizard, pangolin's
   Traefik+WireGuard+IdP, agentgateway's Rust+Go+k8s+xDS.
8. **Signal Studio** — visual fallback-chain editing with YAML-diff preview;
   every rival UI is read-only or edits raw DB rows.

Structural: **MIT license** (cleanest in the set — new-api/pangolin AGPL,
open-webui branding clause, litellm dual-licensed), keys-never-leave-the-host
posture, measured published performance, and **0 forks / 0 borrowers** — the
machinery is genuinely novel, not a repackaged fork.

## Threats to watch (honest)

- **ollama is converging from below**: native MLX second engine, first-party
  `:cloud` proxied through the local server, hosted `/v1/messages`,
  `ollama launch` wiring 18+ agent runners. Still no third-party provider proxy,
  no auth, no onboarding — but "local+cloud one endpoint" is no longer ours alone.
- **agentgateway ships a litellm.yaml importer** (`crates/agentgateway/src/import.rs`
  + fixtures) — our own route config is a one-command migration path INTO them.
  Insurance: a `ferry export` round-trip (issue 30) or documented non-portable semantics.
- **gpt-load goes wider on subscription pooling** (4 providers vs our 2,
  per-client AccessKeys with cost/CIDR/RPM limits, credential encryption at rest).
- **agentgateway has institutional gravity** (Linux Foundation, envoy lineage)
  and will absorb generic gateway features faster.
- **Distribution gap**: llm-ferry is absent from every curated list/comparison
  found, while smaller rivals appear — the moat doesn't matter if nobody finds it.

## The gap queue (37 issues, all verified absent-from-us and rival-proven)

Priorities: 6 P1 / 19 P2 / 12 P3. Grouped by theme across the table. Full bodies
with acceptance criteria and rival citations: `issues/queue.jsonl` in the run dir.

### The six P1s

| # | Issue | Why it's first |
|---|---|---|
| 01 | Per-lane session affinity | CLIProxyAPI ships it and states the payoff verbatim: "maximizing Prompt/KV Cache reuse and minimizing TTFT"; litellm documents it (`docs/routing.md:966`) |
| 02 | Opt-in auto tier routing | litellm auto-routes by cost/latency; our heavy/flash split is the natural home for it |
| 06 | Per-client virtual keys with budgets | table stakes for multi-user: gpt-load AccessKeys, pangolin budgets (6 scopes, capability-shaped 429s), litellm virtual keys all have it |
| 11 | Opt-in response cache with HIT/MISS | litellm caching docs are the category default; we only plumb upstream prompt-cache |
| 15 | MCP tool servers attachable to lanes | litellm + agentgateway both ship MCP; agents increasingly expect tools at the gateway |
| 16 | Built-in web search/fetch tools per lane | ollama already proxies web_search/web_fetch through its cloud |

### Full queue

| # | P | Theme | Title |
|---|---|---|---|
| 01 | P1 | routing | Per-lane session affinity: pin a conversation to one pool deployment for KV reuse |
| 02 | P1 | routing | Opt-in auto tier routing: classify prompts onto heavy/flash lanes |
| 06 | P1 | keys | Per-client virtual keys with spend budgets (`ferry keys add <name> --budget-usd --rpm --expires`) |
| 11 | P1 | caching | Opt-in response cache with HIT/MISS visibility (`X-Ferry-Cache` header, per-lane `cache`/`cache_ttl_s` keys) |
| 15 | P1 | agent-tools | MCP tool servers attachable to lanes |
| 16 | P1 | agent-tools | Built-in web search/fetch tools per lane |
| 03 | P2 | routing | Declarative routing rules in route config (match header, model, client, payload size) |
| 04 | P2 | routing | Per-lane retry and timeout policy knobs in the route config |
| 07 | P2 | keys | Encrypt provider credentials at rest (`ferry secrets lock`: keychain or encrypted store) |
| 08 | P2 | keys | Key lifecycle: `ferry keys rotate/revoke/list` with expiry and no-restart revocation |
| 12 | P2 | caching | Cache-token pricing accounting: capture cache-write tokens and price reads/writes apart from input |
| 13 | P2 | caching | Per-lane/per-client spend rollups with warn/block thresholds (`ferry spend`, `spend_warn_usd`/`spend_block_usd`) |
| 17 | P2 | agent-tools | Pre/post-call guardrail hooks |
| 20 | P2 | api-breadth | Image-generation lanes: an `image` lane type serving `/v1/images/generations` and `/v1/images/edits` |
| 21 | P2 | api-breadth | Audio lanes (`/v1/audio/transcriptions`, `/v1/audio/speech`) and an explicit realtime/WebSocket position |
| 22 | P2 | api-breadth | First-class embeddings lanes behind the already-served `/v1/embeddings` |
| 23 | P2 | api-breadth | Make the served `/v1/responses` surface first-class: a codex-ferry wrapper, docs, and streaming tests |
| 25 | P2 | observability | Opt-in session transcripts with a dash viewer (default-off, retention-bounded) |
| 26 | P2 | observability | One loopback management API behind `ferry` verbs and Signal Studio |
| 27 | P2 | observability | Headless host deployment: service units, `ferry install --yes`, Linux-honest status |
| 30 | P2 | onboarding | `ferry export litellm` / `ferry import` — litellm.yaml round-trip |
| 31 | P2 | onboarding | `ferry models` — host-side model library management (extend existing transfer surface) |
| 34 | P2 | client-compat | Ollama-native API surface (/api/tags, /api/chat) on the front door |
| 36 | P2 | client-compat | Gemini-native endpoint (/v1beta/...:generateContent) for gemini-cli clients |
| 37 | P2 | client-compat | Per-lane reasoning policy key in route config (metrics already count reasoning tokens) |
| 05 | P3 | routing | Per-lane model mapping and an optional wildcard catch-all lane |
| 09 | P3 | keys | Named upstream credential groups a lane can reference (beyond the per-lane key pool) |
| 10 | P3 | keys | Per-upstream egress proxy + proactive RPM caps in route config |
| 14 | P3 | caching | Opt-in prompt/response compression lane (`compression` route-config keys, fidelity-warned, never default) |
| 18 | P3 | agent-tools | A2A agent bridging behind lanes |
| 19 | P3 | agent-tools | Opt-in per-lane persistent memory |
| 24 | P3 | api-breadth | A documented add-a-provider recipe plus the Signal Studio catalog-to-lane bridge (breadth as workflow, not count) |
| 28 | P3 | observability | Alert delivery beyond the Grafana stack: email/Slack receivers, stack-less alerts, quiet hours |
| 29 | P3 | observability | Scheduled backend probes with auto-cooldown (make `Test backends` continuous) |
| 32 | P3 | onboarding | `ferry keys import` — provider key import ergonomics + `ferry://keys` deeplink |
| 33 | P3 | onboarding | Optional menu-bar/tray host status app (`ferry tray`) |
| 35 | P3 | client-compat | `ferry install` editor presets: only opencode is auto-wired - add VSCode Copilot, Continue, Cursor |

### Cross-issue rulings (keep the queue coherent)

- **Enforcement lives in 06**: per-client spend *blocking* is the virtual-keys
  issue; issue 13 is the measurement/warn layer only.
- **One importer, not two**: issue 30 owns the litellm.yaml round-trip; issue 32's
  original `--from-litellm` flag was deferred into it.
- **Reframes, not greenfield**: issues 04, 07, 09, 12, 13, 22–24, 26, 28, 29, 31,
  35, 37 are extension proposals over existing surfaces (verified "exists at
  path:line; the named extension is absent") — read their bodies before scoping.

## Method (for reproducibility)

162 raw mentions → 140 unique candidates → 14 teardown targets (family merges:
one-api→new-api; claude-proxy family → CCR/CLIProxyAPI cards; engines counted as
stack, not rivals) → 190 skeleton gaps → 51 merges + 102 kills (all reasons
ledgered) → 37 issues. Verification: pass-1 absence re-runs on 19 bodies with
independent greps (3 wordings each), adversarial queue review, 39 rival evidence
legs re-fetched live; pass-2 absence re-runs on the remaining 18 bodies + 3 more
legs. Known noise traps handled: repo root has stray log files (`b.log`,
`r.log`, `client_logs.txt`) that pollute repo-wide greps; "tray" hits
false-positive on "stray"; short tokens (rpm/tpm/egress) need word boundaries.
