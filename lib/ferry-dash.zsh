
cmd_dash() {
  # Live local web dashboard for the route proxy (`ferry up --route`). Delegates
  # to the sibling `ferry-dash` python script (stdlib only — runs under any
  # python3, no venv). Pass-through args: --open, --port, --ferry, --config, --log.
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
