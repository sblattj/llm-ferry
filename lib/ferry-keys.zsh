# ferry keys — per-device client keys on the HOST. Thin wrapper over
# front/ferry_keys_cli.py, which edits ~/.config/ferry/keys.json (hashes only,
# 0600) and reads the usage counters the front door keeps in
# ~/.config/ferry/keys-usage.sqlite. A client has no key store, so it refuses.

# `ferry keys --help` text. A function of its own so the dispatcher in
# lib/ferry-main.zsh can print it WITHOUT entering cmd_keys.
_ferry_keys_usage() {
  cat <<'EOF'
ferry keys — per-device client keys (host only).

Usage:
  ferry keys add <name> [--expires YYYY-MM-DD] [--lanes L1,L2] [--rpm N]
                        [--budget-tokens N] [--replace]
                                    Mint a key. It is printed ONCE, alone on
                                    stdout; only its hash is stored.
  ferry keys list                   Every key: status, limits, tokens this
                                    month, requests in the last minute.
  ferry keys revoke <name>          Refuse the key from the next request on.
  ferry keys set <name> [--expires D|none] [--lanes L1,L2|none]
                        [--rpm N|none] [--budget-tokens N|none]
                                    Change a key's limits; 'none' clears one.
  ferry keys --help                 This message.

Budgets are TOKENS (input + output) per calendar month (UTC). A client gets
its key automatically by re-running client-bootstrap.sh with the master key.
EOF
}

cmd_keys() {
  case "${1:-}" in
    ""|-h|--help)
      _ferry_keys_usage
      [[ -z "${1:-}" ]] && exit 1
      return 0
      ;;
  esac
  if [[ "$CLIENT_MODE" == "1" ]]; then
    echo "ferry keys runs on the host (it edits the host's ~/.config/ferry/keys.json); this machine is a client." >&2
    exit 1
  fi
  python3 "$APP_DIR/front/ferry_keys_cli.py" "$@"
}
