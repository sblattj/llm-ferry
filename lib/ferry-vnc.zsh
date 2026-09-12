# ferry-vnc.zsh — the browser half of VNC-through-ferry.
#
# `ferry expose-vnc` (client) publishes a laptop's VNC server through the relay
# as an ordinary TCP port tagged kind=vnc. This module gives that port a URL:
# `ferry serve-vnc` (host) serves noVNC and bridges WebSocket <-> TCP onto
# 127.0.0.1:<published port>. It bridges ONLY ports the relay state file lists
# as kind=vnc at the moment of the request, so it is not a general ws->tcp hole.
#
# What this is NOT: auth. The VNC server's own password gates the screen; this
# is plain HTTP on the LAN, exactly like the relay's published ports.

cmd_serve_vnc() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry serve-vnc' is only available on the LLM-Ferry Host Mac."
    echo "       The client side is 'ferry expose-vnc'."
    exit 1
  fi
  local vnc_port="$VNC_PORT" bind_addr="0.0.0.0" foreground=0 fetch_only=0 marker="ferry-vnc-marker"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port)       vnc_port="$2"; shift 2 ;;
      --bind)       bind_addr="$2"; shift 2 ;;
      --foreground) foreground=1; shift ;;
      --fetch)      fetch_only=1; shift ;;
      --marker)     marker="$2"; shift 2 ;;
      -h|--help)
        echo "Usage: ferry serve-vnc [--port P] [--bind ADDR] [--foreground] [--fetch]"
        echo "  --port P      the viewer port [default: $VNC_PORT]"
        echo "  --bind ADDR   0.0.0.0 (the LAN, default) or 127.0.0.1 (this host only)"
        echo "  --fetch       download noVNC $NOVNC_VERSION into $NOVNC_DIR and exit"
        return 0 ;;
      *) echo "Unknown option for 'ferry serve-vnc': $1"; exit 1 ;;
    esac
  done

  if (( fetch_only )); then
    novnc_fetch || exit 1
    return 0
  fi
  if [[ ! -f "$NOVNC_DIR/VERSION" || "$(cat "$NOVNC_DIR/VERSION")" != "$NOVNC_VERSION" || ! -f "$NOVNC_DIR/vnc.html" ]]; then
    echo "Error: noVNC $NOVNC_VERSION is not installed under $NOVNC_DIR."
    echo "       Run: ferry serve-vnc --fetch   (downloads it once from GitHub)"
    exit 1
  fi

  if (( ! foreground )); then
    if lsof -nP -iTCP:"$vnc_port" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "Error: port $vnc_port is already in use — a viewer may already be running."
      exit 1
    fi
    echo ">>> Starting the browser VNC viewer on port $vnc_port (bind $bind_addr)"
    echo "    Open: http://$MDNS_NAME:$vnc_port  (or http://$LAN_IP:$vnc_port)"
    echo "    Only ports published with 'ferry expose-vnc' are bridged. Stop with: ferry down"
    nohup "$FERRY_BIN_PATH" serve-vnc --foreground --port "$vnc_port" --bind "$bind_addr" \
      --marker ferry-vnc-marker > "$VNC_LOG" 2>&1 & disown
    sleep 1
    if lsof -nP -iTCP:"$vnc_port" -sTCP:LISTEN >/dev/null 2>&1; then
      echo ">>> Viewer is listening on port $vnc_port."
    else
      echo "    WARNING: the viewer is not listening. See $VNC_LOG"
    fi
    return 0
  fi

  # exec, not a child: `pkill -f ferry-vnc-marker` must hit the python that holds
  # the port, not a zsh wrapper in front of it (the relay learned this the hard way).
  exec python3 - "$vnc_port" "$bind_addr" "$RELAY_STATE_FILE" "$NOVNC_DIR" "$marker" <<'PYEOF'
import base64, hashlib, html, json, os, socket, struct, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

PORT, BIND, STATE_FILE, NOVNC_DIR = int(sys.argv[1]), sys.argv[2], sys.argv[3], os.path.realpath(sys.argv[4])
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
         ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
         ".ico": "image/x-icon", ".mp3": "audio/mpeg", ".oga": "audio/ogg",
         ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf"}

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def published_vnc():
    """{port: info} for every relay entry tagged vnc, read fresh on every call."""
    try:
        with open(STATE_FILE) as f:
            pub = json.load(f)
        return {int(p): i for p, i in pub.items() if isinstance(i, dict) and i.get("kind") == "vnc"}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}

def index_html():
    rows = []
    for port, info in sorted(published_vnc().items()):
        rows.append(f'<li><a href="/vnc/{port}">{html.escape(str(info.get("label") or "screen"))}</a>'
                    f' &mdash; {html.escape(str(info.get("client", "?")))}:{port},'
                    f' since {html.escape(str(info.get("since", "?")))}</li>')
    body = "<ul>" + "".join(rows) + "</ul>" if rows else "<p>Nothing published. On the laptop: <code>ferry expose-vnc</code></p>"
    return ("<!doctype html><meta charset=utf-8><title>ferry screens</title>"
            "<h1>Screens published through this host</h1>" + body).encode()

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def reply(self, status, body=b"", ctype="text/html; charset=utf-8", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self.reply(200, index_html())
        if path.startswith("/vnc/"):
            port = self.port_of(path[5:])
            if port not in published_vnc():
                return self.reply(404, b"no such screen")
            return self.reply(302, extra=[("Location", f"/novnc/vnc.html?autoconnect=1&resize=scale&path=ws/{port}")])
        if path.startswith("/novnc/"):
            return self.serve_static(unquote(path[7:]))
        if path.startswith("/ws/"):
            return self.serve_ws(self.port_of(path[4:]))
        self.reply(404, b"not found")

    @staticmethod
    def port_of(text):
        return int(text) if text.isdigit() else -1

    def serve_static(self, rel):
        full = os.path.realpath(os.path.join(NOVNC_DIR, rel))
        if not full.startswith(NOVNC_DIR + os.sep) or not os.path.isfile(full):
            return self.reply(404, b"not found")
        with open(full, "rb") as f:
            data = f.read()
        self.reply(200, data, TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def serve_ws(self, port):
        if port not in published_vnc():
            return self.reply(403, b"that port is not published as a VNC screen")
        # Task 4 replaces this line with the real bridge.
        self.reply(501, b"bridge not implemented")

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

srv = Server((BIND, PORT), Handler)
log(f"vnc viewer listening on {BIND}:{PORT}; noVNC from {NOVNC_DIR}; state {STATE_FILE}")
try:
    srv.serve_forever()
except KeyboardInterrupt:
    pass
PYEOF
}

# novnc_fetch — Task 5 fills this in.
novnc_fetch() {
  echo "Error: noVNC fetch is not implemented yet."
  return 1
}
