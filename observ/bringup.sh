#!/usr/bin/env bash
# =============================================================================
# ferry observability stack — bring-up (Mac primary; Linux best-effort).
#
# Brings up a local Grafana + VictoriaMetrics dashboard stack for llm-ferry as
# nohup daemons with PID files (ferry's own idiom — NOT launchd). Coexists with
# the g0vs1g desk stack (3000/8428/9847) by using distinct ports + data dir:
#
#   proxy log + litellm /health,/models + litellm.yaml
#        │
#        └─(ferry-metrics-exporter :9092)─▶ VictoriaMetrics :8429 ─▶ Grafana :3001
#                                                  ▲
#                             litellm :8090/metrics (OPT-IN, off by default)
#
# Idempotent: safe to re-run. Guards every start on a port-already-listening
# check, re-materializes the provisioning tree, and never double-starts a daemon.
#
# Usage:  bash observ/bringup.sh [--open]
#           --open   open http://127.0.0.1:3001 in the browser once Grafana is up
#
# After it finishes:  bash observ/verify.sh   # smoke-test every layer
# Halt:               bash observ/teardown.sh
#
# Contract: observ/CONTRACT.md. Owned by the "bringup" seat.
# =============================================================================
set -euo pipefail

# ----------------------------------------------------------------------------- args
OPEN=0
for arg in "$@"; do
  case "$arg" in
    --open) OPEN=1 ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "WARN: unknown arg '$arg' (ignored)" >&2 ;;
  esac
done

# ----------------------------------------------------------------------------- paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# bringup.sh lives at <repo>/observ/bringup.sh, so the llm-ferry repo root (APP_DIR)
# is one dir up from observ/. Guarded below.
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OBSERV="$REPO_ROOT/observ"
STATE="$HOME/.config/ferry/observ"
PROV_SRC="$OBSERV/grafana/provisioning"
PROV_RUN="$STATE/grafana-provisioning"
DASH_SRC="$OBSERV/grafana/dashboards"
SCRAPE_CFG="$OBSERV/victoriametrics/scrape.yml"
EXPORTER="$OBSERV/ferry-metrics-exporter"

info() { printf '\033[1;36m[bringup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bringup] WARN:\033[0m %s\n' "$*" >&2; }

OS="$(uname -s)"

# Cross-platform "is something listening on TCP :$1 ?" (lsof on mac, ss on Linux,
# bash /dev/tcp as a last resort).
port_listening() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnH "sport = :$1" 2>/dev/null | grep -q ":$1"
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1 && { exec 3>&- 3<&-; return 0; }
    return 1
  fi
}

# Portable in-place sed (BSD/mac needs the empty backup-suffix arg; GNU does not).
if sed --version >/dev/null 2>&1; then
  sed_inplace() { sed -i "$@"; }
else
  sed_inplace() { sed -i '' "$@"; }
fi

# Sanity-guard the repo-root resolution: observ/ must sit directly under it.
[[ -d "$OBSERV" ]] || { warn "resolved REPO_ROOT=$REPO_ROOT but $OBSERV is missing — is bringup.sh in <repo>/observ/?"; exit 1; }

info "repo root : $REPO_ROOT"
info "state dir : $STATE"
info "platform  : $OS"
mkdir -p "$STATE"/{vm-data,grafana-data,grafana-provisioning,logs}

# ----------------------------------------------------------------------------- secrets
# FERRY_ALERT_WEBHOOK is the optional Grafana alert contact-point URL. Source it
# best-effort from ferry's own secrets file and the shared dotorg secrets, so
# envsubst can bake it into the runtime alerting tree. Alerts still SHOW in the
# Grafana UI if it is unset — they just do not deliver to the webhook.
set +u
# shellcheck disable=SC1090
[[ -f "$HOME/.config/ferry/secrets.env" ]] && source "$HOME/.config/ferry/secrets.env" 2>/dev/null || true
# shellcheck disable=SC1090
[[ -f "$HOME/.dotorg/zsh/secrets.zsh" ]] && source "$HOME/.dotorg/zsh/secrets.zsh" 2>/dev/null || true
set -u
if [[ -z "${FERRY_ALERT_WEBHOOK:-}" ]]; then
  warn "FERRY_ALERT_WEBHOOK not set — Grafana alerts will NOT deliver (dashboards still work)."
fi
export FERRY_ALERT_WEBHOOK="${FERRY_ALERT_WEBHOOK:-}"

# ----------------------------------------------------------------------------- config tokens
export VM_URL="${VM_URL:-http://127.0.0.1:8429}"
export GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-admin}"
export GF_SECURITY_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD:-ferry-observ}"

# ----------------------------------------------------------------------------- binaries (platform)
# Mac: use the brew-installed binaries (already present per preflight). Linux: prefer
# docker, else fall back to a static-binary download into $STATE (best-effort; TODO
# markers where a version/URL can't be verified here). The Mac path is fully working.
VM_BIN=""
GF_BIN=""
GF_HOMEPATH=""

resolve_mac_binaries() {
  VM_BIN="$(command -v victoria-metrics || command -v victoriametrics || true)"
  GF_BIN="$(command -v grafana || true)"
  if [[ -z "$VM_BIN" || -z "$GF_BIN" ]]; then
    if command -v brew >/dev/null 2>&1; then
      info "installing missing formulae via brew (victoriametrics/grafana) ..."
      brew list victoriametrics >/dev/null 2>&1 || brew install victoriametrics
      brew list grafana         >/dev/null 2>&1 || brew install grafana
      VM_BIN="$(command -v victoria-metrics || command -v victoriametrics || true)"
      GF_BIN="$(command -v grafana || true)"
    else
      warn "brew not found and victoria-metrics/grafana are not on PATH."
    fi
  fi
  local brew_prefix; brew_prefix="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
  GF_HOMEPATH="$brew_prefix/opt/grafana/share/grafana"
}

resolve_linux_binaries() {
  VM_BIN="$(command -v victoria-metrics || command -v victoriametrics || true)"
  GF_BIN="$(command -v grafana || command -v grafana-server || true)"
  if [[ -n "$VM_BIN" && -n "$GF_BIN" ]]; then
    # Native packages present (e.g. linuxbrew or distro packages) — use them.
    GF_HOMEPATH="$(dirname "$(dirname "$GF_BIN")")/share/grafana"
    [[ -d "$GF_HOMEPATH" ]] || GF_HOMEPATH="/usr/share/grafana"
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    warn "Linux + docker detected. This script drives HOST binaries, not containers."
    warn "TODO(linux): run the stack via containers, e.g.:"
    warn "  docker run -d --name ferry-vm --network host -v $STATE/vm-data:/vm-data \\"
    warn "    victoriametrics/victoria-metrics:v1.150.0 \\"
    warn "    -promscrape.config=$SCRAPE_CFG -storageDataPath=/vm-data -httpListenAddr=127.0.0.1:8429 -retentionPeriod=12"
    warn "  docker run -d --name ferry-grafana --network host \\"
    warn "    -e GF_SERVER_HTTP_PORT=3001 -e GF_PATHS_PROVISIONING=/prov -v $PROV_RUN:/prov \\"
    warn "    -v $DASH_SRC:$DASH_SRC grafana/grafana-oss:13.2.0"
    warn "  (materialize provisioning below still applies; then run observ/verify.sh)"
  else
    warn "Linux without docker or native binaries."
    warn "TODO(linux): download static binaries into $STATE (URLs/versions NOT verified here):"
    warn "  VictoriaMetrics: https://github.com/VictoriaMetrics/VictoriaMetrics/releases (victoria-metrics-linux-<arch>-v1.150.0.tar.gz)"
    warn "  Grafana OSS:     https://grafana.com/grafana/download (grafana-13.2.0.linux-<arch>.tar.gz)"
    warn "  then set VM_BIN / GF_BIN / GF_HOMEPATH to the extracted paths and re-run."
  fi
}

case "$OS" in
  Darwin) resolve_mac_binaries ;;
  Linux)  resolve_linux_binaries ;;
  *)      warn "unsupported platform '$OS' — treating as best-effort Linux."; resolve_linux_binaries ;;
esac

# ----------------------------------------------------------------------------- materialize provisioning
# Copy the whole provisioning tree into a RUNTIME-ONLY location, expanding ONLY the
# three known tokens (envsubst with an explicit shell-format leaves unrelated $-signs
# alone), so the repo-tracked YAML never carries a real webhook URL / admin user.
# Grafana is launched pointed at PROV_RUN, never at the repo tree.
info "materializing provisioning -> $PROV_RUN"
rm -rf "$PROV_RUN"
mkdir -p "$PROV_RUN"
if [[ -d "$PROV_SRC" ]]; then
  if ! command -v envsubst >/dev/null 2>&1; then
    warn "envsubst not found (install gettext) — copying provisioning WITHOUT token expansion."
  fi
  while IFS= read -r -d '' src; do
    rel="${src#"$PROV_SRC"/}"
    dst="$PROV_RUN/$rel"
    mkdir -p "$(dirname "$dst")"
    if command -v envsubst >/dev/null 2>&1; then
      envsubst '${VM_URL} ${FERRY_ALERT_WEBHOOK} ${GF_SECURITY_ADMIN_USER}' <"$src" >"$dst"
    else
      cp "$src" "$dst"
    fi
  done < <(find "$PROV_SRC" -type f -print0)
else
  warn "provisioning source $PROV_SRC not found — Grafana will start with no datasource/dashboards."
fi

# The dashboards provider YAML ships with a container mount path baked in
# (/var/lib/grafana/dashboards). There is no container mount here, so rewrite it to
# the repo's own dashboards dir.
DASH_PROVIDER="$PROV_RUN/dashboards/dashboards.yml"
if [[ -f "$DASH_PROVIDER" ]]; then
  sed_inplace "s#/var/lib/grafana/dashboards#$DASH_SRC#" "$DASH_PROVIDER"
  info "dashboards provider path -> $DASH_SRC"
fi

# ----------------------------------------------------------------------------- VictoriaMetrics (:8429)
if port_listening 8429; then
  info "VictoriaMetrics already listening on :8429 — leaving it."
elif [[ -n "$VM_BIN" ]]; then
  info "starting VictoriaMetrics (:8429) via $VM_BIN"
  nohup "$VM_BIN" \
    -promscrape.config="$SCRAPE_CFG" \
    -storageDataPath="$STATE/vm-data" \
    -retentionPeriod=12 \
    -httpListenAddr=127.0.0.1:8429 \
    >"$STATE/logs/vm.log" 2>&1 &
  echo $! >"$STATE/vm.pid"
  info "VictoriaMetrics pid $(cat "$STATE/vm.pid") — log: $STATE/logs/vm.log"
else
  warn "no VictoriaMetrics binary resolved — skipping VM start (see Linux TODO above)."
fi

# ----------------------------------------------------------------------------- exporter (:9092)
if [[ -f "$EXPORTER" ]]; then
  chmod +x "$EXPORTER" 2>/dev/null || true
else
  warn "exporter $EXPORTER not found yet — start will no-op until it lands (orchestrator runs bringup after all seats)."
fi
if port_listening 9092; then
  info "ferry-metrics-exporter already listening on :9092 — leaving it."
elif [[ -f "$EXPORTER" ]]; then
  info "starting ferry-metrics-exporter (:9092) via python3"
  nohup python3 "$EXPORTER" --port 9092 >"$STATE/logs/exporter.log" 2>&1 &
  echo $! >"$STATE/exporter.pid"
  info "exporter pid $(cat "$STATE/exporter.pid") — log: $STATE/logs/exporter.log"
else
  warn "exporter not started (file missing)."
fi

# ----------------------------------------------------------------------------- Grafana (:3001)
if port_listening 3001; then
  info "Grafana already listening on :3001 — leaving it."
elif [[ -n "$GF_BIN" ]]; then
  info "starting Grafana (:3001) via $GF_BIN server"
  # GF_* env overrides beat any grafana.ini; the host path uses pure env (no --config),
  # so the committed grafana.ini stays the container artifact. Provisioning + data + logs
  # all live under the runtime STATE dir.
  GF_PATHS_PROVISIONING="$PROV_RUN" \
  GF_PATHS_DATA="$STATE/grafana-data" \
  GF_PATHS_LOGS="$STATE/logs" \
  GF_SECURITY_ADMIN_USER="$GF_SECURITY_ADMIN_USER" \
  GF_SECURITY_ADMIN_PASSWORD="$GF_SECURITY_ADMIN_PASSWORD" \
  GF_SERVER_HTTP_PORT=3001 \
  GF_ANALYTICS_REPORTING_ENABLED=false \
  GF_ANALYTICS_CHECK_FOR_UPDATES=false \
    nohup "$GF_BIN" server --homepath "$GF_HOMEPATH" \
    >"$STATE/logs/grafana.log" 2>&1 &
  echo $! >"$STATE/grafana.pid"
  info "Grafana pid $(cat "$STATE/grafana.pid") — log: $STATE/logs/grafana.log  (homepath: $GF_HOMEPATH)"
else
  warn "no Grafana binary resolved — skipping Grafana start (see Linux TODO above)."
fi

# ----------------------------------------------------------------------------- open (optional)
if [[ "$OPEN" -eq 1 ]]; then
  URL="http://127.0.0.1:3001"
  if command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
  else info "open $URL in your browser."; fi
fi

# ----------------------------------------------------------------------------- summary
cat <<EOF

=============================================================================
 ferry observability stack — UP
=============================================================================
 Grafana           : http://127.0.0.1:3001   (login: $GF_SECURITY_ADMIN_USER / $GF_SECURITY_ADMIN_PASSWORD)
 VictoriaMetrics   : http://127.0.0.1:8429
 Exporter /metrics : http://127.0.0.1:9092/metrics
 State / logs      : $STATE  (logs in $STATE/logs)
-----------------------------------------------------------------------------
 This is a LOCAL surface (127.0.0.1 only). To reach it from the LAN or tailnet,
 expose Grafana explicitly, e.g.:
   tailscale serve --bg --https=3443 http://127.0.0.1:3001    # tailnet HTTPS
   # or bind Grafana to 0.0.0.0 by launching with GF_SERVER_HTTP_ADDR=0.0.0.0
-----------------------------------------------------------------------------
 Smoke-test:  bash $OBSERV/verify.sh
 Halt:        bash $OBSERV/teardown.sh
=============================================================================
EOF
