# Follow-up: ANSI colour is still unconditional outside `ferry drop`

## Origin

Produced by the `sharpen` retrospective on session
`535259f2-06e6-4156-b146-970816725442` (2026-08-30), the session that shipped
v1.17.0.

While building `ferry drop`, a demo harness scraped the printed passphrase and
got a value that failed to decrypt. The harness had its own bug, but chasing it
surfaced a **real defect in ferry**: `ferry drop` wrapped the passphrase in ANSI
escape codes unconditionally, so `ferry drop f | grep passphrase` returned
something subtly wrong rather than something that obviously failed.

**That instance was fixed in v1.17.0** — `lib/ferry-drop.zsh:179` and `:351` now
gate colour on `[[ -t 1 ]]`, with a regression test
(`test_passphrase_is_plain_text_when_stdout_is_not_a_tty`) that round-trips the
scraped value.

**The class was not fixed.** `ferry-drop.zsh` is the only module in `lib/` that
gates colour on a tty. Measured 2026-08-30:

| Module | ANSI escapes | `-t 1` gates |
|---|---|---|
| `lib/ferry-drop.zsh` | 2 | 2 |
| `lib/ferry-serve.zsh` | 13 | **0** |
| `lib/ferry-share.zsh` | 3 | **0** |
| `lib/ferry-relay.zsh` | 1 | **0** |

## Why this is worth a PR rather than a shrug

Two of the un-gated sites print values a human or a script is *expected to copy*,
which is what made the `drop` instance a bug rather than a cosmetic issue:

- **`lib/ferry-relay.zsh:101`** — the same shape as the bug just fixed, with the
  same kind of secret:

  ```zsh
  echo "    \033[1;32mferry expose <local-port> --as <public-port> --token $token\033[0m"
  ```

  A copy-paste command line carrying a **token**, wrapped in escapes. Redirect
  `ferry relay` to a file or a log and the token comes back with escape bytes
  around it; scrape it and you get a mangled credential, and the failure surfaces
  later as an authentication error that points at the wrong thing. This is
  precisely the failure the `drop` fix was written for, still live.

- **`lib/ferry-share.zsh:23,31,36`** — the three `curl … | zsh` bootstrap
  commands, i.e. the lines most likely to be piped into a file and pasted into
  another machine's terminal.

- **`lib/ferry-serve.zsh`** — 13 sites, including `READY` / `NOT READY` status
  and the active model name (`:688`, `:694`), which is the output a monitoring
  script would parse.

## Branch and PR strategy

- Branch: `ansi-tty-gate`
- One focused PR. Mechanical and low-risk, but it touches four modules, so it
  wants its own review rather than riding along with a feature.
- Do it **before** the next release that touches `ferry-relay`, since the token
  line is the one with a real consequence.

## Task list

### 1. Extract the tty gate into one shared helper

**File:** `lib/ferry-core.zsh` (the shared module)

`ferry-drop.zsh` currently repeats the gate inline at `:179` and `:351`. Four
modules should not each grow their own copy. Add one helper that sets the colour
variables once, honouring both a non-tty stdout and the `NO_COLOR` convention:

```zsh
# Colour is for a human at a terminal. When stdout is redirected, everything we
# print may be scraped by a script or pasted into another shell — a value
# wrapped in escape codes comes back subtly wrong rather than obviously broken.
# Honours https://no-color.org as well.
_ferry_colors() {
  if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RESET=$'\033[0m'
  else
    C_GREEN=""; C_YELLOW=""; C_RESET=""
  fi
}
```

### 2. Convert the four modules to the helper

**Files:** `lib/ferry-relay.zsh`, `lib/ferry-share.zsh`, `lib/ferry-serve.zsh`,
`lib/ferry-drop.zsh`

Replace every literal `\033[…m` with the variables. Do `ferry-relay.zsh:101`
first — it is the one carrying a secret. Convert `ferry-drop.zsh` too so there is
exactly one implementation in the tree, not two.

Enumerate the sites before editing rather than fixing them as they turn up:

```bash
grep -rn '\\033\[' lib/
```

### 3. Rebuild and add the regression test

`ferry` is generated from `lib/` by `build.zsh` — editing `lib/` alone means the
shipped CLI still runs the old code.

```bash
zsh ./build.zsh && zsh ./build.zsh --check
```

Generalise the existing `drop` test into one that covers every module. The test
already in the tree is the right model: it asserts on the REAL built binary
through a pipe (`subprocess` gives a pipe by construction, which is the redirected
case), so it cannot pass by accident.

```python
def test_no_ansi_escapes_in_piped_output(self):
    """subprocess gives us a pipe, so this is the redirected case by construction."""
    for args in (["share"], ["relay", "--help"], ["drop", "plain.txt"]):
        r = self.ferry(*args)
        self.assertNotIn("\033", r.stdout, f"ANSI leaked into piped output: {args}")
```

Pair it with a control that would fail if the assertion were vacuous — confirm at
least one of those invocations produces non-empty stdout, or the test passes on a
command that printed nothing at all.

**Ideal commit message:**

```
Colour is for a terminal, not for a pipe

v1.17.0 fixed this for `ferry drop`, where the passphrase came back
wrapped in escape codes and scraped to something subtly wrong. The fix
stopped at the module that happened to expose it; the other three print
colour unconditionally.

Two of them print values meant to be copied: ferry-relay prints a
`ferry expose --token <token>` line, and ferry-share prints the three
curl bootstrap commands. Redirect either and the escapes ride along.

One helper in ferry-core now gates colour on `[[ -t 1 ]]` and NO_COLOR,
and all four modules use it. Test asserts no ESC byte reaches a pipe.
```

## PR description draft

**Summary**

`ferry drop` learned in v1.17.0 that colour belongs to a terminal: a passphrase
wrapped in ANSI escapes scrapes to something subtly wrong rather than something
that obviously failed. That fix stopped at the one module that exposed the bug.

`ferry-serve` (13 sites), `ferry-share` (3) and `ferry-relay` (1) still emit
colour unconditionally. Two of them print things meant to be copied —
`ferry-relay.zsh:101` is a `ferry expose … --token <token>` command line, the
same shape and the same kind of secret as the bug already fixed, and
`ferry-share` prints the `curl … | zsh` bootstrap commands.

This adds one `_ferry_colors` helper in `ferry-core` (gating on `[[ -t 1 ]]` and
`NO_COLOR`) and converts all four modules to it, so there is a single
implementation rather than a per-module habit.

**Test plan**

- [ ] `grep -rn '\\033\[' lib/` returns no literal escapes outside the helper.
- [ ] `zsh ./build.zsh && zsh ./build.zsh --check` passes (the shipped `ferry`
      matches `lib/`).
- [ ] Full suite: `for t in lib/*.test.py; do python3 "$t"; done` — all green.
- [ ] New test asserts no `\033` reaches stdout through a pipe, with a control
      confirming those invocations actually printed something.
- [ ] Manual: `ferry relay` in a terminal is still coloured; `ferry relay | cat`
      is plain, and the token scrapes to a value that works verbatim.
- [ ] `NO_COLOR=1 ferry share` is plain even on a tty.
