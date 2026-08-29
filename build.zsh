#!/usr/bin/env zsh
# build.zsh — assemble the domain modules in lib/ into the single distributable
# `ferry` script.
#
# `ferry` is a GENERATED artifact: clients fetch it as ONE file
# (`curl host:8095/ferry`), so the shipped CLI has to stay a single script even
# though we develop it as focused per-domain modules. Edit the modules under
# lib/ferry-*.zsh, then run ./build.zsh to regenerate ./ferry, and commit both.
#
#   ./build.zsh           regenerate ./ferry from lib/ferry-*.zsh
#   ./build.zsh --check    verify ./ferry matches the modules (CI / pre-commit);
#                          exits 1 if they have drifted out of sync
set -eu
cd "${0:A:h}"

# Assembly order matters: the core bootstrap runs at load time (it defines
# constants and probes the LAN), the command functions follow, and the dispatch
# parser MUST come last so every cmd_* it calls is already defined.
MODULES=(core usage install serve share inbox transfer proxy integrate dash main)

build_to() {
  local out="$1" m
  {
    print -r -- '#!/usr/bin/env zsh'
    print -r -- '# ============================================================================'
    print -r -- '# GENERATED FILE — DO NOT EDIT. Assembled from lib/ferry-*.zsh by build.zsh.'
    print -r -- '# Edit the modules under lib/ and run ./build.zsh to regenerate this file.'
    print -r -- '# ============================================================================'
    for m in $MODULES; do
      cat "lib/ferry-$m.zsh"
    done
  } > "$out"
}

if [[ "${1:-}" == "--check" ]]; then
  tmp="$(mktemp)"
  build_to "$tmp"
  if diff -q "$tmp" ferry >/dev/null 2>&1; then
    rm -f "$tmp"
    print "ferry is in sync with lib/ ✓"
    exit 0
  fi
  print "ERROR: ferry is OUT OF SYNC with lib/. Run ./build.zsh and commit ferry." >&2
  diff ferry "$tmp" >&2 || true
  rm -f "$tmp"
  exit 1
fi

build_to ferry.tmp
chmod +x ferry.tmp
mv ferry.tmp ferry
print "Built ferry from ${#MODULES} modules: $MODULES"
