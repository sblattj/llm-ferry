# Per-device client keys — design

Status: APPROVED 2026-09-27; implemented in v1.39.0 (plan: `docs/superpowers/plans/2026-09-27-client-keys.md`).
Roadmap item: Tier 1, issue 06 of
[the gap sweep](../../competitive-gap-sweep-2026-09-27.md).

## Intent (agreed)

- **Who holds keys:** the operator's own devices. Not household members, not a
  team, not the public.
- **What they are for, in order:** (1) attribution — which device spent what;
  (2) revocation — cut off a lost or stale device without re-keying every
  other device; (3) optional limits — lanes, RPM, monthly token budget.
- **How a device gets one:** automatically at join. The `curl | zsh` client
  bootstrap enrolls with the host and stores a device key instead of the master
  key. `ferry keys add` covers manual cases. Existing clients keep working on
  the master key until they re-run bootstrap.
- **Chosen approach (A):** ferry-native keys enforced in the existing front-door
  ASGI middleware. Rejected: litellm virtual keys (needs Postgres — breaks the
  zero-infrastructure posture) and a separate auth proxy (an extra hop for
  nothing; the front door already sees every request).

Constraint discovered during design: the share server is unauthenticated and
v1.22.0 deliberately keeps the master key off it (`lib/ferry-share.zsh:128`).
Enrollment therefore cannot be "anyone on the LAN asks and receives a key"; it
is authenticated by the master key the bootstrap already requires.

## Non-goals (v1)

- USD budgets. v1 budgets count tokens. (See Open question 1.)
- One-time invite codes so a new device never sees the master key.
- Per-key CIDR allowlists, teams, budget windows other than calendar month.
- Any change to litellm's config or to the master-key mechanism itself.

## 1. Key format and storage

- **Key:** `fk-<slug>-<32 chars base32 from secrets.token_bytes(20)>`, where
  `<slug>` is the key name lowercased to `[a-z0-9-]`, max 24 chars. The `fk-`
  prefix lets the front door recognise a ferry client key without a lookup.
- **`~/.config/ferry/keys.json`**, mode 0600, written atomically (tempfile +
  `os.replace`), shape:

  ```json
  {"version": 1, "keys": [
    {"name": "mbp-work", "sha256": "<hex>", "created": "2026-09-27T20:00:00Z",
     "expires": null, "revoked": null,
     "lanes": null, "rpm": null, "budget_tokens": null}
  ]}
  ```

  Names are unique. The plaintext key is printed once at creation and never
  stored. `lanes: null` means all lanes; `rpm`/`budget_tokens: null` means
  unlimited. `revoked` holds a timestamp; revoked entries are kept for audit.
- **`~/.config/ferry/keys-usage.sqlite`** (stdlib `sqlite3`, WAL, 0600): two
  tables — `minute(name, minute_epoch, n)` for RPM and
  `month(name, yyyymm, tokens)` for budgets. A shared file, not in-memory
  counters, because the front door runs `FERRY_FRONT_WORKERS` (default 4)
  processes and per-process counters would under-count by that factor.
  Rows older than 2 minutes / 13 months are pruned opportunistically.

## 2. Request path (front door)

A new step at the top of `LaneCatalogueFilter.__call__`
(`front/ferry_front.py`), on every HTTP path, before the control-plane routes:

1. **Bearer == master key** (`LITELLM_MASTER_KEY`): unchanged behaviour. God
   key; bypasses every limit; identity resolution as today.
2. **Bearer starts with `fk-`:**
   - Hash and look up in `keys.json`, cached and re-read when the file's mtime
     changes — a revoke takes effect on the next request, no restart.
   - Unknown, revoked, or expired → the front door answers **401** itself.
   - Valid → identity = key name (overrides `X-Ferry-Client`, so fleet
     selection and events follow the key).
   - On inference paths only (`is_inference_path`): check `lanes` against the
     requested model after fleet resolution (**403**), then RPM (**429**), then
     month tokens ≥ `budget_tokens` (**429**). Error bodies are family-correct:
     OpenAI shape (`{"error": {"message", "type", "code"}}`) on OpenAI paths,
     Anthropic shape (`{"type": "error", "error": {"type", "message"}}`) on
     `/v1/messages`. 429s carry `Retry-After`.
   - Replace `Authorization` with `Bearer <master key>` when a master key is
     set (strip it when none is set), then forward. litellm is untouched.
3. **Anything else:** pass through exactly as today; litellm's own master-key
   check decides.

**Why the front door rejects `fk-` keys itself:** with no master key
configured, litellm accepts any bearer, so a revoked key would otherwise still
work. The prefix makes revocation hold in both modes.

**Fail-closed for client keys only.** The front door is fail-open for routing
and stays so. For an `fk-` bearer, an unreadable/corrupt `keys.json` or an
unopenable usage DB means **reject (503 for the DB, 401 for the key file)**,
because admitting a revoked key is the failure this feature exists to prevent.
The master key keeps working in every failure mode.

**Accounting.** RPM increments at admission. Tokens are added on response
completion from the usage the passive parser already extracts (input + output +
reasoning). A request admitted just under budget can overshoot by one request;
that is accepted and documented.

## 3. Enrollment and CLI

- **`POST /v1/ferry/keys/enroll`** on the front door, body `{"name": "<host>"}`,
  requires the master-key bearer (`_bearer_ok`). Mints a key named after the
  device (suffix `-2`, `-3` on collision unless `"replace": true`, which
  revokes the old one), returns `{"name", "key"}`. Loopback and LAN both
  allowed — the master key is the gate. With no master key configured, enroll
  is allowed only from loopback.
- **Client bootstrap** (`client-bootstrap.sh`): when a master key was supplied
  and the host answers enroll, store the device key as `api_key` in
  `~/.config/ferry/client.json` and do **not** persist `master_key`. If enroll
  fails (older host), keep today's behaviour. Everything that reads
  `CLIENT_MASTER_KEY` (`lib/ferry-core.zsh:294`) prefers `api_key`. Editor
  wiring and profiles get the device key.
- **Host CLI** — new module `lib/ferry-keys.zsh`, host-only, logic in a stdlib
  module `front/ferry_keys.py` shared with the front door:
  - `ferry keys add <name> [--expires YYYY-MM-DD] [--lanes a,b] [--rpm N] [--budget-tokens N]`
  - `ferry keys list` — name, created, expires, limits, this month's tokens,
    last-minute requests, status (active/expired/revoked). Never prints keys.
  - `ferry keys revoke <name>`
  - `ferry keys set <name> [same flags as add; "none" clears a limit]`
  - `--help` handled by the v1.38.0 dispatcher guard.

## 4. Attribution

- Events (`lib/ferry_events.py`): new field `key` — the key name, `"master"`
  for the master key, `""` when auth is off and no key was sent. Added to
  `_EMPTY` so every record carries it.
- `ferry dash`: per-key column (requests, tokens this month) in the traffic
  view; the live feed shows the key name.
- Metrics: a `key` label is **not** added to Prometheus series (cardinality is
  small here, but the dashboards are provisioned files and changing them is out
  of scope for v1).

## 5. Testing

Following the repo's existing style (unittest, real embedded Python, throwaway
`$HOME`):

- `front/ferry_keys.py` unit tests: mint/hash/lookup, expiry, revoke, mtime
  reload, atomic write + 0600, corrupt file → reject, slug rules.
- Front-door tests (extend `lib/ferry-front.test.py`): master passes untouched;
  valid `fk-` rewritten to master and forwarded; unknown/revoked/expired →
  401 in both auth-on and auth-off modes; lanes → 403; RPM and budget → 429
  with the correct body per path family; identity overrides `X-Ferry-Client`;
  corrupt key file rejects `fk-` but not master; multi-process RPM via two
  processes sharing one sqlite file.
- Enrollment: requires master bearer; collision suffixing; `replace`; no-master
  mode loopback-only.
- Bootstrap: stores `api_key`, drops `master_key`; falls back on an old host.
- Events: `key` present on every record; `"master"` vs name vs `""`.
- Each new test must fail against the pre-change `ferry`/front (control run).

## 6. Docs

README security section: device keys vs the shared master key, how to migrate
(re-run bootstrap on each device), how to revoke, and the limits' semantics
(token budgets per calendar month, one-request overshoot).

## Open questions for review

1. **USD budgets may be cheap after all.** Events already record
   `x-litellm-response-cost` (`lib/ferry_events.py` `record_from_headers`,
   `rec["cost"]`). A `budget_usd` field could sum that instead of tokens.
   Caveats: subscription lanes likely report 0, and cache-token pricing is
   issue 12. Keep v1 token-only, or add `--budget-usd` on the cost header now?
2. **Should the master key stop being stored on existing clients** when they
   next run `ferry update`, or only when they re-run bootstrap? (Draft: only
   bootstrap; `update` never touches credentials.)

**Resolved 2026-09-27:** (1) v1 is token-only (`budget_tokens`); USD is
deferred. (2) Only re-running bootstrap migrates a client; `ferry update`
never touches credentials. The plan also records eight corrections to this
draft — notably budgets count input + output only, since reasoning is a
subset of output.
