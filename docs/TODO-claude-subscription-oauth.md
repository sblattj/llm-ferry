# TODO: Claude Pro/Max subscription OAuth (Path A)

**Status:** design frozen 2026-09-13; implementation of the modules below is
NOT wired into the build yet. This doc is the end-to-end wiring guide for the
integration step. The route-config half is already landed in
`litellm-route-example.yaml` (the "Claude subscription (OAuth)" block).

**Goal:** let ferry serve Claude opus/sonnet/haiku lanes that draw on a Claude
Pro/Max **subscription** (like the existing ChatGPT-subscription
`domestic.heavy` lane) instead of metered Anthropic API billing, with a
metered OpenRouter fallback hop when the subscription bucket is exhausted or
the token refresh fails.

---

## 1. OAuth constants (reverse-engineered, Claude Code client identity)

| Constant | Value |
|---|---|
| Authorize URL | `https://claude.ai/oauth/authorize` |
| Token URL | `https://console.anthropic.com/v1/oauth/token` |
| Client ID | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| Redirect URI | `http://localhost:54545/callback` |
| Scope | `org:create_api_key user:profile user:inference` |
| PKCE | S256 (code_verifier / code_challenge per RFC 7636) |

Flow: `ferry auth-claude` opens the authorize URL with PKCE, listens on
localhost:54545 for the callback `code`, exchanges it at the token URL, and
stores the result at `~/.config/ferry/claude/auth.json` (access token,
refresh token, `expires_at`; refresh tokens ROTATE — every refresh
invalidates the previous refresh token).

## 2. Cloaking requirement (why this is not a plain Anthropic client)

The subscription inference endpoint only serves requests that look like they
came from the **Claude Code client itself**: Claude Code-identical headers
(user-agent, client version, beta headers) plus the billing fingerprint the
client presents. A stock Anthropic SDK request is rejected or demoted. The
exact header set is **reverse-engineered** from the Claude Code client and is
**ToS-gray**: it impersonates the official client to use a consumer plan for
API-shaped traffic. Consequences:

- It can break without notice whenever Anthropic ships a new Claude Code
  version (header/version drift) or adds detection.
- The constants live in ONE place — `front/ferry_claude_provider.py` — so a
  drift fix is a one-file patch.
- This lane is a convenience/cost lane, never a load-bearing dependency: every
  lane has a metered fallback hop, and the lanes are optional in the route
  config.

## 3. Architecture

Five pieces (plus the route config, already landed):

1. **`front/ferry_claude_oauth.py` — token engine.** Owns
   `~/.config/ferry/claude/auth.json`: the device/localhost-loopback login
   flow with PKCE S256, proactive refresh (refresh when `now > expires_at -
   margin`), forced refresh on upstream 401, and single-writer (lockfile)
   token-file writes so refresh-token rotation cannot race a concurrent
   refresh into consuming the same rotating refresh token twice — a lost
   refresh token means a full re-login, so the lock is mandatory. This closes
   the litellm trap documented in the route config: stock litellm trusts
   `expires_at` and never refreshes on 401 `token_expired`.
2. **`front/ferry_claude_provider.py` — cloaking + litellm seam.** Registers
   a custom litellm provider for the `claude-oauth/` model prefix, swaps the
   `"claude-oauth"` placeholder `api_key` for the live token from (1), stamps
   the Claude Code-identical headers/billing fingerprint on every request,
   and maps 401 `token_expired` to a forced-refresh-and-retry-once before the
   error escapes to the router (which then fires the OpenRouter fallback hop).
3. **`lib/ferry-auth-claude.zsh` — `ferry auth-claude` CLI.** Thin zsh
   wrapper: `login` (run the flow), `status` (token/expiry), `refresh`
   (forced), `logout` (delete auth.json). A NEW build module — not to be
   confused with the existing `lib/ferry-claude.zsh`, which points Claude
   Code at ferry as a *client*.
4. **`build_app()` monkeypatch hook in `front/ferry_front.py`.** The litellm
   proxy app never imports our provider on its own; `build_app()` (currently
   at front/ferry_front.py:1908) must call the provider registration from (2)
   before returning the app, so every `ferry up`/`ferry reload` loads the
   seam.
5. **Route config — `litellm-route-example.yaml`.** LANDED: the
   "Claude subscription (OAuth)" block in `model_list` (lanes `claude-opus`,
   `claude-sonnet`, `claude-haiku` on `claude-oauth/<model>` with the
   `api_key: "claude-oauth"` placeholder; metered hops `claude-opus-or`,
   `claude-sonnet-or`, `claude-haiku-or` via OpenRouter with
   `provider.sort: throughput`) and the three matching single-hop
   `router_settings.fallbacks` entries. Lanes are unprefixed literal names
   (like `schematron`) so they pass the fleet resolver untouched.

## 4. Integration checklist (what the orchestrator must wire)

- [ ] `build.zsh` — add `auth-claude` to `MODULES` (build.zsh:19), keeping
      module-file-name conventions (`lib/ferry-auth-claude.zsh`).
- [ ] `front/ferry_front.py` — in `build_app()` (ferry_front.py:1908), call
      the provider registration from `front/ferry_claude_provider.py` (guard
      with try/except-log so a missing/cloak-broken provider degrades to the
      fallback hop instead of killing the whole front door).
- [ ] `lib/ferry-main.zsh` — add the `auth-claude)` dispatch case next to the
      existing `claude)` case (ferry-main.zsh:36), forwarding to
      `cmd_auth_claude`.
- [ ] Regenerate the CLI: `zsh build.zsh` (never hand-edit `ferry`; the sync
      guard flags drift — commit `lib/` AND the regenerated `ferry`).
- [ ] New tests: `lib/ferry-auth-claude.test.zsh` (CLI dispatch) and a
      provider/oauth unit test alongside `front/` following the
      `*.test.py` stdlib-unittest convention.
- [ ] Run the full suite from the repo root:
      ```bash
      for suite in lib/*.test.py observ/*.test.py; do python3 "$suite" || exit 1; done
      node lib/ferry-dashui.test.mjs
      zsh build.zsh --check
      ```
- [ ] End-to-end verify on a live host: `ferry auth-claude login` → probe
      `claude-sonnet` (streaming) through the front door → `curl
      :8090/metrics` shows per-deployment series for `claude-oauth-*` ids →
      revoke/expire the token and confirm the hop fires (served by
      `or-claude-sonnet-fb`).

## 5. Risks / unknowns

- **ToS exposure.** Cloaking a consumer subscription is against the spirit
  (likely the letter) of Anthropic ToS; account-level sanctions are possible.
  Keep the lane optional and metered-fallback-protected; never route the
  driver lane onto it by default.
- **Cloaking drift.** Any Claude Code client-version bump can change the
  expected headers/billing fingerprint; symptom is a sudden 401/403 wall on
  all three lanes with a token the refresher swears is fresh. Fix path:
  re-diff headers in `front/ferry_claude_provider.py` only.
- **Refresh-token rotation races.** Rotating refresh tokens mean two
  concurrent refreshes can invalidate each other's token (second use = dead).
  The token engine MUST serialize refreshes (file lock) and treat a rejected
  refresh token as "re-login required", surfaced by `ferry auth-claude
  status`, never silently retried in a loop.
- **Model-name drift.** `claude-oauth/claude-opus-4.5` (etc.) strings must
  match what the provider seam accepts; rename in one place per the claiming
  rule when Anthropic version-suffixes change.
