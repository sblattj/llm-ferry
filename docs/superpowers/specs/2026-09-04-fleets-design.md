# Fleets: concurrent routing sets behind the same lane names

**Date:** 2026-09-04
**Status:** approved design, awaiting implementation plan
**Release:** the next minor (`VERSION` is already ahead of the last tagged commit; pick the number at release time with `claiming-names-and-ports`)

## 1. The problem

A ferry client binds to lane NAMES (`heavy`, `flash`, `super-flash`, `local-orch`, `local-sub`) and the host decides which model sits behind each. Today there is exactly one answer per lane, in one file, `~/.config/ferry/litellm.yaml`. Switching the whole lineup from GPT-5.6 Sol + Gemini 3.8 Flash to Kimi K3 + GLM means editing every deployment and reloading the front door, and while it is in effect it is in effect for everyone.

The ask: several complete routing setups, each covering the same lane roles with its own primaries and fallback chains, loaded at once, selectable per client, and switchable at any moment for a running session without a reload. The host machine's own opencode and Claude Code sessions are just another client in this respect.

## 2. Vocabulary

- **Lane** — a role name a client sends as `model`: `heavy`, `flash`, `super-flash` (cloud) and `local-orch`, `local-sub` (host GPU). Unchanged.
- **Fleet** — a named routing set that defines a deployment (and its fallback hops) for each cloud lane. Initial fleets: `domestic` (the current lineup) and `international` (Kimi K3 driver, GLM hops).
- **Fleet lane** — a real litellm `model_name` of the form `<fleet>.<lane>`, e.g. `domestic.heavy`, `international.flash-glm`.
- **Selection** — which fleet a bare lane name resolves to for a given caller.

The word *profile* keeps its existing client-side meaning (the `opencode-cloud` / `opencode-super` / `opencode-local` lane pairs). A fleet is orthogonal to a profile: any profile can run on any fleet.

## 3. Fleets in the routing file

The routing file stays one hand-commented `~/.config/ferry/litellm.yaml`. A fleet is a prefix on the cloud lane names.

```yaml
model_list:
  # ── domestic ──────────────────────────────────────────────
  - model_name: domestic.heavy          # was: heavy
  - model_name: domestic.flash          # was: flash
  - model_name: domestic.super-flash    # was: super-flash
  - model_name: domestic.flash-terra    # was: flash-terra (hop, gpt-5.6-terra)
  - model_name: domestic.super-flash-luna  # was: super-flash-luna (hop)
  # ── international ─────────────────────────────────────────
  - model_name: international.heavy         # anthropic/k3 (Kimi K3)
  - model_name: international.heavy-glm     # zai/glm-5.3 (hop)
  - model_name: international.flash         # zai/glm-5.3-flash coding plan
  - model_name: international.flash-or      # openrouter/~z-ai/glm-flash-latest (hop)
  - model_name: international.super-flash   # zai/glm-5.3-flash, reasoning floor
  - model_name: international.super-flash-or  # openrouter/~z-ai/glm-flash-latest (hop)
  # ── shared GPU lanes, no prefix ───────────────────────────
  - model_name: local-orch
  - model_name: local-sub

router_settings:
  fallbacks:
    - {"domestic.flash": ["domestic.flash-terra"]}
    - {"domestic.super-flash": ["domestic.super-flash-luna"]}
    - {"international.heavy": ["international.heavy-glm"]}
    - {"international.flash": ["international.flash-or"]}
    - {"international.super-flash": ["international.super-flash-or"]}
```

Rules:

- **A fleet name is discovered, not declared.** The set of fleets is the set of distinct prefixes before the first `.` across `model_list` names that contain a `.`. No registry, no extra file to keep in sync. A fleet should define every cloud lane (`heavy`, `flash`, `super-flash`); the front door logs the gap at startup and answers 400 for that lane in that fleet, so a typo degrades one lane rather than taking the front door down.
- **The primary for a lane is `<fleet>.<lane>`; hops are `<fleet>.<lane>-<tag>`.** Chains never cross a fleet, and `ferry-dash`'s chain validator enforces that (a hop from another fleet is rejected with a readable reason).
- **Local lanes are unprefixed and shared.** They have no fleet variant and no fallback, exactly as today.
- **The legacy `orch` and `orchestrator` deployments are deleted.** The resolver maps both names to the fleet's `heavy`, so a session pinned to either keeps working with one fewer copy of the driver block.
- **`model_info.public: true`** keeps its meaning per fleet lane. The catalogue synthesizes bare names from it (section 4).
- **Secrets.** Each fleet's deployments name their own `os.environ/…` keys as today. `_ferry_warn_missing_keys` gains nothing new; a fleet whose key is missing fails at request time like any lane does now, and `ferry fleet ls` marks a fleet whose keys are unset.
- **Every existing lane name of the form `<fleet>.<lane>` is a new claim in the litellm namespace.** litellm treats `.` in `model_name` as an ordinary character (it already serves names like `gpt-3.5-turbo`); the plan's first task verifies fallback lookup with a dotted name under the production loader before anything else is built on it.

## 4. Resolution in the front door

`front/ferry_front.py` already sits in front of litellm as ASGI middleware, buffers request bodies for its control plane, and rewrites the catalogue body. It gains one more job: on every inference path (the existing `INFERENCE_PATH_PREFIXES` allowlist) it reads the JSON body and rewrites the top-level `model` field.

**Precedence, first match wins:**

1. `model` already carries a known fleet prefix (`international.heavy`): pass through untouched.
2. `model` is a local lane (`local-orch`, `local-sub`): pass through untouched.
3. Request header `X-Ferry-Fleet: <fleet>` (non-empty): use that fleet.
4. The caller's sticky selection from the state file, keyed by caller identity.
5. The host-wide default fleet from the state file.

`orch` and `orchestrator` are treated as `heavy` before step 3.

**Caller identity**, first match wins:

1. Request header `X-Ferry-Client: <name>` (non-empty).
2. `host`, when the ASGI peer is loopback (the existing `_is_loopback_client`).
3. The peer IP as a string.

A headerless client reaching the host through `tailscale serve` arrives from loopback and is therefore treated as `host`. Regenerated client configs carry the identity header, so this only affects a client that has not re-run bootstrap or reset since the release; it is documented in the README, not worked around.

**State file** `~/.config/ferry/fleets.json`:

```json
{"default": "domestic", "clients": {"host": "international", "stephens-laptop": "domestic"}}
```

The front door runs four uvicorn workers, so the file is the truth, not process memory. Each worker keeps the parsed document plus the file's mtime and re-reads on a per-request `os.stat` when the mtime changes. A missing file means `{"default": <first fleet in file order>, "clients": {}}` and is written on first mutation. An unparsable file is a startup error with the path in the message, never a silent fall-through.

**Errors are loud.** A fleet named by the header or by a stale sticky selection that no longer exists in the yaml gets HTTP 400 with `{"error": {"message": "unknown fleet 'x'; fleets: domestic, international", "type": "ferry_fleet"}}`. Nothing ever falls through to the default on a bad name. A request whose body is not JSON, or has no `model`, passes through untouched for litellm to answer as it does today.

**Body rewrite mechanics.** The body is fully buffered (chat bodies are not streamed uploads; a 500 KB compaction body parses in single-digit milliseconds). The rewrite is `json.loads`, replace, `json.dumps` with the same separators, and the ASGI `content-length` header is replaced to match. The event tap records the resolved fleet lane, so the dash's live panel and the observability stack see `international.heavy`, which is what metrics should group by. Response bodies are not touched: the `model` a client sees back is whatever litellm returns today.

**Catalogue synthesis.** `filter_catalogue` keeps every public fleet lane and additionally emits one bare entry per cloud lane that is public in the caller's resolved fleet. So a client on `international` lists `heavy`, `flash`, `super-flash`, `local-orch`, `local-sub`, `domestic.heavy`, `domestic.flash`, `domestic.super-flash`, `international.heavy`, …. `ferry opencode`'s catalogue check, `ferry status`, and host-reset's verifier all look for bare names and keep passing unchanged.

## 5. Control plane and the `ferry fleet` command

Two new routes answered by the middleware before litellm sees them, alongside the existing `/v1/ferry/*` routes.

**`GET /v1/ferry/fleet`** — any peer, bearer required when a master key is configured. Returns:

```json
{"you": "stephens-laptop", "fleet": "domestic", "default": "domestic",
 "fleets": {"domestic": {"heavy": "chatgpt/responses/gpt-5.6-sol", "flash": "openrouter/~google/gemini-flash-latest", "super-flash": "…"},
            "international": {"heavy": "anthropic/k3", "flash": "zai/glm-5.3-flash", "super-flash": "…"}},
 "clients": {"host": "international", "stephens-laptop": "domestic"}}
```

The per-lane value is the primary's `litellm_params.model`. Off-host callers get the same body: a client choosing a fleet needs to know what it is choosing, and the existing header-stripping confidentiality rule covers response headers, not this deliberate listing.

**`POST /v1/ferry/fleet`** with `{"fleet": "international"}` — sets the caller's own sticky selection, identity resolved exactly as in section 4. Bearer required when a master key is configured; this is the first LAN-reachable mutation, and it can only ever change the caller's own entry. With `{"fleet": "international", "default": true}` it sets the host-wide default and is loopback-only, like every other mutation today. `{"fleet": null}` clears the caller's entry so they follow the default again. The response is the same document as GET, after the write. The write is atomic (temp file + rename) so a worker never reads a half-written file.

**`ferry fleet`** in a new `lib/ferry-fleet.zsh`, wired into `lib/ferry-main.zsh`:

| Command | Host | Client |
|---|---|---|
| `ferry fleet ls` | GET against loopback; one row per fleet with its three primaries, a `*` on the default, a `you` marker, and `keys missing` when an `os.environ/` key the fleet names is unset in the host environment | GET against `client.json`'s host and key |
| `ferry fleet show` | who am I, my resolved fleet, and every client's selection | same, own view |
| `ferry fleet use <fleet>` | POST for identity `host` | POST for identity `X-Ferry-Client: <name>` from `client.json` |
| `ferry fleet use <fleet> --default` | POST with `default: true` | refused: "the default is the host's to set" |
| `ferry fleet use --clear` | `{"fleet": null}` | same |

Every command validates the fleet name against the GET listing before posting, so a typo is caught client-side with the list of real names. `ferry fleet` prints nothing secret.

**Hot swap semantics.** A running opencode or Claude Code session never named a fleet in its config, so after `ferry fleet use international` its very next request resolves to the international lanes, in every worker, with no reload. Mid-conversation the model changes; that is the user's call and the point of the feature. A process that must not move pins itself with `FERRY_FLEET=<fleet>` (section 6), which the resolver ranks above the sticky selection.

## 6. Client and host wiring

**`ferry opencode`** (`lib/ferry-integrate.zsh`) adds a `headers` map to `provider.ferry.options`:

```json
"options": {"baseURL": "http://host:8090/v1", "apiKey": "…",
            "headers": {"X-Ferry-Client": "stephens-laptop", "X-Ferry-Fleet": "{env:FERRY_FLEET}"}}
```

Verified 2026-09-04 against the installed opencode 1.18.x binary: the provider factory is called as `createOpenAICompatible({...options, baseURL})`, so `headers` reaches the AI SDK, which sends them on every request; and `{env:VAR}` is substituted in config strings at load (22 occurrences in the bundle, including the documented `{env:GITHUB_TOKEN}` example). An unset `FERRY_FLEET` becomes an empty header value, which the resolver treats as absent. The `headers` key is ferry's and is rewritten on every run like `baseURL`; no other key under `options` is touched. The client name is the machine's short hostname, lower-cased, matching what `ferry-relay` already sends; on the host it is the literal `host` so it coincides with the loopback identity.

**`ferry claude`** (`lib/ferry-claude.zsh`): the three `claude-ferry*` wrappers add `ANTHROPIC_CUSTOM_HEADERS` carrying the same two headers, newline-separated, with the fleet line composed at call time from `$FERRY_FLEET`. Verified 2026-09-04: Claude Code 2.1.260 reads `ANTHROPIC_CUSTOM_HEADERS` (27 references in the binary).

**Client bootstrap and reset** (`client-bootstrap.sh`, `client-reset.sh`): write `"name": "<short hostname>"` into `client.json`; `ferry opencode` and `ferry claude` read it (boot-loaded as `CLIENT_NAME` in `lib/ferry-core.zsh` next to `CLIENT_HOST`). A `client.json` without `name` falls back to the hostname at run time, so an old profile keeps working.

**Host reset** (`host-reset.sh`) regenerates the three opencode profile files and the claude wrappers as it does now; they pick up the headers for free.

**One-shot pin:** `FERRY_FLEET=international opencode-super` or `FERRY_FLEET=international claude-ferry` runs one process on a fleet regardless of the sticky selection.

**Docs:** README gains a "Fleets" section (concept, the two commands, the one-shot env var, the headerless-Tailscale note); `client-config-example.json` shows the `headers` map and lists the fleet lanes; the `add-fallback-orchestrator` and `add-worker-model` skills get a one-line note that lane names are now `<fleet>.<lane>`.

## 7. Dash

`ferry-dash`'s topology parser (`parse_topology_text`) groups lane names by prefix. The routes page renders one card per fleet, each with the same order and promote editors, which keep working unchanged because the names they splice are real `model_name`s. `validate_chains` adds the same-fleet rule from section 3. A new **Fleets** panel above the cards shows the default and every client's selection, each with a dropdown that POSTs to the loopback `/v1/ferry/fleet` endpoint (the dash already holds the master key for its live calls). The live traffic panel shows the resolved fleet lane per event, which it already does once the tap records the rewritten name.

## 8. Migration on this host

1. Snapshot `litellm.yaml` (the dash's `snapshot_config` shape), then rewrite: rename the five cloud deployments (`flash` and `super-flash` on `openrouter/~google/gemini-flash-latest`, the `flash-terra` and `super-flash-luna` hops, and `heavy`) to `domestic.*` (re-read the live file first: another session was editing it on 2026-09-04), delete `orch` and `orchestrator`, prefix the two fallback entries, and add the `international.*` deployments seeded from `litellm.yaml.20260904T152100Z.bak` (Kimi K3 as `anthropic/k3` for `heavy`, `zai/glm-5.3` as its hop; the Z.ai coding-plan `zai/glm-5.3-flash` for `flash` and `super-flash` with `openrouter/~z-ai/glm-flash-latest` hops). OpenRouter models are addressed by OpenRouter's `~vendor/model-latest` alias wherever one exists, so a vendor's next flash release lands without a config edit. Reasoning settings per lane follow the recorded findings: the zai adapter drops `reasoning_effort: none`, so `super-flash` uses the lowest value the adapter honours.
2. Write `fleets.json` as `{"default": "domestic", "clients": {"host": "international"}}`: clients run domestic unless they choose otherwise, and the host's own sessions run international. Because `host` is the loopback identity, this holds for every session on the host machine, including ones started before the configs were regenerated.
3. `ferry reload`. The GPU lanes stay warm.
4. `host-reset.sh` regenerates the host's opencode profiles and claude wrappers with the headers.
5. Clients re-run bootstrap or reset when convenient. Until then they follow the default fleet.

## 9. Testing

Unit, in the existing suites (all stdlib, run by `lib/*.test.py` as today):

- `lib/ferry-front.test.py`: fleet discovery from a config; refusal of a fleet missing a lane; precedence (prefix > header > sticky > default) with a control per rank; identity (header > loopback `host` > peer IP); `orch`/`orchestrator` → `heavy`; local lanes untouched; unknown fleet → 400 with the list; non-JSON and model-less bodies pass through byte-identical; `content-length` matches the rewritten body; state reload on mtime change across two middleware instances sharing one file; GET/POST auth and the loopback-only `default` flag; atomic write; catalogue synthesis per resolved fleet.
- `lib/ferry-dashroutes.test.py`: grouping by prefix; cross-fleet hop rejected; order and promote on a `domestic.*` lane produce the same splice as before.
- `lib/ferry-dashui.test.mjs`: the Fleets panel markup exists in the page (assert against `page`, not `script`).
- `lib/ferry-integrate.test.py`: generated config carries both headers; `{env:FERRY_FLEET}` survives as a literal string; `headers` is rewritten and other `options` keys are preserved; host identity is `host`.
- `lib/ferry-claude.test.py`: wrappers export `ANTHROPIC_CUSTOM_HEADERS` with both lines and compose the fleet line from the env var.
- `lib/ferry-clientbootstrap.test.py`: `client.json` gains `name`; a legacy profile without it still drives `ferry opencode`.
- A new `lib/ferry-fleet.test.py` for the CLI: table shape, typo refusal with the real list, `--default` refused on a client.

Live, under the production loader (the CEV ladder, each with a control that must differ):

- Two concurrent streamed requests, one with `X-Ferry-Fleet: domestic` and one with `international`, return different `x-litellm-model-id` values on the loopback (unstripped) path.
- A loop of bare `heavy` requests from the host switches deployment id on the request after `ferry fleet use international`, with no reload and no gap in 200s.
- A request with `X-Ferry-Fleet: nope` returns 400 naming both fleets.
- `ferry opencode` on the host reports every bare lane served; `opencode-cloud` starts and its first request shows the headers in the tap.

## 10. Out of scope

- Per-fleet master keys or per-client authorization beyond "a client can only move itself".
- A fleet that overrides the local GPU lanes.
- Compiling fleets from one file per fleet (rejected: the dash's byte-preserving splice would need a rebuild step and two files can drift).
- A second litellm instance per fleet (rejected: a running session cannot switch, and every tool assumes one port).
