# ----------------- AUTOSTART -----------------
# `ferry autostart` / `ferry boot` — bring a host back by itself after a reboot.
#
# Nothing used to restart the endpoint, the share server or the relay: all three
# are nohup'd background processes. `ferry autostart install` writes a per-user
# LaunchAgent whose only job is to run `ferry boot` at login; `ferry boot` is
# idempotent (it starts only what is not already listening), so running it again
# by hand, or on every bootstrap, is harmless.
#
# The plist sets AbandonProcessGroup because `ferry up/share/relay` leave their
# nohup'd children in the job's process group. Without it launchd kills them the
# moment `ferry boot` exits.

FERRY_AUTOSTART_LABEL="com.llm-ferry.host"

_ferry_autostart_usage() {
  cat <<'USAGE'
Usage: ferry autostart install [--quiet] [--dry-run]
       ferry autostart uninstall
       ferry autostart status
       ferry boot [--dry-run]
USAGE
}

_ferry_autostart_plist_path() {
  print -r -- "${FERRY_LAUNCHAGENTS_DIR:-$HOME/Library/LaunchAgents}/${FERRY_AUTOSTART_LABEL}.plist"
}

_ferry_autostart_log_path() {
  print -r -- "$HOME/.config/ferry/boot.log"
}

# The ferry the job runs: the stable link when it exists, never a worktree path.
_ferry_autostart_ferry_path() {
  if [[ -x "$HOME/.local/bin/ferry" ]]; then
    print -r -- "$HOME/.local/bin/ferry"
  else
    print -r -- "$FERRY_BIN_PATH"
  fi
}

# Stable PATH for the job: canonical dirs first (deterministic bytes, never a
# temporary venv from the installing shell), then the dirs of python3, litellm
# and uv when they resolve elsewhere. Deduped, order kept.
_ferry_autostart_job_path() {
  local -a dirs=("$HOME/.local/bin" /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin /usr/bin /bin /usr/sbin /sbin)
  local tool bin d
  for tool in python3 litellm uv; do
    bin="$(command -v "$tool" 2>/dev/null)" || bin=""
    [[ -n "$bin" && "$bin" == /* ]] || continue
    d="${bin:h}"
    (( ${dirs[(Ie)$d]} )) || dirs+=("$d")
  done
  print -r -- "${(j.:.)dirs}"
}

# Refuse a ferry without cmd_boot (stale stable link); warn on a linked worktree.
_ferry_autostart_check_ferry() {
  local f real dir gd gc
  f="$(_ferry_autostart_ferry_path)"
  real="${f:A}"
  if [[ ! -f "$real" ]] || ! grep -q 'cmd_boot()' "$real" 2>/dev/null; then
    echo "ferry autostart: $f ($real) has no 'boot' command; the stable link points at an older ferry. Run 'ferry update --host' first." >&2
    return 1
  fi
  dir="${real:h}"
  gd="$(git -C "$dir" rev-parse --git-dir 2>/dev/null)" || gd=""
  gc="$(git -C "$dir" rev-parse --git-common-dir 2>/dev/null)" || gc=""
  if [[ -n "$gd" && -n "$gc" && "${gd:A}" != "${gc:A}" ]]; then
    echo "warning: autostart points at a git worktree ($real); it breaks if that worktree is removed" >&2
  fi
  return 0
}

# Print the plist XML for this machine to stdout.
_ferry_autostart_render_plist() {
  python3 - "$FERRY_AUTOSTART_LABEL" "$(_ferry_autostart_ferry_path)" "$(_ferry_autostart_job_path)" "$HOME" "$(_ferry_autostart_log_path)" <<'PY'
import os, plistlib, sys
label, ferry, path, home, log = sys.argv[1:6]
env = {"PATH": path, "HOME": home}
# launchd does not see the installing shell's env: carry the host's settings.
for k in ("FERRY_SHARE_PORT", "FERRY_RELAY_PORT", "FERRY_LOCAL", "FERRY_SHARE_RELAY", "TMPDIR"):
    if os.environ.get(k):
        env[k] = os.environ[k]
d = {
    "Label": label,
    "ProgramArguments": [ferry, "boot"],
    "RunAtLoad": True,
    "KeepAlive": False,
    "AbandonProcessGroup": True,
    "ProcessType": "Interactive",
    "EnvironmentVariables": env,
    "StandardOutPath": log,
    "StandardErrorPath": log,
}
sys.stdout.buffer.write(plistlib.dumps(d))
PY
}

cmd_autostart() {
  local sub="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "$sub" in
    install)   _ferry_autostart_install "$@" ;;
    uninstall) _ferry_autostart_uninstall "$@" ;;
    status)    _ferry_autostart_status "$@" ;;
    ""|-h|--help|help) _ferry_autostart_usage ;;
    *) echo "ferry autostart: unknown subcommand '$sub'" >&2; _ferry_autostart_usage >&2; return 2 ;;
  esac
}

_ferry_autostart_install() {
  local quiet=0 dry_run=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --quiet)   quiet=1; shift ;;
      --dry-run) dry_run=1; shift ;;
      *) echo "ferry autostart install: unknown option '$1'" >&2; return 2 ;;
    esac
  done
  if (( CLIENT_MODE )); then
    echo "ferry autostart: this machine is a client (~/.config/ferry/client.json exists); autostart is for hosts only." >&2
    return 1
  fi
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ferry autostart: macOS only (it installs a launchd LaunchAgent)." >&2
    return 1
  fi

  local lc="${FERRY_LAUNCHCTL:-launchctl}" plist uid dom xml rc=0
  plist="$(_ferry_autostart_plist_path)"
  uid="$(id -u)"
  dom="gui/$uid"
  _ferry_autostart_check_ferry || return 1
  xml="$(_ferry_autostart_render_plist)" || rc=$?
  if (( rc != 0 )) || [[ -z "$xml" ]]; then
    echo "ferry autostart: could not generate the plist (python3 missing?)" >&2
    return 1
  fi

  if (( dry_run )); then
    echo "# would write $plist:"
    print -r -- "$xml"
    echo "# would run: $lc print $dom/$FERRY_AUTOSTART_LABEL   (skip the rest if the plist is unchanged and loaded)"
    echo "# would run: $lc bootout $dom/$FERRY_AUTOSTART_LABEL"
    echo "# would run: $lc bootstrap $dom $plist"
    return 0
  fi

  mkdir -p "${plist:h}" "$HOME/.config/ferry" || return 1
  local tmp="${plist}.tmp.$$"
  print -r -- "$xml" > "$tmp" || { rm -f "$tmp"; return 1; }
  if [[ -f "$plist" ]] && cmp -s "$tmp" "$plist" && "$lc" print "$dom/$FERRY_AUTOSTART_LABEL" >/dev/null 2>&1; then
    rm -f "$tmp"
    (( quiet )) || echo "ferry autostart: already installed ($plist)"
    return 0
  fi
  mv -f "$tmp" "$plist" || { rm -f "$tmp"; return 1; }
  "$lc" bootout "$dom/$FERRY_AUTOSTART_LABEL" >/dev/null 2>&1 || true
  # bootout is async: a bootstrap right after it can fail transiently (rc 5).
  local attempt=1
  while (( attempt <= 3 )); do
    rc=0
    "$lc" bootstrap "$dom" "$plist" >/dev/null 2>&1 || rc=$?
    (( rc == 0 )) && break
    (( attempt < 3 )) && sleep 1
    attempt=$(( attempt + 1 ))
  done
  if (( rc != 0 )); then
    echo "ferry autostart: plist written to $plist but 'launchctl bootstrap' failed (rc=$rc); it will load at next login." >&2
    return 1
  fi
  if (( ! quiet )); then
    echo "ferry autostart: installed $plist"
    echo "  At login, 'ferry boot' brings up the endpoint, share server and relay. Log: $(_ferry_autostart_log_path)"
  fi
  return 0
}

_ferry_autostart_uninstall() {
  local lc="${FERRY_LAUNCHCTL:-launchctl}" plist
  plist="$(_ferry_autostart_plist_path)"
  "$lc" bootout "gui/$(id -u)/$FERRY_AUTOSTART_LABEL" >/dev/null 2>&1 || true
  rm -f "$plist" || return 1
  echo "ferry autostart: uninstalled (removed $plist)"
  return 0
}

_ferry_autostart_status() {
  local lc="${FERRY_LAUNCHCTL:-launchctl}" plist log
  plist="$(_ferry_autostart_plist_path)"
  log="$(_ferry_autostart_log_path)"
  if [[ -f "$plist" ]]; then echo "installed: yes ($plist)"; else echo "installed: no"; fi
  if "$lc" print "gui/$(id -u)/$FERRY_AUTOSTART_LABEL" >/dev/null 2>&1; then
    echo "loaded:    yes"
  else
    echo "loaded:    no"
  fi
  if [[ -f "$log" ]]; then
    echo "last lines of $log:"
    tail -n 5 "$log" | sed 's/^/  /'
  fi
  return 0
}

# The RunAtLoad target. Idempotent: starts only what is not already listening.
cmd_boot() {
  local dry_run=0 rc=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      *) echo "ferry boot: unknown option '$1'" >&2; return 2 ;;
    esac
  done

  echo "=== ferry boot $(date '+%Y-%m-%d %H:%M:%S') ==="

  if (( dry_run )); then
    echo "  [dry-run] 1. wait up to 60s for a non-loopback IPv4 address"
    echo "  [dry-run] 2. if nothing listens on :$PORT, run: $FERRY_BIN_PATH up"
    echo "  [dry-run] 3. ensure the share server (:$SHARE_PORT) and relay (:$RELAY_PORT) are up"
    return 0
  fi

  local i=0 ip=""
  while (( i < 60 )); do
    ip="$(detect_lan_ip 2>/dev/null)" || ip=""
    [[ -n "$ip" && "$ip" != "Unknown-IP" ]] && break
    sleep 1
    i=$(( i + 1 ))
  done
  if [[ -n "$ip" && "$ip" != "Unknown-IP" ]]; then
    echo "  network up (waited ${i}s)"
  else
    echo "  no LAN address after 60s; continuing anyway"
  fi

  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "    endpoint already up on :$PORT"
  else
    echo "    starting the endpoint (ferry up)"
    "$FERRY_BIN_PATH" up || { echo "    ferry up failed"; rc=1; }
  fi

  if (( $+functions[_ferry_ensure_share_relay] )); then
    _ferry_ensure_share_relay || rc=1
  else
    echo "    (share/relay ensure not available in this build)"
  fi

  if (( rc == 0 )); then echo "  boot ok"; else echo "  boot finished with problems"; fi
  return $rc
}
