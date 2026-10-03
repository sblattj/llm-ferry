# llm-ferry — notes for coding agents

- `ferry` is a GENERATED file. Do not edit it. `build.zsh` assembles it from `lib/ferry-*.zsh` in the
  order of its `MODULES=(...)` list.
  - Subcommand dispatch (`case` in the entry point): `lib/ferry-main.zsh`
  - Top-level help / usage text: `lib/ferry-usage.zsh`
  - A new module `lib/ferry-<name>.zsh` must be added to `MODULES` in `build.zsh`
- After editing anything under `lib/ferry-*.zsh`, run `./build.zsh` and commit `ferry` together with
  its sources. `./build.zsh --check` must print `ferry is in sync with lib/`.
- Tests: `lib/*.test.*`, `observ/*.test.py`, `scripts/*.test.py`. The full run takes several minutes.
  Edit first, run `./build.zsh`, then start the suite. Do not edit `lib/ferry-*.zsh` (even comments)
  while the suite runs: the sync test compares `ferry` to a fresh build, fails, and costs a full rerun.
- `ferry` runs under errexit. A bare `x=$(cmd)` whose `cmd` returns non-zero silently kills the calling
  function with no output. Write `x=$(cmd) || rc=$?` or `if x=$(cmd); then`. Diagnose with
  `zsh -x ferry ...`.
