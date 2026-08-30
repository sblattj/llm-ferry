# Ferry route editor — design

**Date:** 2026-08-29
**Status:** implemented in v1.15.0
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

A **Routes** panel below the existing topology view. Per lane: its primary, then
the ordered hops, each showing the model and the deployment name. Per hop, `↑`
`↓` to reorder and `remove` to drop it; a select adds a hop from the configured
backends. A lane whose chain differs from the file is marked `modified`.

**Reorder is buttons, not drag.** The first draft of this spec said drag. Buttons
are unambiguous about where an item lands, work without a pointer, and can be
asserted directly from a test — a dragged list is none of those, and this is the
control surface for the config that routes every request.

**Apply is reachable only through Preview**, which validates and renders the
unified diff, so the diff on screen is always the diff that gets written. Apply
then snapshots and splices.

Two behaviours that are not obvious and are load-bearing:

- The 5s status poll does not reseed the editor while an edit is in progress.
  Re-rendering mid-edit would drop a half-built chain and reset the buttons under
  the cursor.
- The editor's "unsaved edits" comparison is against a server view built with the
  SAME key set — a lane with no `fallbacks` entry is an empty chain, not a
  missing one. Building it from the `fallbacks` keys alone made the panel claim
  unsaved edits on load, which also froze the poll. Caught only by opening the
  page; no unit test covered it.

**After applying, the running proxy still has the old chains.** litellm has no
reload for a file-backed config, so the response says to run `ferry update`.

### Writer

**ONE edit shape: rewrite the single `fallbacks:` line.** An earlier draft listed
three, including inserting and deleting deployment blocks. Narrowing v1 to chain
editing removed the need for the other two, and that is most of why this is safe:
editing a chain cannot touch any other line, so the writer never has to reason
about where a block begins or ends.

Every other byte passes through untouched. The line is located by anchored regex
(`^(\s*)fallbacks:\s*\[.*\]\s*$`), and the writer refuses rather than guessing
when there is no match or more than one — a config it cannot locate exactly is a
config it must not edit. Lanes absent from the submitted edit keep their existing
entry, so editing one lane cannot silently drop another's failover.

Before every write: snapshot the whole original to a UTC-timestamped sibling
(`shutil.copy2`), then write through a temp file and `os.replace`, so the config
is never observed half-written.

Verified end-to-end 2026-08-30 against a copy of the real 459-line config: 279
comment lines in, 279 out, exactly one line changed, the snapshot byte-identical
to the pre-write file, and the result parsed by litellm's own loader.

### Applying

Writing the file does not change what the proxy is serving; litellm has no reload
for a file-backed config (see Constraints). The apply response says so and points
at `ferry update` (v1.14.0), which on a host runs `host-reset.sh` — validate the
route config, bounce the proxy — without touching the MLX lanes on 8092/8093.

## Validation

Every rewire is validated before it is written, and a failed validation writes
nothing at all (validation runs before the snapshot, so a rejected edit leaves no
debris):

- every lane is a real `model_name` in this config — a lane reachable only by
  alias silently gets no chain at all, which is the bug in Problem (1)
- every hop is a real `model_name` — litellm skips an unknown hop silently
- no lane lists itself as its own fallback
- no hop appears twice in one chain
- an empty chain is allowed: it means hard-fail rather than spill, which is what
  the local GPU lanes deliberately do

Deliberately NOT validated in v1, because chain editing cannot change any of
them: duplicate `model_name`, duplicate `model_info.id`, and whether the file
parses as YAML. The written line is generated by `json.dumps` from names that
already validated, so it cannot produce YAML this writer would otherwise accept.
These become necessary the moment the editor can add or remove deployments —
which is the v2 boundary.

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

The unit suite covers the writer, not the page. Both UI defects found during
implementation — the editor claiming unsaved edits on load, and diff file headers
rendered as changed lines — were invisible to it and surfaced only by opening the
page and driving it. Treat "the suite is green" as a statement about the writer
alone, and open the dashboard before believing anything about the panel.

## v1 scope

Reorder, add, and remove hops on lanes that already exist, from backends already
configured.

Out of scope: defining new backends (needs credential handling — a materially
larger surface on a LAN-served tool), and creating or deleting lanes (a lane name
is a shared-namespace claim clients bind to; deleting one 404s any client still
on it, which is the damage class v1.8.9 exists to prevent).
