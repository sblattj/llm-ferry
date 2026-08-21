# ----------------- FERRY TRANSFER COMMANDS -----------------

# Resolve the LAN host to talk to for client-side pull/get:
#   --host flag (already captured into $1) > $CLIENT_HOST (client mode) > error.
resolve_ferry_host() {
  local h="$1"
  if [[ -n "$h" ]]; then echo "$h"; return 0; fi
  if [[ -n "${CLIENT_HOST:-}" ]]; then echo "$CLIENT_HOST"; return 0; fi
  return 1
}

# Resolve the share-server port for client-side pull/get:
#   --port flag ($1) > $CLIENT_SHARE_PORT > $SHARE_PORT > 8095.
resolve_ferry_port() {
  local p="$1"
  if [[ -n "$p" ]]; then echo "$p"; return; fi
  if [[ -n "${CLIENT_SHARE_PORT:-}" ]]; then echo "$CLIENT_SHARE_PORT"; return; fi
  if [[ -n "${SHARE_PORT:-}" ]]; then echo "$SHARE_PORT"; return; fi
  echo "8095"
}

cmd_offer() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry offer' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi
  if [[ $# -lt 1 ]]; then
    echo "Usage: ferry offer <path>..."
    exit 1
  fi
  local cfg_dir="$HOME/.config/ferry"
  local offered="$cfg_dir/offered.json"
  mkdir -p "$cfg_dir"

  # Merge the given paths (basename -> absolute path) into offered.json via python for safe JSON.
  python3 - "$offered" "$@" <<'PYEOF'
import json, os, sys
offered_path = sys.argv[1]
paths = sys.argv[2:]
data = {}
if os.path.exists(offered_path):
    try:
        data = json.load(open(offered_path))
    except Exception:
        data = {}
for p in paths:
    ap = os.path.abspath(os.path.expanduser(p))
    if not os.path.exists(ap):
        print(f"    WARNING: path not found, skipping: {p}")
        continue
    name = os.path.basename(ap.rstrip("/"))
    data[name] = ap
    print(f"    Offered: {name}  ->  {ap}")
json.dump(data, open(offered_path, "w"), indent=2)
print(f">>> Offered manifest saved: {offered_path}")
PYEOF
}

cmd_pull() {
  if [[ $# -lt 1 || "$1" == --* ]]; then
    echo "Usage: ferry pull <model-id> [--host H] [--port P] [--transport http|hf|nc] [--to DIR]"
    exit 1
  fi
  local model_id="$1"; shift
  local host="" port="" transport="http" to=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)      host="$2"; shift 2 ;;
      --port)      port="$2"; shift 2 ;;
      --transport) transport="$2"; shift 2 ;;
      --to)        to="$2"; shift 2 ;;
      *)           echo "Unknown option: $1"; exit 1 ;;
    esac
  done

  case "$transport" in
    http)
      local h; h=$(resolve_ferry_host "$host") || {
        echo "Error: no host resolved. Pass --host <hostname-or-ip> (or run on a configured client)."; exit 1; }
      local p; p=$(resolve_ferry_port "$port")
      local dest="${to:-$HOME/.cache/ferry/models/$model_id}"
      mkdir -p "$dest"
      echo ">>> Pulling model '$model_id' from http://$h:$p over HTTP (tar stream)..."
      if curl -fsS "http://$h:$p/pull/$model_id" | tar -x -C "$dest"; then
        echo ">>> Model landed at: $dest"
      else
        echo "Error: pull failed. Is the model in the host's HF cache, and is 'ferry share' running?"
        exit 1
      fi
      ;;
    hf)
      local h; h=$(resolve_ferry_host "$host") || {
        echo "Error: no host resolved. Pass --host <hostname-or-ip>."; exit 1; }
      local hfp="${port:-$HF_PORT}"
      echo ">>> [EXPERIMENTAL] Pulling '$model_id' THROUGH host HF proxy at http://$h:$hfp ..."
      echo "    (Requires the host to be running: ferry serve-hf)"
      if command -v hf >/dev/null 2>&1; then
        HF_ENDPOINT="http://$h:$hfp" hf download "$model_id"
      else
        echo "    'hf' not found; falling back to 'uv run huggingface-cli'..."
        HF_ENDPOINT="http://$h:$hfp" uv run huggingface-cli download "$model_id"
      fi
      ;;
    nc)
      echo ">>> Pull via netcat: this laptop will LISTEN and receive a tar stream."
      echo "    On the HOST, run:  ferry send <path-to-model-dir> <this-laptop-host-or-ip> --port ${port:-9099}"
      local recv_args=()
      [[ -n "$port" ]] && recv_args+=(--port "$port")
      [[ -n "$to" ]]   && recv_args+=(--to "$to")
      cmd_receive "${recv_args[@]}"
      ;;
    *)
      echo "Unknown transport: $transport (use http|hf|nc)"
      exit 1
      ;;
  esac
}

cmd_get() {
  if [[ $# -lt 1 || "$1" == --* ]]; then
    echo "Usage: ferry get <name> [--host H] [--port P] [--to DIR]"
    exit 1
  fi
  local name="$1"; shift
  local host="" port="" to=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host) host="$2"; shift 2 ;;
      --port) port="$2"; shift 2 ;;
      --to)   to="$2"; shift 2 ;;
      *)      echo "Unknown option: $1"; exit 1 ;;
    esac
  done
  local h; h=$(resolve_ferry_host "$host") || {
    echo "Error: no host resolved. Pass --host <hostname-or-ip>."; exit 1; }
  local p; p=$(resolve_ferry_port "$port")
  local dest="${to:-.}"
  mkdir -p "$dest"
  echo ">>> Fetching offered file '$name' from http://$h:$p into $dest ..."
  if curl -fsS "http://$h:$p/file/$name" | tar -x -C "$dest"; then
    echo ">>> Landed under: $dest"
    ls -la "$dest"
  else
    echo "Error: fetch failed. Is '$name' offered on the host (see 'ferry offer')?"
    exit 1
  fi
}

cmd_receive() {
  local port="" to=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port) port="$2"; shift 2 ;;
      --to)   to="$2"; shift 2 ;;
      *)      echo "Unknown option: $1"; exit 1 ;;
    esac
  done
  local rcv_port="${port:-9099}"
  local dest="${to:-.}"
  mkdir -p "$dest"
  echo ">>> Receiving: listening on port $rcv_port; extracting into $dest"
  echo "    On the HOST run:  ferry send <file|dir> <this-laptop-host-or-ip> --port $rcv_port"
  # BSD/macOS netcat: `nc -l PORT` listens; the stream is piped straight into tar.
  # `-d` stops nc from reading stdin — without it, a backgrounded listener whose
  # stdin has already reached EOF tears the connection down before the tar payload
  # finishes transferring (Apple nc reads stdin and closes the socket on its EOF).
  # openbsd-nc (the Ubuntu default) has no `-d` flag; plain `nc -l` is correct there,
  # and ncat (nmap) is preferred when present for cross-platform consistency.
  if (( IS_MAC )); then
    nc -d -l "$rcv_port" | tar -x -C "$dest"
  elif command -v ncat >/dev/null 2>&1; then
    ncat -l "$rcv_port" | tar -x -C "$dest"
  else
    nc -l "$rcv_port" | tar -x -C "$dest"
  fi
  echo ">>> Receive complete. Files are in: $dest"
}

cmd_send() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry send' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi
  if [[ $# -lt 2 || "$1" == --* || "$2" == --* ]]; then
    echo "Usage: ferry send <file|dir> <client-host> [--port P]"
    exit 1
  fi
  local src="$1"; local client_host="$2"; shift 2
  local port=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port) port="$2"; shift 2 ;;
      *)      echo "Unknown option: $1"; exit 1 ;;
    esac
  done
  local send_port="${port:-9099}"
  if [[ ! -e "$src" ]]; then
    echo "Error: path not found: $src"
    exit 1
  fi
  # zsh modifiers: :A = absolute path, :h = head (dirname), :t = tail (basename).
  # Using them avoids forking dirname/basename and works for relative paths too.
  # NB: do NOT name this var 'path' — in zsh that is the array tied to $PATH.
  local parent base
  parent="${src:A:h}"
  base="${src:t}"
  echo ">>> Sending '$base' to $client_host:$send_port via netcat (tar stream)..."
  echo "    (The client must already be running: ferry receive --port $send_port)"
  # macOS/BSD nc closes on its own once stdin (the tar) ends. openbsd-nc (Ubuntu)
  # keeps the socket half-open on stdin EOF, so add `-N` to half-close and let the
  # receiver's tar finish.
  if (( IS_MAC )); then
    tar -c -C "$parent" "$base" | nc "$client_host" "$send_port"
  else
    tar -c -C "$parent" "$base" | nc -N "$client_host" "$send_port"
  fi
  echo ">>> Sent: $base"
}

