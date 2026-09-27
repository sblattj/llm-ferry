# Follow-up tasks — `ferry offer` naming and `.app` payload integrity

Staged by the `sharpen` retro of session `858e1614-9a00-4eff-be5a-f86b47f37aba`
(2026-08-30). No code in this repo was changed by that retro. Both items were
found by an implementer seat (`a81fd7ae4099fab80`) that built a stand-in server
reproducing `_tar_stream`'s byte semantics, because the host's own manifest was
empty and there was nothing real to test against.

Each claim below was re-verified against the code by the retro; where the retro
could NOT re-run the seat's observation, it says so.

## Origin

A dual-mode payload fetcher for `~/.dotorg/provision/macbook/steps/40-payloads.sh`
pulls installers from this host's ferry share, falling back to the public source.
Writing it surfaced two host-side defects and one testing gap.

## Branch and PR strategy

`~/code/llm-ferry` is the canonical dev checkout. One focused branch,
`fix/offer-naming-and-app-payloads`. **Edit `lib/ferry-*.zsh`, never `ferry`** —
`ferry` is assembled from `lib/` by `build.zsh` behind a `--check` CI gate, so a
hand-edit of `ferry` is silently reverted by the next build. Bump `VERSION` and
re-run `build.zsh` in the same commit.

## Tasks

### 1. `ferry offer` cannot name a payload — the manifest key is always the basename

**File:** `lib/ferry-transfer.zsh`, `cmd_offer()` (the embedded python heredoc)
**Verified:** yes, in code. The manifest key is computed unconditionally as
`name = os.path.basename(ap.rstrip("/"))`, `data[name] = ap`, and the usage line
is `ferry offer <path>...` — there is no way to choose a name.

**Problem:** a consumer spec that lists a payload NAME and a SOURCE PATH as two
independent columns is wrong unless the two happen to agree, because
`GET /file/<name>` resolves against the basename and nothing else. The mismatch is
silent: the client asks for the name it was told and gets a 404, or worse gets a
DIFFERENT file that happens to share a basename with something else offered. The
seat only caught it by reading `cmd_offer`; nothing in the docs flags it.

**Change:** accept an explicit name. Two options, in preference order:

- `ferry offer --as <name> <path>` (and/or `ferry offer <name>=<path>`), falling
  back to the basename when no name is given, so every existing invocation keeps
  working.
- At minimum, make `cmd_offer` **refuse a collision** — if `data[name]` already
  exists and points at a different absolute path, error out instead of silently
  overwriting the earlier entry.

**Also:** print the resulting `GET /file/<name>` URL for each offered path, so the
name the client must use is visible at offer time rather than inferred.

**Commit:** `ferry offer: let the caller name a payload, and refuse a silent collision`

### 2. `_tar_stream`'s `dereference=True` breaks a ferried `.app`'s code signature

**File:** `lib/ferry-share.zsh`, `DynamicHandler._tar_stream` (line ~73-80)
**Verified:** the code is as reported — `tarfile.open(..., mode="w|", dereference=True)`,
unconditional, with a comment explaining it exists to resolve the HuggingFace
cache's `snapshots/ -> ../../blobs/` symlinks into real content so a pulled model
is self-contained. **Not re-verified by the retro:** the downstream symptom (a
ferried `.app` failing `codesign --verify`). That was observed by the seat, which
built and ad-hoc-signed its own bundle to test it after SIP blocked copying a
system `.app`. Re-confirm with `codesign --verify --deep --strict` on a ferried
bundle before/after the fix.

**Mechanism:** a macOS app bundle's seal covers its symlink structure —
`Contents/Frameworks/*.framework/Versions/Current`, `Contents/MacOS` links, and so
on. `dereference=True` replaces each of those links with a duplicated real file, so
the extracted bundle has a different layout from the one that was signed and the
signature no longer validates. It is exactly the right setting for an HF cache and
exactly the wrong one for an `.app`.

**Change:** make dereferencing a property of the payload rather than of the server.
Either decide per payload (`dereference` off when the root is a `*.app` or contains
`Contents/MacOS`, on otherwise), or record the intent in `offered.json` at offer
time (`{"name": {"path": ..., "deref": false}}`) and have `_tar_stream` read it —
the second is more explicit and survives payloads that are neither an HF cache nor
an app. Keep the current behavior as the default so model pulls are unaffected.

**Test plan:** ferry an ad-hoc-signed `.app` both ways; `codesign --verify --deep
--strict` must pass with dereferencing off and fail with it on. That failing case is
the control — without it the test cannot fail and proves nothing.

**Commit:** `ferry share: don't dereference symlinks for .app payloads, it voids the signature`

### 3. Testing gap: the host's offered manifest is empty

**Observed:** `~/.config/ferry/offered.json` had `"files": []`, so a client-side
payload fetcher could only be dry-run — the seat spent ~20 min building a stand-in
server reproducing `_tar_stream`'s byte semantics to get real evidence. That
stand-in is what surfaced items 1 and 2, so the time was well spent, but it should
not be the only path.

**Change:** add a `ferry offer --selftest` (or a `scripts/` fixture) that offers a
tiny generated tree — one plain file, one symlink, one minimal `.app` bundle — so
any client-side work can be exercised against a real `GET /file/<name>` without
staging real installers, and so items 1 and 2 both get a regression test.

**Commit:** `ferry: ship a tiny offered-payload fixture so client work is testable`

### 4. `/file/` ignores `Range`, so `curl -r 0-N` silently downloads the whole payload

**File:** `lib/ferry-share.zsh`, `DynamicHandler` (the `/file/` route and `_tar_stream`)
**Observed by:** seat `a5c2c210b092cb5ad`, which tested it proactively rather than
relying on it (~5 min). **Verified by the retro:** the handler code sends no
`Accept-Ranges` and no `Content-Length` and never inspects the `Range` header, which
is consistent with the report; the retro did not re-run the `curl -r` itself.

**Problem:** `curl -r 0-1024 .../file/<name>` returns the entire payload. A caller
using a byte range as a cheap "is it there / how big is it" probe or as a size guard
therefore downloads the whole thing — for the `opencode` payload that is 137MB
instead of 1KB, with no error and no signal that the range was ignored. Note the
provisioner brief itself suggested exactly this technique, which is why it was worth
testing before trusting.

**Change:** either honor `Range` on `/file/` (respond `206` with `Content-Range`
where the payload is a plain file), or make the no-range case explicit — advertise
`Accept-Ranges: none` and document that `/file/` is a streaming tar with no length.
A `HEAD` route, or a `/size/<name>` endpoint, gives callers the cheap probe they
actually want. **`_tar_stream` is a streaming `w|` tar, so its length is genuinely
unknown up front** — a size endpoint that stats the source tree is the honest answer
for the directory case, not a `Content-Length` on the stream.

**Commit:** `ferry share: stop silently ignoring Range on /file/, and give callers a size probe`

## PR description draft

**Summary**

Three host-side fixes found while writing a client that pulls installers from the
ferry share.

- `ferry offer` derived the manifest key from the path's basename with no way to
  override it, so any consumer that treats the payload NAME and SOURCE PATH as
  independent gets a 404 or the wrong file, silently. Adds an explicit name and
  refuses a silent collision.
- `_tar_stream` dereferenced symlinks unconditionally (correct for a HuggingFace
  cache, wrong for a macOS `.app`, whose code signature seals its symlink layout).
  Dereferencing is now a per-payload property.
- Adds a tiny offered-payload fixture, because the host manifest being empty meant
  client-side work could only be tested against a hand-built stand-in server.

**Test plan**

- `build.zsh --check` clean; `VERSION` bumped.
- `ferry offer --as tools /some/path/toolsdir` then `curl .../file/tools` returns
  that tree; `curl .../file/toolsdir` 404s.
- Offering two different paths with the same basename errors instead of clobbering.
- An ad-hoc-signed `.app` ferried with dereferencing off passes
  `codesign --verify --deep --strict`; with it on, it fails (the control).
- An HF model pull is unchanged — snapshots still arrive as real content.
