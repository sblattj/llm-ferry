#!/bin/zsh
# client-to-host.sh — migrate THIS machine from a ferry CLIENT into a ferry HOST.
#
# The inverse direction of client-bootstrap.sh: that turns a fresh laptop into a
# client of some OTHER machine's host; this promotes a machine that has been a
# client into a host of its own. Use it on a box that used to point at someone
# else's ferry and now needs to serve the LAN itself.
#
#   ./client-to-host.sh                 # migrate (asks once before it starts)
#   ./client-to-host.sh --dry-run       # print every step, change nothing
#   ./client-to-host.sh --yes           # skip the confirmation prompt
#   ./client-to-host.sh --full          # also reload the GPU lanes at the end
#   ./client-to-host.sh --pull          # let host-reset git-pull the checkout first
#
# WHAT IT DOES, in order. Each step reuses the repo's own tested engines rather
# than re-implementing them, so this script owns only the client->host TRANSITION:
#
#   1. Confirm this machine is a client (~/.config/ferry/client.json present).
#   2. Carry the client's master_key (if any) forward into
#      ~/.config/ferry/secrets.env as LITELLM_MASTER_KEY, so the new host serves
#      the same key the box was already using and its rewired opencode/claude
#      configs keep working. A key already in secrets.env always wins.
#   3. Seed ~/.config/ferry/litellm.yaml from litellm-route-example.yaml if it is
#      not there yet (host-reset.sh requires the route config to exist).
#   4. Provision host dependencies via 'ferry install' from the checkout, which
#      is OS-aware (uv + litellm everywhere; on macOS also mlx-vlm and the
#      ~16.6GB default models, skipped on Linux), installs the host's own shell
#      wrappers pointed at 127.0.0.1, and symlinks ferry into this checkout.
#   5. Archive ~/.config/ferry/client.json to client.json.pre-migrate.<UTC>. This
#      single move is what flips ferry out of CLIENT_MODE (see lib/ferry-core.zsh),
#      and it is reversible — move the file back to become a client again.
#   6. Bring the host online via host-reset.sh: validate the route config,
#      re-link the CLI, bounce the proxy + share server, re-apply the opencode
#      takeover and the claude wrappers pointed at 127.0.0.1, and verify the lanes.
#   7. Propagate the front-door master key into the host's OWN opencode/claude
#      configs. host-reset wires them keyless (it reads the bearer from the
#      client.json we just archived), so on a keyed front door they would 401;
#      this re-wires them with the key so the host's own tools keep working.
#
# This script must run from inside an llm-ferry CHECKOUT (it needs
# host-bootstrap.sh, host-reset.sh, the route template and lib/). A machine
# bootstrapped as a client has only the single-file `ferry` CLI and no checkout,
# so `ferry migrate` clones one first and then runs this; run this directly only
# when you already have the repo.

set -eu

APP_DIR="${0:A:h}"
cd "$APP_DIR"

DRY_RUN=0
ASSUME_YES=0
FULL=0
DO_PULL=0   # default: reset onto the checkout exactly as it stands (host-reset
            # --no-pull). --pull opts into host-reset's git fast-forward, which
            # refuses on a dirty or diverged tree.

usage() {
  cat <<EOF
Usage: ./client-to-host.sh [options]

  Migrate THIS machine from a ferry client into a ferry host.

  --dry-run   Print every action and change nothing. Run it first.
  --yes, -y   Skip the confirmation prompt.
  --full      Pass --full to host-reset.sh (also reload the GPU lanes; minutes).
  --pull      Let host-reset.sh git-pull the checkout first (default: use the
              tree as-is, so a dirty dev checkout is never touched).
  --help, -h  This message.
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -y|--yes)  ASSUME_YES=1; shift ;;
    --full)    FULL=1; shift ;;
    --pull)    DO_PULL=1; shift ;;
    --no-pull) DO_PULL=0; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown flag: $1 (want: --dry-run, --yes, --full, --pull, --help)"; exit 1 ;;
  esac
done

say()  { echo "$@"; }
ok()   { echo "    \033[1;32m$*\033[0m"; }
warn() { echo "    \033[1;33m$*\033[0m"; }
die()  { echo "    \033[1;31mError: $*\033[0m" >&2; exit 1; }

# run/do: under --dry-run print instead of execute. Arguments are literals from
# this script, exactly like client-cleanup.sh's helper of the same name.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "    [dry-run] $*"
  else
    "$@"
  fi
}

echo "================================================================="
echo "               LLM-FERRY  CLIENT  ->  HOST  MIGRATION"
[[ $DRY_RUN -eq 1 ]] && echo "                     (DRY RUN — nothing changes)"
echo "================================================================="

# --- 0. Preconditions: this must be a real checkout -------------------------
for f in ferry host-reset.sh litellm-route-example.yaml lib; do
  [[ -e "$APP_DIR/$f" ]] || die "this is not an llm-ferry checkout (missing $f in $APP_DIR).
       Clone the repo and run this from inside it, or use 'ferry migrate' which clones for you."
done

# --- 1. Confirm this machine is actually a client ---------------------------
# ferry decides host-vs-client purely on this file (CLIENT_MODE in
# lib/ferry-core.zsh). No file => nothing to migrate.
CLIENT_JSON="$HOME/.config/ferry/client.json"
if [[ ! -f "$CLIENT_JSON" ]]; then
  # A leftover archive means the migration already ran (or a previous run got
  # past the pivot in step 6 but host-reset in step 7 did not finish). (N) is
  # zsh's null-glob qualifier: no match yields an empty array, not a nomatch error.
  archives=( "$CLIENT_JSON".pre-migrate.*(N) )
  if (( ${#archives} )); then
    echo "This machine already left client mode — a pre-migrate archive exists:"
    printf '    %s\n' "${archives[@]}"
    echo "If the host is already serving, you are done — check with:  ferry status"
    echo "If a previous migration stopped before the host came up, FINISH it (not"
    echo "re-migrate) from this checkout:"
    echo "    zsh $APP_DIR/host-reset.sh --no-pull      # bring the endpoint up + rewire"
    echo "    ferry status                              # then verify"
    echo "To go back to being a client instead:"
    echo "    mv '${archives[1]}' '$CLIENT_JSON'"
  else
    echo "This machine has no client profile ($CLIENT_JSON) — it is not a ferry client,"
    echo "so there is nothing to migrate."
    echo "To provision this box as a host from scratch instead:"
    echo "    zsh $APP_DIR/ferry install   &&   ferry up   (seeds the route config, then re-run)"
  fi
  exit 0
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SECRETS="$HOME/.config/ferry/secrets.env"
ROUTE_CONFIG="$HOME/.config/ferry/litellm.yaml"
TEMPLATE="$APP_DIR/litellm-route-example.yaml"
IS_MAC=0
[[ "$(uname -s)" == "Darwin" ]] && IS_MAC=1

# --- 2. Confirm with the operator -------------------------------------------
if [[ $DRY_RUN -eq 0 && $ASSUME_YES -eq 0 ]]; then
  echo ""
  echo "This will turn THIS machine into a ferry host:"
  echo "  - carry the client's master key (if any) into $SECRETS"
  echo "  - seed $ROUTE_CONFIG from the template if absent"
  if [[ $IS_MAC -eq 1 ]]; then
    echo "  - run 'ferry install': install uv + litellm + mlx-vlm, and DOWNLOAD"
    echo "    the ~16.6GB default local models (this is the slow part)"
  else
    echo "  - run 'ferry install': install uv + litellm (Linux: no local GPU models)"
  fi
  echo "  - archive $CLIENT_JSON (reversible) so ferry leaves CLIENT_MODE"
  echo "  - run host-reset.sh: bounce the proxy + share server and rewire"
  echo "    opencode/claude at 127.0.0.1"
  echo ""
  printf "Proceed? [y/N]: "
  ans=""
  read ans < /dev/tty || ans=""
  case "$ans" in
    y*|Y*) ;;
    *) echo "Aborted. (Re-run with --dry-run to preview, or --yes to skip this prompt.)"; exit 1 ;;
  esac
fi

# --- 3. Carry the client's master key forward -------------------------------
# The client profile may hold the shared front-door key of the host it used. A
# new host that reuses it keeps its rewired opencode/claude configs valid and
# lets any laptops that will point at THIS box authenticate the same way. A key
# already present in secrets.env always wins; the value is never printed.
echo ""
say ">>> Carrying the client's master key forward (if any)..."
CLIENT_KEY="$(python3 -c "import json,os;print((json.load(open(os.path.expanduser('$CLIENT_JSON'))).get('master_key') or ''))" 2>/dev/null || true)"
if [[ -z "$CLIENT_KEY" ]]; then
  ok "no master_key in client.json — host-reset will generate one if the route config needs it."
elif [[ -f "$SECRETS" ]] && grep -qE '^[[:space:]]*(export[[:space:]]+)?LITELLM_MASTER_KEY=' "$SECRETS" 2>/dev/null; then
  ok "secrets.env already defines LITELLM_MASTER_KEY — keeping it, ignoring the client's."
elif [[ $DRY_RUN -eq 1 ]]; then
  echo "    [dry-run] would append the client master_key to $SECRETS as LITELLM_MASTER_KEY (0600)"
else
  mkdir -p "$(dirname "$SECRETS")"
  [[ -f "$SECRETS" ]] || : > "$SECRETS"
  {
    echo ""
    echo "# Carried from client.json by client-to-host.sh $STAMP: front-door master_key."
    echo "export LITELLM_MASTER_KEY=$CLIENT_KEY"
  } >> "$SECRETS"
  chmod 600 "$SECRETS"
  unset CLIENT_KEY
  ok "carried the client's master key into $SECRETS (0600); the value is not printed."
fi

# --- 4. Seed the route config -----------------------------------------------
# host-reset.sh requires ~/.config/ferry/litellm.yaml to exist; on a host it is
# `ferry up` that seeds it from the template. Do the same copy here so the reset
# has something to validate.
echo ""
say ">>> Seeding the route config..."
if [[ -f "$ROUTE_CONFIG" ]]; then
  ok "route config already present ($ROUTE_CONFIG) — keeping it."
else
  run mkdir -p "$(dirname "$ROUTE_CONFIG")"
  run cp "$TEMPLATE" "$ROUTE_CONFIG"
  ok "seeded $ROUTE_CONFIG from litellm-route-example.yaml — edit model ids/keys before serving."
fi

# --- 5. Provision host dependencies -----------------------------------------
# 'ferry install' from the checkout is used rather than host-bootstrap.sh
# because it is OS-aware: host-bootstrap.sh unconditionally installs mlx-vlm and
# the Apple-only models, which fails under set -eu on Linux, whereas cmd_install
# guards those behind IS_MAC. It also installs the host's shell wrappers at
# 127.0.0.1 and symlinks ferry into this checkout (its ${0:A} is this ferry).
# Invoked through zsh so a checkout that lost ferry's exec bit still runs.
echo ""
say ">>> Provisioning host dependencies ('ferry install')..."
if [[ $DRY_RUN -eq 1 ]]; then
  if [[ $IS_MAC -eq 1 ]]; then
    echo "    [dry-run] zsh $APP_DIR/ferry install"
    echo "              (installs uv + litellm + mlx-vlm, downloads ~16.6GB models,"
    echo "               installs host wrappers at 127.0.0.1, symlinks ~/.local/bin/ferry here)"
  else
    echo "    [dry-run] zsh $APP_DIR/ferry install"
    echo "              (installs uv + litellm, installs host wrappers at 127.0.0.1,"
    echo "               symlinks ~/.local/bin/ferry here; local GPU serving is macOS-only)"
  fi
else
  zsh "$APP_DIR/ferry" install
fi

# --- 6. Archive the client identity -----------------------------------------
# This is the pivot: the move flips ferry out of CLIENT_MODE. Do it AFTER the
# bootstrap so a failed install leaves the machine a recoverable client, and
# BEFORE host-reset, which refuses to run while client.json is present.
echo ""
say ">>> Retiring the client profile..."
ARCHIVE="$CLIENT_JSON.pre-migrate.$STAMP"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "    [dry-run] mv $CLIENT_JSON $ARCHIVE"
  echo "              (this is what leaves CLIENT_MODE; move it back to revert)"
else
  mv "$CLIENT_JSON" "$ARCHIVE"
  ok "archived the client profile -> $ARCHIVE"
  ok "revert any time with:  mv '$ARCHIVE' '$CLIENT_JSON'"
fi

# --- 7. Bring the host online -----------------------------------------------
echo ""
say ">>> Bringing the host online (host-reset.sh)..."
reset_flags=()
(( DO_PULL )) || reset_flags+=(--no-pull)
(( FULL ))    && reset_flags+=(--full)
if [[ $DRY_RUN -eq 1 ]]; then
  echo "    [dry-run] zsh $APP_DIR/host-reset.sh ${reset_flags[*]}"
  echo "              (validate route config, re-link ferry, bounce proxy + share,"
  echo "               rewire opencode/claude at 127.0.0.1, verify the lanes)"
else
  zsh "$APP_DIR/host-reset.sh" "${reset_flags[@]}"
fi

# --- 7b. Propagate the front-door key to the host's OWN configs -------------
# host-reset's step 6 rewires opencode/claude at 127.0.0.1, but resolves the
# bearer from CLIENT_MASTER_KEY (loaded from client.json), which we archived in
# step 6 above — so its calls bake the 'local' placeholder. The shipped template
# gates the front door on LITELLM_MASTER_KEY (and host-reset generates one if it
# is unset), so 'local' would 401 on every local request. Read the key back and
# re-wire opencode + claude WITH it. Keyless hosts (no LITELLM_MASTER_KEY) skip
# this entirely. The value is passed to ferry but never printed here.
PORT_FOR_KEY="${FERRY_PORT:-8090}"
OC_DEFAULT="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.json}"
echo ""
say ">>> Matching the host's own opencode/claude bearer to the front door..."
if [[ $DRY_RUN -eq 1 ]]; then
  echo "    [dry-run] if $SECRETS defines LITELLM_MASTER_KEY: re-run 'ferry opencode --key ...'"
  echo "              for the default + cloud/local/super profiles and 'ferry claude --wrappers --key ...',"
  echo "              all at http://127.0.0.1:$PORT_FOR_KEY (value never printed)"
else
  HOSTKEY="$(grep -E '^[[:space:]]*(export[[:space:]]+)?LITELLM_MASTER_KEY=' "$SECRETS" 2>/dev/null \
               | tail -1 \
               | sed -E 's/^[[:space:]]*(export[[:space:]]+)?LITELLM_MASTER_KEY=//; s/^["'\'']//; s/["'\'']$//' \
             || true)"
  if [[ -z "$HOSTKEY" ]]; then
    ok "front door is keyless (no LITELLM_MASTER_KEY) — nothing to re-key."
  else
    for spec in \
      "$OC_DEFAULT|" \
      "$HOME/.config/ferry/opencode-cloud.json|" \
      "$HOME/.config/ferry/opencode-local.json|--local" \
      "$HOME/.config/ferry/opencode-super.json|--super"; do
      cfg="${spec%%|*}"; flag="${spec#*|}"
      FERRY_GOAL_SKILL_QUIET=1 env -u OPENCODE_CONFIG "$APP_DIR/ferry" opencode \
        --host 127.0.0.1 --port "$PORT_FOR_KEY" --key "$HOSTKEY" --config "$cfg" $flag >/dev/null 2>&1 \
        || warn "could not re-key $cfg"
    done
    "$APP_DIR/ferry" claude --wrappers --host 127.0.0.1 --port "$PORT_FOR_KEY" --key "$HOSTKEY" >/dev/null 2>&1 \
      || warn "could not re-key the claude wrappers"
    unset HOSTKEY
    ok "opencode + claude now carry the front-door key (value not printed)."
  fi
fi

# --- 8. Report --------------------------------------------------------------
echo ""
echo "================================================================="
if [[ $DRY_RUN -eq 1 ]]; then
  echo "DRY RUN COMPLETE — re-run without --dry-run to apply."
  echo "================================================================="
  exit 0
fi
echo "\033[1;32mMIGRATION COMPLETE — this machine is now a ferry host.\033[0m"
echo "  Client profile archived at: $ARCHIVE"
echo "  Master key (if the route config gates it): read it from $SECRETS"
echo ""
echo "Next:"
echo "    ferry status                 # per-lane health and served lane names"
echo "    ferry up                     # serve the stack on :8090 (edit litellm.yaml first)"
echo "    ferry share                  # advertise client-bootstrap.sh over the LAN (:8095)"
echo "Open a NEW terminal so the host shell wrappers load."
echo "To go back to being a client:  mv '$ARCHIVE' '$CLIENT_JSON'"
echo "================================================================="
