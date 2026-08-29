# ----------------- UPDATE -----------------
# `ferry update` — catch this machine up, whichever end of the wire it is on.
#
# It owns NO update logic. Both catch-up paths already existed and are the
# tested ones; what was missing was a single command that picks the right one,
# so nobody has to remember which half of the stack they are standing on:
#
#   host    ->  ./host-reset.sh      rebuild the CLI from lib/, re-link it,
#                                    validate the route config, bounce the proxy
#   client  ->  curl .../client-reset.sh | zsh
#                                    re-pull the CLI from the host, re-apply the
#                                    opencode takeover
#
# They are deliberately NOT mirrors of each other (see host-reset.sh's header): a
# client is stale because the HOST has a newer CLI to download, while a host is
# stale because its own `ferry` drifted from its own lib/. Same word, two
# different repairs — which is exactly why guessing wrong is easy and why this
# command exists.
#
# ROLE DETECTION IS NOT NEW HERE. ferry has always decided host-vs-client on the
# presence of ~/.config/ferry/client.json (ferry-core.zsh sets CLIENT_MODE from
# it, and ferry-hostreset.test.py pins that rule). This reuses that flag rather
# than inventing a second, divergent notion of role.
cmd_update() {
  local dry_run=0 full=0 forced=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      --full)    full=1; shift ;;
      --host)    forced="host"; shift ;;
      --client)  forced="client"; shift ;;
      -h|--help)
        echo "Usage: ferry update [--full] [--host|--client] [--dry-run]"
        echo
        echo "  Catches this machine up. Detects host vs client from"
        echo "  ~/.config/ferry/client.json; --host/--client override it."
        echo "  --full    [Host] also reload the GPU lanes (minutes, not seconds)"
        echo "  --dry-run print the command that would run, and stop"
        return 0
        ;;
      *)
        echo "Error: unknown option for 'ferry update': $1" >&2
        echo "       ferry update [--full] [--host|--client] [--dry-run]" >&2
        return 1
        ;;
    esac
  done

  local role
  if [[ -n "$forced" ]]; then
    role="$forced"
  elif (( CLIENT_MODE )); then
    role="client"
  else
    role="host"
  fi

  local cmd
  if [[ "$role" == "client" ]]; then
    # --full reloads the GPU lanes, which live on the host. Refusing beats
    # silently dropping the flag: a user who passed it believes something extra
    # happened, and on a client nothing extra can.
    if (( full )); then
      echo "Error: --full applies to the host (it reloads the GPU lanes)." >&2
      echo "       A client has none; drop the flag." >&2
      return 1
    fi
    # CLIENT_HOST is empty both when there is no profile at all (--client forced
    # on a host) and when the profile omits `host`. Either way there is nothing
    # to curl, and an unguarded template would dial the literal "http:///".
    if [[ -z "$CLIENT_HOST" ]]; then
      echo "Error: no host to update from — ~/.config/ferry/client.json is" >&2
      echo "       missing or has no 'host' key." >&2
      echo "       Bootstrap this client first:" >&2
      echo "         curl -fsSL http://<host>:8095/client-bootstrap.sh | zsh" >&2
      return 1
    fi
    cmd="curl -fsSL http://$CLIENT_HOST:$CLIENT_SHARE_PORT/client-reset.sh | zsh"
  else
    local reset="$APP_DIR/host-reset.sh"
    if [[ ! -f "$reset" ]]; then
      echo "Error: host-reset.sh not found next to the CLI ($reset)." >&2
      echo "       On a host, ~/.local/bin/ferry should symlink into the checkout." >&2
      return 1
    fi
    cmd="zsh $reset"
    (( full )) && cmd="$cmd --full"
  fi

  if (( dry_run )); then
    echo ">>> ferry update — detected role: $role"
    echo "    would run: $cmd"
    return 0
  fi

  echo ">>> ferry update — $role mode"
  eval "$cmd"
}
