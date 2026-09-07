#!/usr/bin/env zsh
# Behavioural test for the zsh mirror of resolve_chatgpt_instructions().
#
# Run:  zsh lib/ferry-chatgpt-instructions.test.zsh
#       (also driven from lib/ferry-serve.test.py so `python3 lib/ferry-serve.test.py`
#        covers it)
#
# WHY A SEPARATE HARNESS. lib/ferry-serve.test.py can only regex the shipped
# `ferry` text, and a text assertion cannot see behaviour: dropping the `export`
# keyword, or swapping the user file and the shipped file in the candidate list,
# both leave that suite fully green while production silently serves litellm's
# Codex prompt again. So this file runs the REAL function.
#
# Three properties the shape tests structurally cannot reach:
#
#   1. It reads the BUILT `ferry`, not lib/ferry-serve.zsh. `ferry` is the
#      artifact clients fetch and run; lib/ is only its source.
#   2. Every assertion is made in a CHILD process
#      (`zsh -fc 'print -r -- ${CHATGPT_DEFAULT_INSTRUCTIONS-<unset>}'`), because
#      what matters is what the `nohup litellm` the helper precedes will
#      INHERIT. A plain assignment sets the variable in the calling shell and
#      passes any same-shell check; the child never sees it.
#   3. `<unset>` is distinguished from the empty string. litellm reads
#      `os.getenv(...) or CHATGPT_DEFAULT_INSTRUCTIONS`, so an exported "" is
#      not an override — it silently restores the Codex prompt.
set -u

REPO="${0:A:h:h}"
FERRY="$REPO/ferry"
WORK="$(mktemp -d)"
HELPER="$WORK/helper.zsh"
ERR="$WORK/stderr.txt"
FAILED=0

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

if [[ ! -r "$FERRY" ]]; then
  print -r -- "FAIL setup: built ferry not found at $FERRY (run ./build.zsh)"
  exit 1
fi

# Carve the function out of the generated CLI exactly as shipped.
awk '/^_ferry_export_chatgpt_instructions\(\) \{$/,/^\}$/' "$FERRY" > "$HELPER"
if [[ ! -s "$HELPER" ]]; then
  print -r -- "FAIL setup: _ferry_export_chatgpt_instructions is not in $FERRY"
  exit 1
fi
if [[ "$(tail -n 1 "$HELPER")" != "}" ]]; then
  print -r -- "FAIL setup: extracted helper is truncated (no closing brace)"
  exit 1
fi
source "$HELPER"

SHIPPED="$REPO/front/chatgpt-instructions.txt"
if [[ ! -r "$SHIPPED" ]]; then
  print -r -- "FAIL setup: shipped prompt missing at $SHIPPED"
  exit 1
fi
SHIPPED_MARK="You are not running in the Codex CLI"

TMP="$WORK/home"
mkdir -p "$TMP/.config/ferry"
export HOME="$TMP"
APP_DIR="$REPO"
USER_FILE="$TMP/.config/ferry/chatgpt-instructions.txt"

reset_env() {
  unset CHATGPT_DEFAULT_INSTRUCTIONS
  unset FERRY_CHATGPT_INSTRUCTIONS
  APP_DIR="$REPO"
  rm -rf "$USER_FILE"
  print -r -- "User prompt." > "$USER_FILE"
  : > "$ERR"
}

run_helper() {
  # stderr to a FILE, not a pipe: a command substitution would run the helper
  # in a subshell and throw the export away.
  _ferry_export_chatgpt_instructions 2>"$ERR"
}

child_sees() {
  zsh -fc 'print -r -- "${CHATGPT_DEFAULT_INSTRUCTIONS-<unset>}"'
}

check() {  # check <name> <UNSET | =exact | substring>
  local name="$1" want="$2" got errtext
  got="$(child_sees)"
  errtext="$(<"$ERR")"
  if [[ -n "$errtext" ]]; then
    print -r -- "FAIL $name: helper wrote to stderr: ${errtext}"
    FAILED=1
    return
  fi
  if [[ "$want" == "UNSET" ]]; then
    if [[ "$got" == "<unset>" ]]; then
      print -r -- "PASS $name"
    else
      print -r -- "FAIL $name: expected the variable to stay unset, child saw [${got}]"
      FAILED=1
    fi
    return
  fi
  if [[ "$want" == "="* ]]; then
    if [[ "$got" == "${want#=}" ]]; then
      print -r -- "PASS $name"
    else
      print -r -- "FAIL $name: expected exactly [${want#=}], child saw [${got}]"
      FAILED=1
    fi
    return
  fi
  if [[ "$got" == *"$want"* ]]; then
    print -r -- "PASS $name"
  else
    print -r -- "FAIL $name: expected the child to see [${want}], it saw [${got}]"
    FAILED=1
  fi
}

# ---- (a) the opt-out --------------------------------------------------------
for spelling in "off" "OFF" "  Off  "; do
  reset_env
  FERRY_CHATGPT_INSTRUCTIONS="$spelling"
  run_helper
  check "a-off-sentinel[$spelling]" UNSET
done

# (a2) INTERIOR whitespace is not the sentinel — the Python side only strips the
# ends, so "o f f" is a PATH there. Both must agree or the mirror is a lie.
reset_env
FERRY_CHATGPT_INSTRUCTIONS="o f f"
run_helper
check "a2-spacey-off-is-a-path-not-the-optout" "User prompt."

# ---- (b) an operator export is never overwritten ----------------------------
reset_env
export CHATGPT_DEFAULT_INSTRUCTIONS="operator text"
run_helper
check "b-operator-env-survives" "=operator text"

# (b2) an all-whitespace operator value is NOT the operator deciding anything:
# litellm's `getenv(...) or DEFAULT` would hand the model three spaces as its
# entire preamble. The Python resolver uses .strip(); so must this.
reset_env
export CHATGPT_DEFAULT_INSTRUCTIONS="   "
run_helper
check "b2-blank-operator-env-falls-through" "User prompt."

# (b3) idempotence: ferry's own export takes branch (b) on a second call.
reset_env
run_helper
run_helper
check "b3-second-call-is-a-no-op" "=User prompt."

# ---- (c) an explicit path ---------------------------------------------------
reset_env
print -r -- "Explicit prompt." > "$TMP/explicit.txt"
FERRY_CHATGPT_INSTRUCTIONS="$TMP/explicit.txt"
run_helper
check "c-explicit-path-beats-user-file" "Explicit prompt."

reset_env
print -r -- "Tilde prompt." > "$TMP/tilde.txt"
FERRY_CHATGPT_INSTRUCTIONS="~/tilde.txt"
run_helper
check "c2-tilde-is-expanded" "Tilde prompt."

reset_env
FERRY_CHATGPT_INSTRUCTIONS="$TMP/gone.txt"
run_helper
check "c3-missing-explicit-path-falls-back" "User prompt."

# ---- (d) the user file ------------------------------------------------------
reset_env
run_helper
check "d-user-file-beats-shipped-default" "=User prompt."

reset_env
print -r -- "   " > "$USER_FILE"
run_helper
check "d2-blank-user-file-is-not-an-override" "$SHIPPED_MARK"

# (d3) `-r` is true for a DIRECTORY: reading one spills
# "error when reading ...: is a directory" onto ferry's stderr. check() fails on
# ANY stderr, so this asserts the silent fall-through the Python side gives.
reset_env
rm -f "$USER_FILE"
mkdir -p "$USER_FILE"
run_helper
check "d3-a-directory-is-survived-silently" "$SHIPPED_MARK"
rmdir "$USER_FILE"

# ---- (e) the shipped default ------------------------------------------------
reset_env
rm -f "$USER_FILE"
run_helper
check "e-shipped-default-is-the-last-resort" "$SHIPPED_MARK"

# The exported text must be the shipped file's, trailing newline stripped —
# an env var carrying a stray newline lands ahead of the client's own prompt.
reset_env
rm -f "$USER_FILE"
run_helper
expected="$(<"$SHIPPED")"
check "e2-shipped-text-is-verbatim" "=$expected"

# ---- (f) nothing readable at all --------------------------------------------
reset_env
rm -f "$USER_FILE"
APP_DIR="$TMP/nowhere"
run_helper
check "f-nothing-readable-exports-nothing" UNSET

if (( FAILED )); then
  print -r -- "ZSH CHATGPT-INSTRUCTIONS MIRROR: FAILURES"
  exit 1
fi
print -r -- "ZSH CHATGPT-INSTRUCTIONS MIRROR: all checks passed"
exit 0
