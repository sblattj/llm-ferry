# Ferry route editor — design

**Date:** 2026-08-29
**Status:** approved, not yet implemented
**Scope:** add lane-chain editing to `ferry dash`

## Problem

A ferry lane is a name plus an ordered failover chain. Both live in
`~/.config/ferry/litellm.yaml`, and today the only way to change a chain is to
hand-edit that file, restart the proxy, and hope. The chains were rewired four
times in the week of 2026-08-22 alone.

Hand-editing has already produced two silent outages:

1. **Aliased lanes lose their entire chain.** A `model_group_alias` resolves for
   deployment selection but not for fallbacks, because litellm reads the
   fallbacks map with the raw client-supplied model string *before* alias
   resolution (`router.py:6411` → `:6345` → `:6357`, while alias resolution is at
   `:9278`). Three lanes — `orchestrator`, `gemini-3.7-flash`, `super-flash` —
   had zero failover and looked fine. `super-flash` is the compaction lane, where
   a failure drops the whole transcript. Fixed 2026-08-29.
2. **A duplicated `model_info.id`** silently conflated paid and free traffic in
   every Grafana panel grouped by model id.

Neither is visible from reading the config. Both are trivially visible in a UI
that renders the resolved chains.

`ferry dash` already parses and displays this exact data (`ferry-dash:78
load_topology`), read-only. This adds the write half.

## Constraints

- **`ferry-dash` is stdlib-only by contract** (its docstring: "runs under any
  python3 on macOS and Linux — no venv, no pip"). Verified 2026-08-29 that bare
  `/usr/bin/python3` has neither `pyyaml` nor `ruamel`. The writer cannot import
  a YAML library.
- **The config is 66% commentary** (267 comment lines of 404). That commentary is
  load-bearing: the Google ToS §2.d warning behind the 2026-08-25 nine-project
  suspension, the duplicate-`model_info` trap, measured reasoning-token tables
  telling a future reader not to re-tune an already-tested parameter. A
  `safe_load`/`dump` round-trip destroys all of it.
- **No config reload endpoint exists.** litellm's reload paths are DB-backed
  (`reload_mcp_servers_from_db`, prisma config params) or unrelated
  (`reload_model_cost_map`). Applying a change means restarting the proxy.
- **A lane name must be a real `model_name`**, never an alias — see Problem (1).
  Catalogue visibility is controlled separately, by `model_info: {public: true}`,
  which `front/ferry_front.py` filters on (v1.9.0).

## Design

### Data model

The UI reads and writes two things:

- **Backends** — the `model_list` deployments. Identified by `model_name`,
  described by provider + model id.
- **Chains** — `router_settings.fallbacks`, a lane name mapped to an ordered list
  of backend names.

A lane's *primary* is the deployment sharing the lane's name; its chain is the
fallbacks entry. So the lane `flash` and the backend "glm-5.3-flash on z.ai" are
one object, and a chain hop referencing `flash` means that backend. The UI shows
provider + model, and emits the name.

**A hop is one name, but a name may be one deployment or several.** litellm has
two distinct failover mechanisms and the UI must render both:

| | Ordered chain | Pool |
|---|---|---|
| Shape | distinct `model_name`s listed in `fallbacks` | several deployments sharing ONE identical `model_name` |
| Selection | in order, on failure | `usage-based-routing-v2`, proactive even split |
| Order | meaningful | meaningless |
| `fallbacks` entry | required | none |

They compose: a chain hop can be a pooled name, so a lane is an ordered list of
hops where any hop may fan out. Pick a chain when the members differ in quality
(an even split would send traffic to the weakest); pick a pool when members are
interchangeable and the goal is throughput — on Gemini, several model ids on one
key, since limits are per-project-per-model.

`heavy`, `flash`, and `super-flash` are all ordered chains as of 2026-08-29. The
UI supports both from v1 because the config already contains deployments that
were documented as a pool while wired as a chain, and a UI that renders only one
mechanism cannot show that mismatch — which is precisely the class of bug it
exists to surface.

### UI

A **Routes** panel below the existing topology view. Per lane: an ordered list of
hops, each showing model, provider, and the liveness the dash already collects.
Drag to reorder. `+` adds a hop from the configured-backend catalogue; `×`
removes one.

**Apply** renders a unified diff of the YAML change and waits for confirmation.
On confirm: snapshot, splice, restart, re-probe, report which lane names serve.

### Writer

Anchor-based line splicing with exactly three edit shapes:

1. delete a contiguous anchored block
2. insert deployment blocks before an anchor line
3. rewrite the single `fallbacks:` line

Every other line passes through byte-identical. Deployment blocks the writer
creates carry a sentinel comment so it can find them again without parsing prose.

Before every write, snapshot via the existing helper
(`lib/ferry-integrate.zsh:262 snapshot()`): `shutil.copy2` of the whole original
to a UTC-timestamped sibling, with stem-anchored retention pruning.

A prototype of this writer was used to apply the 2026-08-29 rewire. It preserved
all 267 comment lines with 0 unexpected losses, and its preservation check was
proven falsifiable by a control that deletes a kept comment and asserts the check
flags it.

### Applying

`ferry up --route`. It stops litellm on the target port, waits, frees the port,
and relaunches (`lib/ferry-serve.zsh:448-449`). It does not touch the MLX lanes
on 8092/8093 — verified 2026-08-29: they held 2d15h uptime across a restart.

## Validation

Before any write, and again after the restart:

- every `fallbacks` key and every hop resolves to a real `model_name`
- no duplicate `model_name`
- no duplicate `model_info.id`
- no lane is reachable only via `model_group_alias`
- **the file parses** — see below

The parse check cannot run in `ferry-dash`'s own interpreter, which has no YAML
library (see Constraints). It shells out to the interpreter that actually serves
the config, resolved by the existing `_ferry_front_python()`
(`lib/ferry-serve.zsh:221`) — the litellm venv, which has `pyyaml`.

That is a feature, not a workaround: validating in the interpreter that will load
the file is the only check that can prove the file is loadable. A parse that
succeeds in some other Python proves the bytes are well-formed YAML, not that the
proxy can start on them.

After the restart, probe each lane name and report which backend served. To prove
a *chain* rather than a primary, run the probe against a throwaway instance with
`general_settings.dangerously_allow_mock_testing_request_params: true` and
`mock_testing_fallbacks` — never on the LAN-facing proxy.

## Testing

- splice against a copy of the real config; assert every comment line survives
  byte-identical, with a control that must fail
- each validation rule above, with a fixture that violates it
- an alias-shaped lane is rejected with the reason
- the writer refuses when an anchor does not match, rather than writing partially

## v1 scope

Reorder, add, and remove hops on lanes that already exist, from backends already
configured.

Out of scope: defining new backends (needs credential handling — a materially
larger surface on a LAN-served tool), and creating or deleting lanes (a lane name
is a shared-namespace claim clients bind to; deleting one 404s any client still
on it, which is the damage class v1.8.9 exists to prevent).
