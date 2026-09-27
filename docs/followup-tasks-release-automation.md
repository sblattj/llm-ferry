# Follow-up: automate the tag → GitHub Release step

> **RESOLVED 2026-09-27:** task 1 landed as `.github/workflows/release.yml` + `scripts/release-notes.py` (tests: `scripts/release-notes.test.py`). Like the sketch below it reads the annotated tag's message first, but it falls through to the tagged commit and `docs/releases/<tag>.md` for lightweight tags (v1.37.0 is one), re-fetches tags because checkout can flatten the triggering tag, and `--latest` is computed from semver so a re-run for an old tag cannot steal the marker. Tasks 2 (drift check) and 3 (worktree prune) remain open; the history below is kept as written.

## Origin

Produced by the `sharpen` retrospective on session
`535259f2-06e6-4156-b146-970816725442` (2026-08-30), the session that shipped
v1.17.0 (`ferry drop` / `ferry pickup` plus the host-side opencode wrapper
installer).

**What was already completed in that session**, by hand:

- v1.17.0 tagged at `a305b32` and its GitHub Release published.
- The four missing Releases backfilled — v1.14.0, v1.15.0, v1.15.1, v1.16.0 —
  each created with `--latest=false` so the Latest marker provably stayed on
  v1.17.0.
- Verified afterwards: **41 tags, 41 releases, no gap.**

**What is left, and why it is a repo change rather than a habit.** The Releases
page had been frozen at **v1.13.0 while four tags shipped past it**. The cause is
structural, not carelessness: `llm-ferry` has no `.github/workflows/` directory at
all, so `git push origin vX.Y.Z` is the entire release procedure and nothing
creates the Release object. Every individual release looked successful — the tag
pushed, the code went live, `git tag` listed it — while the only surface a
stranger reads stayed empty. Nothing in the repo will stop this recurring on
v1.18.0.

A knowledge-side fix already landed in the `oss-launch` skill (a tag-set vs
release-set diff to run at every cut, plus the `--latest=false` backfill rule).
That is a checklist item performed by whoever cuts the release. The item below
removes the human from the loop entirely, which is the durable half.

## Branch and PR strategy

- Branch: `release-automation`
- One focused PR. It touches only new files under `.github/`; no shell, no
  `lib/`, no `ferry` rebuild, so it cannot affect the CLI.
- Reviewers: repo owner (solo repo — self-merge is fine).
- Not urgent, but it should land **before the next version bump**, since its
  whole value is being in place when the next tag is pushed.

## Task list

### 1. Add `.github/workflows/release.yml`

**File:** `.github/workflows/release.yml` (new; the `.github/workflows/`
directory does not exist yet and must be created)

**What it does:** on any `v*` tag push, create the GitHub Release for that tag if
one does not already exist.

```yaml
name: release

on:
  push:
    tags: ["v*"]

permissions:
  contents: write

jobs:
  create-release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Create the GitHub Release if it is missing
        env:
          GH_TOKEN: ${{ github.token }}
          TAG: ${{ github.ref_name }}
        run: |
          set -euo pipefail
          if gh release view "$TAG" >/dev/null 2>&1; then
            echo "Release $TAG already exists — nothing to do."
            exit 0
          fi
          # Prefer the annotated tag's own message as the notes; it is written
          # deliberately at tag time and reads better than generated notes.
          notes="$(git tag -l --format='%(contents)' "$TAG")"
          if [ -n "${notes// /}" ]; then
            printf '%s\n' "$notes" > /tmp/notes.md
            gh release create "$TAG" --title "$TAG" --notes-file /tmp/notes.md
          else
            gh release create "$TAG" --title "$TAG" --generate-notes
          fi
```

**Why these specific choices:**

- **Idempotent** (`gh release view … || create`). A re-run, or a tag pushed twice,
  must not error or duplicate.
- **`permissions: contents: write`** — the default token is read-only and
  `gh release create` fails without this. This is the single most common reason
  a workflow like this looks correct and does nothing.
- **Built-in `gh`** rather than a third-party release action: no floating ref to
  pin and no supply-chain surface for a repo whose whole pitch is that it is
  auditable shell.
- **`fetch-depth: 0`** so the annotated tag's message is actually available;
  a shallow checkout does not fetch tag objects.
- **No `--latest=false`** here on purpose: a workflow only ever fires on a
  freshly pushed tag, which *is* the newest, so the default is correct. The flag
  is only needed when backfilling an older tag by hand (see the `oss-launch`
  skill).

**Ideal commit message:**

```
Tagging a version now creates its GitHub Release

The Releases page had been frozen at v1.13.0 while four tags shipped past
it — v1.14.0, v1.15.0, v1.15.1 and v1.16.0 all existed as tags with no
release object. Nothing was broken; there was simply no step that made
one. This repo has no workflows at all, so `git push origin vX.Y.Z` was
the entire release, and the only surface a stranger reads stayed empty.

A tag push now creates the release from the annotated tag's own message,
falling back to generated notes. Idempotent, so re-running or re-pushing
a tag is a no-op rather than an error.
```

### 2. (Optional, same PR) Add a tag/release drift check

**File:** `.github/workflows/release.yml` — an additional job, or a small
`scripts/check-releases.sh`.

Guards the *accumulated* gap rather than the current release, which is the shape
the v1.13.0→v1.16.0 drift actually took. A per-release check cannot see it.

```bash
diff <(git tag --sort=-v:refname) \
     <(gh release list --limit 100 | awk -F'\t' '{print $3}' | sort -rV)
```

Non-empty left column = a tag with no release. **Echo both counts before
trusting a clean result** — both sides fail silently to empty outside a git
repo, and `diff` of two empty lists exits 0, so the check reports success when
it is broken. Verified 2026-08-30: 41/41 clean, and removing one release entry
makes it exit 1.

### 3. (Housekeeping, separate from the PR) Prune the stale worktrees

Not a repo change — local state only, so it needs no PR, but it is the other
thing that session found and nobody owns.

Four worktrees are parked on commits that are already merged and released:

| Worktree | Branch | Commit |
|---|---|---|
| `.claude/worktrees/ferry-inbox` | `ferry-relay` | `42a17dd` |
| `.claude/worktrees/ferry-update` | `ferry-update` | `8f96333` |
| `.claude/worktrees/live-observ` | `live-observ` | `26fe3ce` (v1.16.0) |
| `.claude/worktrees/route-editor` | `route-editor` | `f144487` |

Prove each holds nothing unique before removing it:

```bash
git -C <wt> status --porcelain                        # must be empty
git -C <wt> log <branch> --not origin/main --oneline  # must be empty
git worktree remove <wt> && git branch -d <branch>
```

## PR description draft

**Summary**

Tagging a version now creates its GitHub Release automatically.

The Releases page had been frozen at v1.13.0 while four tags shipped past it
(v1.14.0, v1.15.0, v1.15.1, v1.16.0). Those four were backfilled by hand when
v1.17.0 went out, but nothing prevented a recurrence: this repo has no
workflows, so pushing a tag was the entire release procedure and no step ever
created the Release object. Each release looked fine in isolation — the tag
pushed, the code was live — while the page a stranger reads stayed stale.

This adds `.github/workflows/release.yml`, which fires on a `v*` tag push and
creates the release from the annotated tag's own message (falling back to
generated notes). It is idempotent, so re-pushing a tag is a no-op.

No shell, no `lib/`, no `ferry` rebuild — CLI behavior is unchanged.

**Test plan**

- [ ] `actionlint .github/workflows/release.yml` (or the GitHub editor's
      validator) reports no syntax errors.
- [ ] Push a throwaway tag `v0.0.0-test` on a branch; confirm the workflow runs
      and creates the release. Delete both the release and the tag afterwards.
- [ ] Re-push the same throwaway tag and confirm the run is a clean no-op
      ("already exists") rather than a failure — this is the idempotency path.
- [ ] Confirm the release body picked up the annotated tag message rather than
      the generated-notes fallback.
- [ ] After merge, `diff <(git tag --sort=-v:refname) <(gh release list --limit 100 | awk -F'\t' '{print $3}' | sort -rV)`
      is empty, with both counts non-zero and equal.
