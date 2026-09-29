
# ----------------- PARSER -----------------

# Help for ONE command, printed WITHOUT entering its cmd_ function. A command
# with its own usage function (_ferry_<cmd>_usage: auth-claude, claude, fleet, keys)
# prints that; every other command prints its entry from the usage() banner.
# Returns 1 when <cmd> is not a known command.
_ferry_help_for() {
  local cmd="$1" fn="_ferry_${1//-/_}_usage"
  if (( $+functions[$fn] )); then
    "$fn"
    return 0
  fi
  _ferry_usage_section "$cmd"
}

# Pass-through commands: every argument, -h/--help included, goes unchanged to
# a child script that answers --help itself before doing anything:
#   dash  -> ferry-dash (argparse), observ/bringup.sh, observ/teardown.sh
# `ferry help dash` still prints the banner entry without running the child.
_FERRY_PASSTHROUGH_CMDS=(dash)

# Commands that take NO arguments. Anything after them is an error (exit 2)
# rather than being silently ignored while the command runs.
_FERRY_NOARG_CMDS=(install reload status share log)

if [[ $# -lt 1 ]]; then
  usage
fi

COMMAND="$1"
shift

# `ferry help [cmd]`
if [[ "$COMMAND" == "help" ]]; then
  [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]] && usage
  if ! _ferry_help_for "$1"; then
    echo "Unknown command: $1" >&2
    usage
  fi
  exit 0
fi

# Central -h/--help guard: `ferry <cmd> ... --help ...` prints that command's
# help and exits 0 without calling cmd_<cmd>. Without it, a command that
# ignores its arguments (reload, install, ...) would RUN when asked for help.
if (( ! ${_FERRY_PASSTHROUGH_CMDS[(Ie)$COMMAND]} )); then
  for _ferry_arg in "$@"; do
    if [[ "$_ferry_arg" == "-h" || "$_ferry_arg" == "--help" ]]; then
      _ferry_help_for "$COMMAND" && exit 0
      break  # not a known command: fall through to the Unknown arm below
    fi
  done
  unset _ferry_arg
fi

if (( ${_FERRY_NOARG_CMDS[(Ie)$COMMAND]} )) && [[ $# -gt 0 ]]; then
  echo "ferry $COMMAND: unexpected argument '$1' ('ferry $COMMAND' takes no arguments; see: ferry help $COMMAND)" >&2
  exit 2
fi

case "$COMMAND" in
  install)       cmd_install ;;
  up)            cmd_up "$@" ;;
  down)          cmd_down "$@" ;;
  reload)        cmd_reload ;;
  status)        cmd_status ;;
  share)         cmd_share ;;
  msg)           cmd_msg "$@" ;;
  log)           cmd_log ;;
  inbox)         cmd_inbox "$@" ;;
  relay)         cmd_relay "$@" ;;
  expose)        cmd_expose "$@" ;;
  expose-vnc)    cmd_expose_vnc "$@" ;;
  offer)         cmd_offer "$@" ;;
  pull)          cmd_pull "$@" ;;
  get)           cmd_get "$@" ;;
  receive)       cmd_receive "$@" ;;
  send)          cmd_send "$@" ;;
  drop)          cmd_drop "$@" ;;
  pickup)        cmd_pickup "$@" ;;
  serve-hf)      cmd_serve_hf "$@" ;;
  serve-proxy)   cmd_serve_proxy "$@" ;;
  serve-vnc)     cmd_serve_vnc "$@" ;;
  env)           cmd_env "$@" ;;
  opencode)      cmd_opencode "$@" ;;
  claude)        cmd_claude "$@" ;;
  auth-claude)   cmd_auth_claude "$@" ;;
  cline)         cmd_cline "$@" ;;
  fleet)         cmd_fleet "$@" ;;
  keys)          cmd_keys "$@" ;;
  update)        cmd_update "$@" ;;
  migrate)       cmd_migrate "$@" ;;
  dash)          cmd_dash "$@" ;;
  --help|-h)     usage ;;
  *)             echo "Unknown command: $COMMAND"; usage ;;
esac
