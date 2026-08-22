#!/usr/bin/env bash
# =============================================================================
# ferry observability stack — smoke test.
#
# Runs one check per layer (exporter -> VictoriaMetrics -> scrape landed ->
# VictoriaLogs -> logs landed -> Grafana), prints a PASS/FAIL line for each, and exits
# nonzero if ANY check fails. Safe to run repeatedly; run it right after observ/bringup.sh.
#
# Contract: observ/CONTRACT.md — ports 9092 (exporter) / 8429 (VM) / 9428 (VictoriaLogs)
# / 3001 (Grafana). Owned by the "bringup" seat.
# =============================================================================
set -euo pipefail

FAIL=0
pass() { printf '\033[1;32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '\033[1;31mFAIL\033[0m  %s\n' "$*"; FAIL=1; }

# 1) exporter health endpoint
if curl -fsS --max-time 5 http://127.0.0.1:9092/healthz >/dev/null 2>&1; then
  pass "exporter /healthz (127.0.0.1:9092)"
else
  fail "exporter /healthz (127.0.0.1:9092) — is ferry-metrics-exporter running? (check $HOME/.config/ferry/observ/logs/exporter.log)"
fi

# 2) exporter is emitting the ferry_up metric line
if curl -fsS --max-time 5 http://127.0.0.1:9092/metrics 2>/dev/null | grep -q '^ferry_up '; then
  pass "exporter /metrics emits ferry_up"
else
  fail "exporter /metrics missing 'ferry_up' line"
fi

# 3) VictoriaMetrics health
if curl -fsS --max-time 5 http://127.0.0.1:8429/health >/dev/null 2>&1; then
  pass "VictoriaMetrics /health (127.0.0.1:8429)"
else
  fail "VictoriaMetrics /health (127.0.0.1:8429)"
fi

# 4) VictoriaMetrics has actually scraped the exporter (ferry_up present in the TSDB).
#    scrape_interval is 15s, so the first scrape can lag — retry for ~20s.
VM_OK=0
for _ in 1 2 3 4 5 6 7; do
  if curl -fsS --max-time 5 'http://127.0.0.1:8429/api/v1/query?query=ferry_up' 2>/dev/null | grep -q '"value"'; then
    VM_OK=1
    break
  fi
  sleep 3
done
if [[ "$VM_OK" -eq 1 ]]; then
  pass "VictoriaMetrics scraped ferry_up (query returned a value)"
else
  fail "VictoriaMetrics query for ferry_up returned no value after ~20s of retries"
fi

# 5) VictoriaLogs health
if curl -fsS --max-time 5 http://127.0.0.1:9428/health >/dev/null 2>&1; then
  pass "VictoriaLogs /health (127.0.0.1:9428)"
else
  fail "VictoriaLogs /health (127.0.0.1:9428) — is victoria-logs running? (check $HOME/.config/ferry/observ/logs/vlogs.log)"
fi

# 6) Logs are actually LANDING in VictoriaLogs (the ferry-log-shipper is pushing).
#    The shipper batches, so the first flush can lag — retry for ~20s like check 4.
#    LogsQL '*' matches everything; limit=1 keeps the response to a single line.
#    A response line is one JSON record, so any '{' means at least one log landed.
VL_OK=0
for _ in 1 2 3 4 5 6 7; do
  if curl -fsS --max-time 5 'http://127.0.0.1:9428/select/logsql/query' \
       --data-urlencode 'query=*' --data-urlencode 'limit=1' 2>/dev/null | grep -q '{'; then
    VL_OK=1
    break
  fi
  sleep 3
done
if [[ "$VL_OK" -eq 1 ]]; then
  pass "VictoriaLogs has log lines (logsql query '*' returned a result)"
else
  fail "VictoriaLogs logsql query returned nothing after ~20s — is ferry-log-shipper running and is the ferry proxy logging? (check $HOME/.config/ferry/observ/logs/shipper.log)"
fi

# 7) Grafana health API returns HTTP 200
CODE="$(curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:3001/api/health 2>/dev/null || true)"
if [[ "$CODE" == "200" ]]; then
  pass "Grafana /api/health (127.0.0.1:3001) -> 200"
else
  fail "Grafana /api/health (127.0.0.1:3001) -> ${CODE:-no response}"
fi

echo
if [[ "$FAIL" -eq 0 ]]; then
  printf '\033[1;32mALL CHECKS PASSED\033[0m\n'
  exit 0
else
  printf '\033[1;31mSMOKE TEST FAILED\033[0m — see failing check(s) above; logs in %s/.config/ferry/observ/logs/\n' "$HOME"
  exit 1
fi
