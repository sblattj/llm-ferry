
# ----------------- PARSER -----------------

if [[ $# -lt 1 ]]; then
  usage
fi

COMMAND="$1"
shift

case "$COMMAND" in
  install)       cmd_install ;;
  up)            cmd_up "$@" ;;
  down)          cmd_down ;;
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
  fleet)         cmd_fleet "$@" ;;
  update)        cmd_update "$@" ;;
  migrate)       cmd_migrate "$@" ;;
  dash)          cmd_dash "$@" ;;
  --help|-h)     usage ;;
  *)             echo "Unknown command: $COMMAND"; usage ;;
esac
