# Roadmap (re-ranked 2026-09-27)

Re-ranking of the 37-issue queue in
[competitive-gap-sweep-2026-09-27.md](competitive-gap-sweep-2026-09-27.md),
filtered by the positioning (LAN appliance, zero-infra, strict named chains)
rather than by rival parity. Issue numbers refer to that queue.

## Tier 0 — hygiene (shipped in v1.38.0)

- [x] Release automation: `.github/workflows/release.yml` creates the Release
      on tag push; the 11 missing Releases (v1.29.4–v1.36.0) were backfilled.
      See [followup-tasks-release-automation.md](followup-tasks-release-automation.md).
- [x] ANSI colour gated on a tty (and `NO_COLOR`) across all `lib/` modules. See
      [followup-tasks-ansi-in-piped-output.md](followup-tasks-ansi-in-piped-output.md).
- [x] `ferry offer` naming and `.app` payload integrity. See
      [followup-tasks-offer-and-app-payloads.md](followup-tasks-offer-and-app-payloads.md).
- [x] Subcommand `--help` safety: `-h`/`--help` never runs a subcommand;
      arg-less commands reject stray arguments.

Still open from the follow-up docs:

- [ ] `Range` requests on the share server's `/file/` (offer doc, item 4).
- [ ] ANSI escapes outside `lib/` (`client-*.sh`, `host-*.sh`, `status.sh`,
      `observ/*`) are not tty-gated.
- [ ] Release follow-up tasks 2–3 (drift check, worktree prune).

## Tier 1 — strengthen the differentiators

- [x] **06 Per-client virtual keys with budgets.** Shipped in v1.39.0 as
      per-device keys with token budgets ([spec](superpowers/specs/2026-09-27-client-keys-design.md),
      [release](releases/v1.39.0.md)). USD budgets are deferred (spec open
      question 1). Unblocks 13 (spend rollups), 25 (transcripts) and 08 (key rotation).

Still open from 06:

- [ ] Pre-existing: the loopback-only gate on `/v1/ferry/reorder`, `/promote`
      and `/chains` treats `tailscale serve` clients as loopback (the enroll
      endpoint on a host with no master key has the same caveat).
- [ ] The device-key route allowlist (`device_key_route_allowed`) may need
      widening if a client uses a route outside it — watch the front log for
      `route_not_allowed`.
- [ ] Gemini-native generate (`/v1beta/models/…:generateContent`) and the
      websocket routes admit a device key without its lane, RPM or budget
      limits and without metering: `_key_admit` runs only on
      `INFERENCE_PATH_PREFIXES`. Either meter and limit them or drop them from
      the allowlist for limited keys.
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
