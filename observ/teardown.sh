#!/usr/bin/env bash
# =============================================================================
# ferry observability stack — teardown.
#
# Stops everything bringup.sh started: the VictoriaMetrics, VictoriaLogs,
# ferry-metrics-exporter, ferry-log-shipper, and Grafana nohup daemons (by PID file,
# with a port fallback for the ones that listen). Idempotent — safe to run when
# nothing is up.
#
# Usage:  bash observ/teardown.sh [--purge]
#           --purge   ALSO delete the runtime state dir (~/.config/ferry/observ:
#                     VictoriaMetrics TSDB, VictoriaLogs store, Grafana DB,
#                     materialized provisioning, logs).
#                     Without it, data is left in place for the next bringup.
#
# Contract: observ/CONTRACT.md. Owned by the "bringup" seat.
# =============================================================================
set -euo pipefail

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "WARN: unknown arg '$arg' (ignored)" >&2 ;;
  esac
done

STATE="$HOME/.config/ferry/observ"

info() { printf '\033[1;36m[teardown]\033[0m %s\n' "$*"; }

# Stop a daemon by its PID file, then remove the file. Tolerant of an already-dead pid.
kill_pidfile() { # $1 = pidfile, $2 = human name
  local pf="$1" name="$2" pid
  if [[ -f "$pf" ]]; then
    pid="$(cat "$pf" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      info "stopping $name (pid $pid)"
      kill "$pid" 2>/dev/null || true
    else
      info "$name pid file present but process not running — cleaning up"
    fi
    rm -f "$pf"
  fi
}

# Fallback: stop whatever is listening on the port if the PID file was lost.
kill_port() { # $1 = port, $2 = human name
  local port="$1" name="$2" pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  elif command -v ss >/dev/null 2>&1; then
    pids="$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
  fi
  if [[ -n "$pids" ]]; then
    info "stopping $name on :$port (pid(s) $(echo "$pids" | tr '\n' ' '))"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
  fi
}

kill_pidfile "$STATE/grafana.pid"  "Grafana"
kill_pidfile "$STATE/exporter.pid" "ferry-metrics-exporter"
# The shipper is a pusher — it listens on nothing, so its PID file is the ONLY handle.
kill_pidfile "$STATE/shipper.pid"  "ferry-log-shipper"
kill_pidfile "$STATE/vm.pid"       "VictoriaMetrics"
kill_pidfile "$STATE/vlogs.pid"    "VictoriaLogs"
# Port fallback for anything the PID files missed (listeners only — not the shipper).
kill_port 3001 "Grafana"
kill_port 9092 "ferry-metrics-exporter"
kill_port 8429 "VictoriaMetrics"
kill_port 9428 "VictoriaLogs"

# ----------------------------------------------------------------------------- state
if [[ "$PURGE" -eq 1 ]]; then
  info "purging state dir $STATE"
  rm -rf "$STATE"
else
  info "state dir left in place: $STATE  (re-run with --purge to delete data)"
fi

info "teardown complete."
