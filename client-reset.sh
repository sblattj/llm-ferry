#!/bin/zsh
# client-reset.sh — re-pull the `ferry` CLI from the host and re-apply the
# opencode takeover to all three configs. For a client that is ALREADY
# bootstrapped and just needs to catch up with the host.
#
#   curl -fsSL http://<host>:<share-port>/client-reset.sh | zsh
#
# Difference from client-bootstrap.sh: this does not touch ~/.zshrc, does not
# write client.json, and does not probe/prompt for a host — it reuses the
# profile a previous bootstrap left behind. Run the bootstrap instead if this
# machine has never been set up, or if the wrapper functions need refreshing.
#
# WHY THE CLI COMES FIRST, ALWAYS: `ferry opencode` is the thing that performs
# the takeover, so an out-of-date CLI silently does the OLD thing and reports
# success either way. Updating it is not an optimisation, it is the point.

set -eu

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
saved_host=""; saved_share=""; saved_port=""
if [[ -f "$CLIENT_JSON" ]]; then
  saved=$(python3 - "$CLIENT_JSON" <<'PYEOF'
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    c = {}
print(f"{c.get('host','')}\t{c.get('share_port','')}\t{c.get('port','')}")
PYEOF
)
  saved_host="${saved%%$'\t'*}"
  rest="${saved#*$'\t'}"
  saved_share="${rest%%$'\t'*}"
  saved_port="${rest##*$'\t'}"
fi

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
echo ">>> Re-applying the opencode takeover..."
RESET_FAILED=0
for oc_target in \
  "$HOME/.config/opencode/opencode.json|" \
  "$HOME/.config/ferry/opencode-cloud.json|" \
  "$HOME/.config/ferry/opencode-local.json|--local"
do
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
echo "The previous configs are kept beside each file as <name>.<UTC>.jsonc."
echo "Shell wrappers (opencode / opencode-cloud / opencode-local) are NOT touched"
echo "by a reset — re-run client-bootstrap.sh if those need refreshing."
echo "================================================================="
