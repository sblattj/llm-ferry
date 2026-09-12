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
MAX_FRAME = 1 << 20   # a client-declared frame length is untrusted input; RFB never needs more
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

def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()

def frame(payload, opcode=0x2):
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload

def unmask(data, mask):
    if not data:
        return data
    m = (mask * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(len(data), "big")

def read_frame(rfile):
    """(opcode, payload, masked), or None at EOF / on a short read / on an oversized
    frame. Reads through the handler's buffered rfile so bytes that arrived with the
    request headers are not lost. EVERY read is length-checked: a client that vanishes
    mid-header would otherwise hand struct.unpack a short buffer, and struct.error is
    not an OSError, so it would escape the handler and dump a traceback into the log on
    a routine abrupt disconnect."""
    head = rfile.read(2)
    if len(head) < 2:
        return None
    opcode, n = head[0] & 0x0F, head[1] & 0x7F
    masked = bool(head[1] & 0x80)
    if n == 126:
        ext = rfile.read(2)
        if len(ext) < 2:
            return None
        n = struct.unpack("!H", ext)[0]
    elif n == 127:
        ext = rfile.read(8)
        if len(ext) < 8:
            return None
        n = struct.unpack("!Q", ext)[0]
    if n > MAX_FRAME:
        return None
    mask = None
    if masked:
        mask = rfile.read(4)
        if len(mask) < 4:
            return None
    data = rfile.read(n) if n else b""
    if len(data) < n:
        return None
    return opcode, (unmask(data, mask) if mask else data), masked

def pump_tcp_to_ws(upstream, ws, closing, lock):
    """TCP -> WS until upstream EOF. `closing` is set by the handler thread once it has
    already sent a close frame, so the shutdown it performs to wake this recv does not
    put a second, spurious close frame on a socket the client is watching for EOF.
    `lock` serialises sendall with the handler thread's pongs and close echo — without
    it a pong can splice into the middle of a large frame's payload."""
    try:
        while True:
            chunk = upstream.recv(65536)
            if not chunk:
                break
            with lock:
                ws.sendall(frame(chunk))
        if not closing.is_set():
            with lock:
                ws.sendall(frame(struct.pack("!H", 1000), 0x8))
    except OSError:
        pass

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
        key = self.headers.get("Sec-WebSocket-Key")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            return self.reply(400, b"expected a WebSocket upgrade")
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
        except OSError as e:
            log(f"bridge to 127.0.0.1:{port} failed: {e}")
            return self.reply(502, b"the published port did not answer")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        if "binary" in [p.strip() for p in self.headers.get("Sec-WebSocket-Protocol", "").split(",")]:
            self.send_header("Sec-WebSocket-Protocol", "binary")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        ws = self.connection
        upstream.settimeout(None)
        closing = threading.Event()
        lock = threading.Lock()   # both threads write to ws; every sendall holds this
        t = threading.Thread(target=pump_tcp_to_ws, args=(upstream, ws, closing, lock), daemon=True)
        t.start()
        log(f"bridge open {self.address_string()} -> 127.0.0.1:{port}")
        try:
            while True:
                got = read_frame(self.rfile)
                if got is None:
                    break
                opcode, data, masked = got
                if not masked:
                    # RFC 6455 6.1: a client frame must be masked; fail the connection.
                    closing.set()
                    try:
                        with lock:
                            ws.sendall(frame(struct.pack("!H", 1002), 0x8))
                    except OSError:
                        pass
                    break
                # FIN/fragmentation is deliberately ignored: this is a byte stream onto RFB.
                if opcode in (0x0, 0x1, 0x2):
                    upstream.sendall(data)
                elif opcode == 0x9:
                    with lock:
                        ws.sendall(frame(data, 0xA))
                elif opcode == 0x8:
                    closing.set()
                    try:
                        with lock:
                            ws.sendall(frame(data[:2], 0x8))
                    except OSError:
                        pass
                    break
                else:
                    log(f"ignoring unknown WebSocket opcode 0x{opcode:x} from {self.address_string()}")
        except OSError:
            pass
        finally:
            closing.set()
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            t.join(timeout=5)   # join BEFORE close: closing an fd another thread is
            upstream.close()    # blocked in recv() on is an fd-reuse hazard.
            try:
                ws.close()
            except OSError:
                pass
            log(f"bridge closed 127.0.0.1:{port}")

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

# novnc_fetch — one pinned tarball, checksum-verified, only the viewer tree kept.
# Downloaded rather than vendored: ~250 files of someone else's JS do not belong
# in a repo whose clients fetch `ferry` as one script. Re-run to change versions.
novnc_fetch() {
  echo ">>> Fetching noVNC $NOVNC_VERSION -> $NOVNC_DIR"
  python3 - "$NOVNC_URL" "$NOVNC_SHA256" "$NOVNC_DIR" "$NOVNC_VERSION" <<'PYEOF'
import hashlib, os, shutil, sys, tarfile, tempfile, urllib.request
url, want, dest, version = sys.argv[1:5]
prefix = f"noVNC-{version}/"
dirs = ("app", "core", "vendor")


def safe_member(m):
    """True if m's prefix-stripped .name is safe to extract: no absolute path, no
    '..' traversal segment, and either exactly "vnc.html" or under one of the
    app/core/vendor directories. This is the ONLY thing that protects extraction
    on pre-3.12 Python (no `filter=` kwarg, so it falls back below); on 3.12+
    tf.extractall(..., filter="data") is an additional backstop, not the sole
    line of defense — safety must not depend on the interpreter version."""
    rel = m.name
    if os.path.isabs(rel):
        return False
    norm = os.path.normpath(rel)
    if norm == ".." or norm.startswith(".." + os.sep) or any(seg == ".." for seg in norm.split(os.sep)):
        return False
    if not (m.isfile() or m.isdir()):
        return False
    top = norm.split(os.sep, 1)[0]
    return norm == "vnc.html" or top in dirs


tmp = tempfile.mkdtemp(prefix="ferry-novnc-")
try:
    tgz = os.path.join(tmp, "novnc.tar.gz")
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as r, open(tgz, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != want:
        print(f"Error: checksum mismatch for {url}\n       got  {h.hexdigest()}\n       want {want}")
        sys.exit(1)
    out = os.path.join(tmp, "tree")
    os.makedirs(out)
    with tarfile.open(tgz, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            if not m.name.startswith(prefix):
                continue
            m.name = m.name[len(prefix):]
            if not safe_member(m):
                continue
            members.append(m)
        try:
            tf.extractall(out, members=members, filter="data")
        except TypeError:                      # python < 3.12 has no filter kwarg
            tf.extractall(out, members=members)
    if not os.path.isfile(os.path.join(out, "vnc.html")):
        print("Error: the tarball has no vnc.html under " + prefix)
        sys.exit(1)
    with open(os.path.join(out, "VERSION"), "w") as f:
        f.write(version + "\n")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    old = dest + ".old"
    if os.path.isdir(old):                     # a stale leftover from a crash mid-swap
        shutil.rmtree(old)
    if os.path.isdir(dest):
        os.rename(dest, old)                    # keep the previous good install until
    shutil.move(out, dest)                      # the new one is fully in place
    if os.path.isdir(old):
        shutil.rmtree(old)
    print(f"    noVNC {version} installed ({sum(len(fs) for _, _, fs in os.walk(dest))} files).")
except (OSError, ValueError, tarfile.TarError) as e:
    print(f"Error: fetching noVNC failed: {e}")
    sys.exit(1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PYEOF
}
