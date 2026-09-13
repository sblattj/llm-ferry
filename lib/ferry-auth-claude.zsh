# ferry auth-claude — Claude Pro/Max subscription OAuth (browser PKCE).
#
# Wraps the Python OAuth engine (front/ferry_claude_oauth.py, Path A) so an
# operator can run `ferry auth-claude login|status|refresh|logout`. The token
# JSON lives in ferry's own config dir, NOT litellm's: the ChatGPT lane keeps
# ~/.config/litellm/chatgpt/auth.json, and this is its Claude twin at
# ~/.config/ferry/claude/auth.json — mode 0600, readable by this account only.
#
# The access/refresh tokens NEVER reach stdout: every line this module prints
# is derived (email, expiry, state, paths), and the only reader of the raw
# JSON is the summary helper, which emits exactly those safe fields.
#
# Cross-seat contract with front/ferry_claude_oauth.py (the engine seat):
#   login        argv `login --output <path>`; the FERRY_CLAUDE_AUTH_JSON env
#                var carries the same path. Runs the browser PKCE flow, writes
#                the token JSON (access_token / refresh_token / expires_at /
#                email or id_token), exits 0.
#   ensure-valid argv `ensure-valid --auth <path> --force`; same env var.
#                Forces a refresh, persisting the ROTATED refresh_token in
#                place at <path>; exits 0 on success, nonzero when the grant
#                is dead (re-run `ferry auth-claude login`).
# The zsh side never parses the engine's stdout for data — it re-reads the
# token file after every engine call, so the two sides evolve independently.

# Where the subscription tokens live. Overridable via the environment (tests,
# alternate profiles) — same override pattern as FERRY_SCHEMATRON_PORT.
FERRY_CLAUDE_AUTH_JSON="${FERRY_CLAUDE_AUTH_JSON:-$HOME/.config/ferry/claude/auth.json}"
# The OAuth engine script. Empty = resolve from $APP_DIR at call time, so
# loading this module never fails when front/ is not deployed yet.
FERRY_CLAUDE_OAUTH_PY="${FERRY_CLAUDE_OAUTH_PY:-}"

# _ferry_litellm_python — the venv interpreter that owns litellm. Reuses the
# serve module's resolver when it is loaded (the built monolith always loads
# it); otherwise the identical litellm-bin resolution inline, because this
# module must also stand alone with only ferry-core.zsh sourced (the test
# harness does exactly that). `litellm` is installed as a uv tool, so the
# python sitting next to the resolved binary is the only interpreter whose
# site-packages can import the engine's dependencies.
_ferry_litellm_python() {
  if (( $+functions[_ferry_front_python] )); then
    _ferry_front_python
    return $?
  fi
  local bin real py
  bin="$(command -v litellm 2>/dev/null)" || return 1
  [[ -n "$bin" ]] || return 1
  real="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$bin" 2>/dev/null)" || return 1
  py="${real:h}/python"
  [[ -x "$py" ]] || return 1
  print -r -- "$py"
}

# _ferry_claude_oauth_script — the engine script path. Explicit
# FERRY_CLAUDE_OAUTH_PY wins (tests, side-by-side engines); otherwise resolve
# from $APP_DIR. Sourcing the modules from lib/ makes APP_DIR the lib dir
# (ferry-core computes it from $0), and the engine lives one level up in
# front/, so try the parent when the direct guess misses.
_ferry_claude_oauth_script() {
  if [[ -n "$FERRY_CLAUDE_OAUTH_PY" ]]; then
    print -r -- "$FERRY_CLAUDE_OAUTH_PY"
    return 0
  fi
  local d="$APP_DIR"
  [[ -f "$d/front/ferry_claude_oauth.py" ]] || d="${d:h}"
  print -r -- "$d/front/ferry_claude_oauth.py"
}

# _ferry_claude_engine — shared preflight for login/refresh: resolve the venv
# python and the engine script, verify both, print "python\nscript" on
# success. Fails with an operator-readable message on stderr otherwise.
_ferry_claude_engine() {
  local py script
  if ! py="$(_ferry_litellm_python)"; then
    echo "Error: no litellm venv on PATH — the OAuth engine needs its python." >&2
    echo "Is ferry installed here? Run: ferry install" >&2
    return 1
  fi
  script="$(_ferry_claude_oauth_script)"
  if [[ ! -f "$script" ]]; then
    echo "Error: OAuth engine missing: $script" >&2
    return 1
  fi
  print -r -- "$py"
  print -r -- "$script"
}

# _ferry_claude_auth_summary — the ONLY reader of the raw token JSON. Prints
# exactly three lines — email, expiry (UTC ISO), state (valid|expired|
# unknown) — and never the tokens themselves. A missing/unparsable file
# reports "unknown" fields rather than failing, so no caller ever sees a
# traceback or a secret on an error path.
_ferry_claude_auth_summary() {
  python3 - "$1" <<'PYEOF'
import base64, datetime, json, sys

try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    print("unknown")
    print("unknown")
    print("missing")
    raise SystemExit(0)

def jwt_email(tok):
    # Unverified payload peek ONLY to surface the account's email address;
    # the token itself is never emitted anywhere.
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email") or ""
    except Exception:
        return ""

email = (d.get("email") or d.get("account_email")
         or jwt_email(d.get("id_token") or "") or d.get("account_id") or "unknown")
exp = d.get("expires_at")
if isinstance(exp, (int, float)) and exp > 0:
    iso = datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    state = ("expired" if exp <= datetime.datetime.now(datetime.timezone.utc).timestamp()
             else "valid")
else:
    iso, state = "unknown", "unknown"
print(email)
print(iso)
print(state)
PYEOF
}

_ferry_auth_claude_usage() {
  cat <<'EOF'
ferry auth-claude — Claude Pro/Max subscription OAuth (browser PKCE).

Usage:
  ferry auth-claude login             Run the browser PKCE flow and store the
                                      tokens at ~/.config/ferry/claude/auth.json
                                      (mode 0600). Prints the account email.
  ferry auth-claude status            Show account, expiry, and VALID/EXPIRED.
                                      Exits 1 when logged out or expired.
                                      Never prints tokens.
  ferry auth-claude refresh           Force a token refresh and persist the
                                      rotated refresh token in place.
  ferry auth-claude logout [--force]  Delete the stored credentials; --force
                                      skips the 'yes' confirmation.

The token path defaults to ~/.config/ferry/claude/auth.json; redirect it with
the FERRY_CLAUDE_AUTH_JSON environment variable.
EOF
}

# _ferry_claude_print_summary — the shared status tail: account / expiry /
# state lines from the summary helper. Echoes the derived fields only.
_ferry_claude_print_summary() {
  local summary email="unknown" iso="unknown" state="unknown"
  summary="$(_ferry_claude_auth_summary "$FERRY_CLAUDE_AUTH_JSON" 2>/dev/null)"
  { read -r email && read -r iso && read -r state; } <<< "$summary" || true
  echo "    Account: $email"
  echo "    Expires: $iso"
  case "$state" in
    valid)   echo "    State:   VALID (token usable)"; return 0 ;;
    expired) echo "    State:   EXPIRED (run: ferry auth-claude refresh)"; return 1 ;;
    *)       echo "    State:   unknown (no parsable expires_at)"; return 0 ;;
  esac
}

cmd_auth_claude() {
  # `ferry auth-claude` — manage the Claude Pro/Max subscription credentials.
  # All token material stays inside $FERRY_CLAUDE_AUTH_JSON; stdout carries
  # only email, expiry, state, and paths.
  local sub="${1:-}"

  if [[ "$sub" == "--help" || "$sub" == "-h" ]]; then
    _ferry_auth_claude_usage
    return 0
  fi
  if [[ -z "$sub" ]]; then
    _ferry_auth_claude_usage
    return 1
  fi
  shift

  local force=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --force)    force=1; shift ;;
      --help|-h)  _ferry_auth_claude_usage; return 0 ;;
      *) echo "Unknown option for 'ferry auth-claude $sub': $1" >&2; exit 1 ;;
    esac
  done

  case "$sub" in
    login)
      local py script
      { read -r py && read -r script; } <<< "$(_ferry_claude_engine)" || exit 1
      mkdir -p "${FERRY_CLAUDE_AUTH_JSON:h}"
      # The engine inherits the terminal so it can open the browser and print
      # its own progress. The token path rides BOTH as --output and as the
      # FERRY_CLAUDE_AUTH_JSON env var (the engine may read either).
      FERRY_CLAUDE_AUTH_JSON="$FERRY_CLAUDE_AUTH_JSON" \
        "$py" "$script" login --output "$FERRY_CLAUDE_AUTH_JSON" || exit 1
      if [[ ! -f "$FERRY_CLAUDE_AUTH_JSON" ]]; then
        echo "Error: the OAuth engine exited 0 but wrote no token file at" >&2
        echo "$FERRY_CLAUDE_AUTH_JSON" >&2
        exit 1
      fi
      # Enforce the mode even when the engine forgot: this file IS the
      # credential. Status lines come from the safe summary, never the engine
      # stdout (which is not parsed for data).
      chmod 600 "$FERRY_CLAUDE_AUTH_JSON"
      echo ">>> Claude subscription authorized."
      _ferry_claude_print_summary
      echo "    Tokens:  $FERRY_CLAUDE_AUTH_JSON (0600)"
      ;;

    status)
      if [[ ! -f "$FERRY_CLAUDE_AUTH_JSON" ]]; then
        echo "Not logged in (no $FERRY_CLAUDE_AUTH_JSON)."
        echo "Run: ferry auth-claude login"
        return 1
      fi
      echo ">>> Claude subscription auth: $FERRY_CLAUDE_AUTH_JSON"
      _ferry_claude_print_summary
      ;;

    refresh)
      if [[ ! -f "$FERRY_CLAUDE_AUTH_JSON" ]]; then
        echo "Not logged in (no $FERRY_CLAUDE_AUTH_JSON) — nothing to refresh."
        echo "Run: ferry auth-claude login"
        return 1
      fi
      local py script
      { read -r py && read -r script; } <<< "$(_ferry_claude_engine)" || exit 1
      # --force: the operator asked for a refresh, so refresh — not "maybe".
      # The engine must persist the ROTATED refresh_token in place or the
      # next refresh uses a dead token.
      FERRY_CLAUDE_AUTH_JSON="$FERRY_CLAUDE_AUTH_JSON" \
        "$py" "$script" ensure-valid --auth "$FERRY_CLAUDE_AUTH_JSON" --force || exit 1
      chmod 600 "$FERRY_CLAUDE_AUTH_JSON"
      echo ">>> Refreshed the Claude subscription token."
      _ferry_claude_print_summary
      ;;

    logout)
      if [[ ! -f "$FERRY_CLAUDE_AUTH_JSON" ]]; then
        echo "Not logged in (no $FERRY_CLAUDE_AUTH_JSON) — nothing to remove."
        return 0
      fi
      if (( ! force )); then
        local answer=""
        echo "Remove the Claude subscription credentials at"
        echo "  $FERRY_CLAUDE_AUTH_JSON"
        printf "Type 'yes' to confirm: "
        if ! read -r answer; then
          echo ""
          echo "No confirmation available (stdin closed) — pass --force." >&2
          return 1
        fi
        if [[ "$answer" != "yes" ]]; then
          echo "Aborted — credentials left in place."
          return 1
        fi
      fi
      rm -f "$FERRY_CLAUDE_AUTH_JSON"
      echo ">>> Removed $FERRY_CLAUDE_AUTH_JSON (logged out)."
      ;;

    *)
      echo "Unknown subcommand for 'ferry auth-claude': $sub" >&2
      echo "Run: ferry auth-claude --help" >&2
      exit 1
      ;;
  esac
}
