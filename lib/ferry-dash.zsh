
cmd_dash() {
  # Live dashboard for ferry — two modes:
  #   ferry dash [--open] [--port P] ...   lightweight stdlib page (`ferry-dash`, localhost:8091),
  #                                        delegates to the sibling python script. Default mode,
  #                                        unchanged behavior.
  #   ferry dash --grafana [--open]        full Grafana+VictoriaMetrics observability stack
  #                                        (localhost:3001) — delegates to observ/bringup.sh.
  #   ferry dash --grafana --down|--stop   tear the Grafana stack down via observ/teardown.sh.
  # Scan args for --grafana / --down / --stop; everything else (--open, --port, --purge, ...)
  # is forwarded through unchanged to whichever script handles the request.
  local arg has_grafana=0 is_down=0
  local -a fwd_args
  for arg in "$@"; do
    case "$arg" in
      --grafana) has_grafana=1 ;;
      --down|--stop) is_down=1 ;;
      *) fwd_args+=("$arg") ;;
    esac
  done

  if (( has_grafana )); then
    local observ_dir="$APP_DIR/observ"
    if [[ ! -f "$observ_dir/bringup.sh" ]]; then
      echo "Error: the Grafana stack lives in observ/ — re-run 'ferry install' or pull latest."
      exit 1
    fi
    if (( is_down )); then
      exec bash "$observ_dir/teardown.sh" "${fwd_args[@]}"
    else
      exec bash "$observ_dir/bringup.sh" "${fwd_args[@]}"
    fi
  fi

  # ---- default: lightweight stdlib page ----
  local dash="$APP_DIR/ferry-dash"
  [[ -f "$dash" ]] || dash="$(command -v ferry-dash || true)"
  if [[ -z "$dash" || ! -f "$dash" ]]; then
    echo "Error: 'ferry-dash' not found next to 'ferry' or on PATH. Re-run: ferry install"
    exit 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: 'ferry dash' needs python3 (any version — stdlib only)."
    exit 1
  fi
  exec python3 "$dash" "$@"
}
