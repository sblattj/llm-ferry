#!/bin/zsh
# client-reset.sh — re-pull the `ferry` CLI from the host and re-apply the
# opencode takeover to all three configs. For a client that is ALREADY
# bootstrapped and just needs to catch up with the host.
#
#   curl -fsSL http://<host>:<share-port>/client-reset.sh | zsh
#   curl -fsSL http://<host>:<share-port>/client-reset.sh | zsh -s -- --profiles-only
#
# Difference from client-bootstrap.sh: this does not touch ~/.zshrc, does not
# write client.json, and does not probe/prompt for a host — it reuses the
# profile a previous bootstrap left behind. Run the bootstrap instead if this
# machine has never been set up, or if the wrapper functions need refreshing.
#
# WHY THE CLI COMES FIRST, ALWAYS: `ferry opencode` is the thing that performs
# the takeover, so an out-of-date CLI silently does the OLD thing and reports
# success either way. Updating it is not an optimisation, it is the point.
#
# HOW WIDE THE TAKEOVER GOES is not this script's decision to re-make. It reads
# "opencode_mode" out of client.json — written by the bootstrap that set this
# machine up — and re-applies exactly that scope:
#
#   full (or the key absent, i.e. a pre-1.10.0 profile)
#                    all three configs, opencode's own default included.
#   profiles         only ~/.config/ferry/opencode-{cloud,local}.json.
#                    ~/.config/opencode is not read or written.
#   none             no config is written at all; the CLI is still re-pulled,
#                    which is the other half of what a reset is for.
#
# A flag overrides the saved mode for THIS RUN only — client.json is never
# rewritten here, so an override cannot silently redefine the machine.

set -eu

# --- Flags ------------------------------------------------------------------
OC_MODE_OVERRIDE=""
for arg in "$@"; do
  case "$arg" in
    --no-opencode)   OC_MODE_OVERRIDE="none" ;;
    --profiles-only) OC_MODE_OVERRIDE="profiles" ;;
    --full-opencode) OC_MODE_OVERRIDE="full" ;;
    -h|--help)
      sed -n '2,30p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'
      echo ""
      echo "Flags: --profiles-only | --no-opencode | --full-opencode | --help"
      exit 0 ;;
    *) echo "Unknown flag: $arg (want: --profiles-only, --no-opencode, --full-opencode, --help)"; exit 1 ;;
  esac
done

# Rewritten by the host's share server (see cmd_share in lib/ferry-share.zsh).
# If you fetched this some other way the placeholders survive and we fall back
# to the profile a previous bootstrap wrote.
HOST_NAME="${HOST_NAME:-HOST_MDNS_PLACEHOLDER}"
SHARE_PORT="${SHARE_PORT:-SHARE_PORT_PLACEHOLDER}"
# The INFERENCE port, not the share port. Only the share port is injected (the
# share server is the thing serving this file), so this comes from the profile
# or the default.
HOST_PORT="${HOST_PORT:-}"

CLIENT_JSON="$HOME/.config/ferry/client.json"
FERRY_BIN="$HOME/.local/bin/ferry"

echo "================================================================="
echo "                   LLM-FERRY CLIENT RESET"
echo "================================================================="

# --- Resolve the host -------------------------------------------------------
# Injected value wins; otherwise the last bootstrap's profile. We never prompt:
# a reset is a catch-up on a machine that has already been set up, so if there
# is no profile the right answer is the bootstrap, not an interactive guess.
saved_host=""; saved_share=""; saved_port=""; saved_mode=""
if [[ -f "$CLIENT_JSON" ]]; then
  saved=$(python3 - "$CLIENT_JSON" <<'PYEOF'
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    c = {}
print(f"{c.get('host','')}\t{c.get('share_port','')}\t{c.get('port','')}\t{c.get('opencode_mode','')}")
PYEOF
)
  # Peel the fields off one at a time. A `##*\t` shortcut for the last field
  # breaks the moment a field is added after it, which is how this one grew.
  saved_host="${saved%%$'\t'*}";  rest="${saved#*$'\t'}"
  saved_share="${rest%%$'\t'*}";  rest="${rest#*$'\t'}"
  saved_port="${rest%%$'\t'*}";   rest="${rest#*$'\t'}"
  saved_mode="${rest%%$'\t'*}"
fi

# The override is per-run; the profile is the default; a profile written before
# opencode_mode existed means the machine was set up with the full takeover.
OC_MODE="${OC_MODE_OVERRIDE:-${saved_mode:-full}}"
case "$OC_MODE" in
  full|profiles|none) ;;
  *) echo "Error: unrecognised opencode_mode '$OC_MODE' in $CLIENT_JSON."
     echo "       Expected full, profiles or none. Pass --full-opencode /"
     echo "       --profiles-only / --no-opencode to override for this run."
     exit 1 ;;
esac

[[ "$HOST_NAME"  == "HOST_MDNS_PLACEHOLDER"  ]] && HOST_NAME="$saved_host"
[[ "$SHARE_PORT" == "SHARE_PORT_PLACEHOLDER" ]] && SHARE_PORT="${saved_share:-8095}"
HOST_PORT="${HOST_PORT:-${saved_port:-8090}}"

if [[ -z "$HOST_NAME" ]]; then
  echo "Error: no host injected and none in $CLIENT_JSON."
  echo "       This machine has not been bootstrapped. Run:"
  echo "         curl -fsSL http://<host>:8095/client-bootstrap.sh | zsh"
  exit 1
fi
echo "Endpoint: http://$HOST_NAME:$HOST_PORT   Share: http://$HOST_NAME:$SHARE_PORT"
case "$OC_MODE" in
  full)     echo "opencode scope: FULL (opencode's own config + both ferry profiles)" ;;
  profiles) echo "opencode scope: PROFILES ONLY (~/.config/opencode untouched)" ;;
  none)     echo "opencode scope: NONE (CLI re-pull only)" ;;
esac
[[ -n "$OC_MODE_OVERRIDE" ]] && echo "                (flag override for this run; client.json is not rewritten)"
echo "================================================================="

# --- 1. Re-pull the CLI -----------------------------------------------------
# Download to a temp file and VALIDATE before overwriting. A share server that
# is down, or a reverse proxy serving an error page, otherwise replaces a
# working ferry with an HTML 404 that fails on every later invocation — and the
# failure surfaces far from its cause.
echo ">>> Re-pulling the 'ferry' CLI..."
mkdir -p "$HOME/.local/bin"
tmp_ferry="$(mktemp)"
trap 'rm -f "$tmp_ferry"' EXIT

if ! curl -fsSL -m 30 "http://$HOST_NAME:$SHARE_PORT/ferry" -o "$tmp_ferry"; then
  echo "    Error: could not download the CLI from http://$HOST_NAME:$SHARE_PORT/ferry"
  echo "           Is 'ferry share' running on the host?"
  exit 1
fi

# Three cheap checks, each catching a different way the download can be wrong:
# an error page (no shebang), a truncated transfer (missing the function we are
# about to call), and a corrupt one (syntax).
if ! head -1 "$tmp_ferry" | grep -q '^#!'; then
  echo "    Error: downloaded file is not a script (an error page?). Keeping the existing CLI."
  exit 1
fi
if ! grep -q 'cmd_opencode()' "$tmp_ferry"; then
  echo "    Error: downloaded CLI has no cmd_opencode — truncated? Keeping the existing CLI."
  exit 1
fi
if ! zsh -n "$tmp_ferry" 2>/dev/null; then
  echo "    Error: downloaded CLI fails a syntax check. Keeping the existing CLI."
  exit 1
fi

mv "$tmp_ferry" "$FERRY_BIN"
trap - EXIT
chmod +x "$FERRY_BIN"
echo "    \033[1;32mCLI updated: $FERRY_BIN\033[0m"

# --- 2. Re-apply the opencode takeover --------------------------------------
# Same three targets the bootstrap writes: opencode's own default config (for a
# bare `command opencode` or a non-zsh shell) plus the two ferry profiles the
# wrapper functions select between. Each is snapshotted to <name>.<UTC>.jsonc
# before it is written, so this is reversible.
echo ""
RESET_FAILED=0
if [[ "$OC_MODE" == "none" ]]; then
  echo ">>> opencode scope is 'none' — no config written."
  echo "    The CLI above is up to date, which is the whole reset for this machine."
  echo "    Widen it with:  client-reset.sh --profiles-only   (or --full-opencode)"
  oc_targets=()
else
  echo ">>> Re-applying the opencode takeover..."
  oc_targets=(
    "$HOME/.config/ferry/opencode-cloud.json|"
    "$HOME/.config/ferry/opencode-local.json|--local"
  )
  # opencode's OWN config joins the list only in full scope — see the header.
  if [[ "$OC_MODE" == "full" ]]; then
    oc_targets=("$HOME/.config/opencode/opencode.json|" "${oc_targets[@]}")
  fi
fi
for oc_target in "${oc_targets[@]}"; do
  oc_path="${oc_target%%|*}"
  oc_flag="${oc_target#*|}"
  echo "    -> $oc_path"
  # --host/--port are passed EXPLICITLY rather than left to the CLI's own
  # profile lookup. Without them, a ferry that cannot find a client profile
  # decides it must be running ON the host and wires the config to 127.0.0.1 —
  # which on a client points opencode at itself and fails every request. Caught
  # by running this script against a scratch HOME, where exactly that happened.
  #
  # env -u OPENCODE_CONFIG: `ferry opencode` honours that variable as its
  # default target, so a shell that exports one would redirect all three writes
  # onto the same file.
  if ! env -u OPENCODE_CONFIG "$FERRY_BIN" opencode \
        --host "$HOST_NAME" --port "$HOST_PORT" --config "$oc_path" $oc_flag; then
    RESET_FAILED=1
  fi
done

echo ""
echo "================================================================="
if [[ $RESET_FAILED -eq 1 ]]; then
  echo "\033[1;31mDONE WITH ERRORS\033[0m — at least one config was not written."
  echo "Check that the host's endpoint is reachable, then re-run."
  exit 1
fi
echo "\033[1;32mRESET COMPLETE\033[0m"
if [[ "$OC_MODE" != "none" ]]; then
  echo "The previous configs are kept beside each file as <name>.<UTC>.jsonc."
fi
if [[ "$OC_MODE" == "profiles" ]]; then
  echo "Scope was PROFILES ONLY: ~/.config/opencode/opencode.json was not read or written,"
  echo "so bare 'opencode' still uses this machine's own config."
fi
echo "Shell wrappers (opencode / opencode-cloud / opencode-local) are NOT touched"
echo "by a reset — re-run client-bootstrap.sh if those need refreshing."
echo "================================================================="
