# Roadmap (re-ranked 2026-09-27)

Re-ranking of the 37-issue queue in
[competitive-gap-sweep-2026-09-27.md](competitive-gap-sweep-2026-09-27.md),
filtered by the positioning (LAN appliance, zero-infra, strict named chains)
rather than by rival parity. Issue numbers refer to that queue.

## Tier 0 — hygiene (in progress)

- [ ] Release automation: tag push creates the GitHub Release; backfill the
      missing Releases (v1.30.x–v1.34.0, v1.36.0). See
      [followup-tasks-release-automation.md](followup-tasks-release-automation.md).
- [ ] ANSI colour gated on a tty across all `lib/` modules. See
      [followup-tasks-ansi-in-piped-output.md](followup-tasks-ansi-in-piped-output.md).
- [x] `ferry offer` naming and `.app` payload integrity. See
      [followup-tasks-offer-and-app-payloads.md](followup-tasks-offer-and-app-payloads.md).
- [ ] Subcommand `--help` safety: `ferry reload --help` currently reloads the
      live front door because `reload`/`install`/`status`/`share`/`log` drop
      their arguments in `lib/ferry-main.zsh`.

## Tier 1 — strengthen the differentiators

- [ ] **06 Per-client virtual keys with budgets.** Every multi-user LAN claim
      depends on it, and it is the prerequisite for 13 (spend rollups),
      25 (transcripts) and 08 (key rotation). Build first.
- [ ] **34 Ollama-native API** (`/api/tags`, `/api/chat`). Many tools speak
      only Ollama; also answers the "ollama converging from below" threat.
- [ ] **01 Session affinity.** Pin a conversation to one deployment — matters
      most for local MLX lanes (KV reuse) and pooled subscriptions.
- [ ] **23 First-class `/v1/responses`.** Already served; needs a wrapper,
      docs and streaming tests. Cheap; covers Codex clients.
- [ ] **30 `ferry export` / `ferry import` for litellm.yaml.** Cheap insurance
      against agentgateway's litellm.yaml importer.
- [ ] **21 Audio lanes, transcription only, on local MLX-whisper.** Fits
      "the Mac's GPU serves the LAN" exactly.

## Tier 2 — later

04 retry/timeout knobs, 07 credentials at rest, 29 continuous probes,
27 headless/Linux, 37 per-lane reasoning policy, 12 cache-token pricing.

- [ ] **Jev failure-classifier audit** (branch `jev-audit-tier`, `f39a5ec`,
      based on pre-rewrite `db4ebad` — rebase onto `origin/main` first).
      Merge the shadow tier after launch, not before: it adds third-party
      egress, which cuts against "keys never leave the host". Higher-value use:
      run Jev offline over logged `unknown` failures to mine new keyword rules
      for `lib/ferry_live.py` `classify()` (eval: 105/105 unknown-gap fill), so
      the deterministic table stays in charge. Add a pluggable local-lane
      backend (host flash/schematron) and eval it on the same 340-call set.
      Stays shadow-only: adversarial steer breached 21.4% against a 5% bar.

## Deliberately skipped

- 02 auto tier routing — contradicts strict named chains; silent quality risk.
- 11 response cache — agentic traffic rarely repeats exactly.
- 14 compression, 18 A2A, 19 per-lane memory.
- 15/16 MCP and web tools at the gateway — clients already host these; parity,
  not differentiation. Revisit only on user demand.

Distribution (2 stars, absent from curated lists) may matter more than any
single feature.
