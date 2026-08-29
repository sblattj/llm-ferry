
# Reverse expose — the symmetric half of ferry.
#
# Everything else in ferry pushes HOST -> CLIENT: inference, files, the forward
# proxy. This is the other direction, and it exists because of a topology ferry
# already assumes: a trusted host, and clients that can only reach it OUTBOUND.
# A locked-down laptop (a managed firewall that resets inbound connections, a
# corporate proxy that kills every tunnel service) can still publish its own local
# service — an opencode server, a dev server, a notebook — to a phone or another
# machine on the LAN, by dialling the host and letting the host do the listening.
#
#   host:    ferry relay                      # accept registrations, publish ports
#   client:  ferry expose 4290 --as 4290      # "serve my 127.0.0.1:4290 from the host"
#
# HOW THE BYTES MOVE. Two kinds of connection, BOTH dialled by the client, so
# nothing ever connects INTO the client:
#
#   control  client -> relay, held open. Carries {"op":"register"}, then one
#            {"op":"open","id":N} from the relay per inbound public connection.
#   data     client -> relay, one per public connection. The client dials its own
#            127.0.0.1:<local-port> and pumps bytes between the two sockets.
#
# The relay parks each accepted public socket until its matching data connection
# arrives, then splices them. When the control connection drops, the public
# listener and everything parked behind it are closed — an exposure cannot outlive
# the client that asked for it.
#
# WHAT THIS IS NOT: an auth layer for the service being exposed. The token proves
# that whoever REGISTERED is your client; it says nothing about whoever connects
# to the published port. Expose something that has its own authentication.
cmd_relay() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry relay' is only available on the LLM-Ferry Host Mac."
    echo "       The client side is 'ferry expose <port>'."
    exit 1
  fi

  local relay_port="$RELAY_PORT" bind_addr="0.0.0.0" foreground=0 show_token=0
  # Default, not empty: a hand-run `ferry relay --foreground` should be just as
  # killable by `ferry down` as a backgrounded one.
  local marker="ferry-relay-marker"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port)       relay_port="$2"; shift 2 ;;
      --bind)       bind_addr="$2"; shift 2 ;;
      --foreground) foreground=1; shift ;;
      --token)      show_token=1; shift ;;
      # Sentinel carried into the SERVER's argv so `ferry down` can find it; the
      # same trick cmd_share uses. It must ride on the process that actually holds
      # the port, which is why the python is exec'd below rather than spawned.
      --marker)     marker="$2"; shift 2 ;;
      -h|--help)
        echo "Usage: ferry relay [--port P] [--bind ADDR] [--foreground] [--token]"
        echo "  --port P      control port clients dial [default: $RELAY_PORT]"
        echo "  --bind ADDR   what published ports bind to [default: 0.0.0.0, i.e. the LAN]"
        echo "                  --bind 127.0.0.1 keeps an exposure on this machine only"
        echo "  --foreground  run in this terminal instead of the background"
        echo "  --token       print the shared token and exit"
        echo "Stop it with 'ferry down'."
        return 0 ;;
      *) echo "Unknown option for 'ferry relay': $1"; exit 1 ;;
    esac
  done

  # The token is what separates "a client of yours" from "anything on the LAN".
  # Generated once, 0600, and never written to a log — only to this terminal,
  # because the human is the transport that carries it to the client.
  mkdir -p "$HOME/.config/ferry"
  if [[ ! -f "$RELAY_TOKEN_FILE" ]]; then
    python3 -c "import secrets; print(secrets.token_urlsafe(24))" > "$RELAY_TOKEN_FILE"
    chmod 600 "$RELAY_TOKEN_FILE"
  fi
  local token; token="$(cat "$RELAY_TOKEN_FILE")"

  if (( show_token )); then
    echo "$token"
    return 0
  fi

  # Ports ferry itself owns are refused as publish targets, so an exposure can
  # never quietly shadow the inference endpoint or the share server. Built from
  # the same constants those services use — 8091 is the dashboard, which is a
  # literal there too.
  local reserved="$PORT,8091,$SHARE_PORT,$HF_PORT,$PROXY_PORT,$LOCAL_ORCH_PORT,$LOCAL_SUB_PORT,$relay_port"

  if (( ! foreground )); then
    if lsof -nP -iTCP:"$relay_port" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "Error: port $relay_port is already in use — a relay may already be running."
      echo "       'ferry down' stops it, or pass --port to use another."
      exit 1
    fi

    echo "================================================================="
    echo "                    LLM-FERRY REVERSE RELAY"
    echo "================================================================="
    echo "Control port:   $relay_port   (clients dial this, outbound)"
    echo "Published ports bind to: $bind_addr"
    echo "Log:            $RELAY_LOG"
    echo "================================================================="
    echo ">>> On the client, publish a local service through this host:"
    echo "    \033[1;32mferry expose <local-port> --as <public-port> --token $token\033[0m"
    echo "    (the token is saved on the client after the first successful run)"
    echo ""
    echo ">>> Whatever you expose keeps its OWN auth. The relay authenticates the"
    echo "    client that registers, never the visitors who reach the port."
    echo "================================================================="

    _ferry_reset_log "$RELAY_LOG"
    # Re-invoke this same script in --foreground rather than duplicating the
    # server: one implementation, one heredoc, and the sentinel lands in argv.
    nohup "$FERRY_BIN_PATH" relay --foreground --port "$relay_port" --bind "$bind_addr" \
      --marker ferry-relay-marker > "$RELAY_LOG" 2>&1 & disown
    sleep 1
    if lsof -nP -iTCP:"$relay_port" -sTCP:LISTEN >/dev/null 2>&1; then
      echo ">>> Relay running in the background. 'ferry status' lists published ports;"
      echo "    'ferry down' stops it."
    else
      echo "    WARNING: the relay is not listening. See $RELAY_LOG"
      exit 1
    fi
    return 0
  fi

  # exec, and the sentinel as the last argv: the process that ends up holding the
  # port must be the one `ferry down` can match. Spawning the python as a child of
  # this shell put the sentinel on the PARENT — `pkill -f ferry-relay-marker` then
  # killed the wrapper, reported success, and left the relay listening. The python
  # reads argv[1:6] and ignores the rest, exactly as the share server does.
  exec python3 - "$relay_port" "$bind_addr" "$RELAY_TOKEN_FILE" "$RELAY_STATE_FILE" "$reserved" "$marker" <<'PYEOF'
import hmac, json, os, socket, sys, threading, time

port, bind_addr, token_file, state_file, reserved_csv = sys.argv[1:6]
port = int(port)
reserved = {int(p) for p in reserved_csv.split(",") if p.strip()}

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def token():
    with open(token_file) as f:
        return f.read().strip()

def read_line(sock, limit=4096):
    """Read one \\n-terminated line ONE BYTE AT A TIME.

    Deliberately not sock.makefile(): a buffered reader can swallow the first
    bytes of the stream that follows the handshake into its readahead, and on a
    data connection whose service greets first (SSH, SMTP, anything chatty) those
    bytes are then lost with no error anywhere.
    """
    buf = bytearray()
    while len(buf) < limit:
        b = sock.recv(1)
        if not b:
            return None
        if b == b"\n":
            return bytes(buf)
        buf += b
    return None

def send_json(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode())

# --- shared state -----------------------------------------------------------
pending = {}                       # conn id -> public socket awaiting its data conn
pending_lock = threading.Lock()
published = {}                     # public port -> what `ferry status` reports
published_lock = threading.Lock()

def write_state():
    with published_lock:
        snapshot = {str(k): v for k, v in published.items()}
    tmp = state_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp, state_file)     # atomic: `ferry status` never reads a half-file

def close_quietly(sock):
    try:
        sock.close()
    except OSError:
        pass

def pump(src, dst):
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

def splice(a, b):
    """Pump both directions, and do not return until both are done."""
    t = threading.Thread(target=pump, args=(a, b), daemon=True)
    t.start()
    pump(b, a)
    t.join(timeout=5)
    close_quietly(a)
    close_quietly(b)

# --- one registered client --------------------------------------------------
def serve_registration(ctrl, addr, req):
    public_port = int(req.get("public_port", 0))
    label = str(req.get("label", ""))[:120]
    if public_port < 1024 or public_port > 65535:
        send_json(ctrl, {"ok": False, "error": f"public port {public_port} out of range (1024-65535)"})
        return
    if public_port in reserved:
        send_json(ctrl, {"ok": False,
                         "error": f"port {public_port} belongs to ferry itself; pick another"})
        return

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((bind_addr, public_port))
        listener.listen(64)
    except OSError as e:
        send_json(ctrl, {"ok": False, "error": f"cannot bind {bind_addr}:{public_port} ({e})"})
        close_quietly(listener)
        return

    send_json(ctrl, {"ok": True, "public_port": public_port, "bind": bind_addr})
    with published_lock:
        published[public_port] = {"client": addr[0], "label": label,
                                  "since": time.strftime("%Y-%m-%d %H:%M:%S"), "bind": bind_addr}
    write_state()
    log(f"published {bind_addr}:{public_port} for {addr[0]} {('(' + label + ')') if label else ''}")

    ctrl_lock = threading.Lock()
    stop = threading.Event()
    counter = [0]

    def accept_loop():
        while not stop.is_set():
            try:
                pub, who = listener.accept()
            except OSError:
                break
            counter[0] += 1
            cid = counter[0]
            with pending_lock:
                pending[cid] = pub
            try:
                with ctrl_lock:
                    send_json(ctrl, {"op": "open", "id": cid})
            except OSError:
                with pending_lock:
                    pending.pop(cid, None)
                close_quietly(pub)
                break
            # A client that never dials back must not leak the parked socket.
            threading.Timer(30.0, reap, args=(cid,)).start()

    def reap(cid):
        with pending_lock:
            sock = pending.pop(cid, None)
        if sock is not None:
            log(f"conn {cid} was never claimed by the client — dropping it")
            close_quietly(sock)

    acceptor = threading.Thread(target=accept_loop, daemon=True)
    acceptor.start()

    # The control connection carries nothing else from the client, so a read that
    # returns empty IS the disconnect. That is the teardown signal.
    try:
        while True:
            if not ctrl.recv(1):
                break
    except OSError:
        pass
    finally:
        stop.set()
        close_quietly(listener)
        with published_lock:
            published.pop(public_port, None)
        write_state()
        with pending_lock:
            orphans = list(pending.values())
            pending.clear()
        for sock in orphans:
            close_quietly(sock)
        log(f"unpublished {bind_addr}:{public_port} (client {addr[0]} disconnected)")

def handle(sock, addr):
    sock.settimeout(20)
    line = read_line(sock)
    if line is None:
        close_quietly(sock)
        return
    try:
        req = json.loads(line.decode())
    except ValueError:
        close_quietly(sock)
        return
    if not hmac.compare_digest(str(req.get("token", "")), token()):
        log(f"rejected {req.get('op')} from {addr[0]}: bad token")
        try:
            send_json(sock, {"ok": False, "error": "bad token"})
        except OSError:
            pass
        close_quietly(sock)
        return

    op = req.get("op")
    if op == "register":
        sock.settimeout(None)
        # Keepalive on the control connection, because teardown is driven by
        # noticing that it closed. A client that vanishes WITHOUT closing — a lid
        # shut, Wi-Fi dropped, a laptop carried out of the building — leaves a
        # bare TCP socket that never reports anything, and the host would keep a
        # port published for an absent machine indefinitely.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPALIVE", 60),
                           ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
            if hasattr(socket, opt):
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)
                except OSError:
                    pass
        serve_registration(sock, addr, req)
        close_quietly(sock)
    elif op == "data":
        cid = int(req.get("id", 0))
        with pending_lock:
            pub = pending.pop(cid, None)
        if pub is None:
            close_quietly(sock)
            return
        sock.settimeout(None)
        pub.settimeout(None)
        splice(pub, sock)
    else:
        close_quietly(sock)

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", port))
server.listen(64)
write_state()
log(f"relay control listening on 0.0.0.0:{port}; published ports bind {bind_addr}")
try:
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
except KeyboardInterrupt:
    pass
finally:
    with published_lock:
        published.clear()
    write_state()
PYEOF
}

# cmd_expose — the client half. Foreground on purpose, like `ssh -N -R`: the
# exposure lasts exactly as long as you can see it running, so a laptop that
# closes its lid stops publishing instead of leaving a port open on the host.
cmd_expose() {
  local local_port="" public_port="" host="${CLIENT_HOST:-}" relay_port="$RELAY_PORT" token=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --as)    public_port="$2"; shift 2 ;;
      --host)  host="$2"; shift 2 ;;
      --port)  relay_port="$2"; shift 2 ;;
      --token) token="$2"; shift 2 ;;
      -h|--help)
        echo "Usage: ferry expose <local-port> [--as PUBLIC] [--host H] [--port P] [--token T]"
        echo "  <local-port>  the port on THIS machine to publish (dialled as 127.0.0.1)"
        echo "  --as PUBLIC   the port it is served from on the host [default: same number]"
        echo "  --host H      the ferry host [default: the client profile's host]"
        echo "  --port P      the host's relay control port [default: $RELAY_PORT]"
        echo "  --token T     the relay token ('ferry relay --token' on the host); saved to"
        echo "                  ~/.config/ferry/relay-token after a first successful run"
        echo "Runs in the foreground — Ctrl-C stops publishing."
        return 0 ;;
      -*) echo "Unknown option for 'ferry expose': $1"; exit 1 ;;
      *)  if [[ -z "$local_port" ]]; then local_port="$1"; shift
          else echo "Unexpected argument: $1"; exit 1; fi ;;
    esac
  done

  if [[ -z "$local_port" ]]; then
    echo "Usage: ferry expose <local-port> [--as PUBLIC] [--host H] [--port P] [--token T]"
    exit 1
  fi
  public_port="${public_port:-$local_port}"

  if [[ -z "$host" ]]; then
    echo "Error: no host. Pass --host <mdns-or-ip>, or bootstrap this machine first"
    echo "       (a client profile at ~/.config/ferry/client.json supplies one)."
    exit 1
  fi

  # Token precedence: the flag, the environment, then whatever a previous run saved.
  [[ -z "$token" ]] && token="${FERRY_RELAY_TOKEN:-}"
  if [[ -z "$token" && -f "$RELAY_TOKEN_FILE" ]]; then
    token="$(cat "$RELAY_TOKEN_FILE")"
  fi
  if [[ -z "$token" ]]; then
    echo "Error: no relay token. Run 'ferry relay --token' on the host, then:"
    echo "       ferry expose $local_port --as $public_port --token <token>"
    exit 1
  fi

  echo ">>> Publishing 127.0.0.1:$local_port  ->  $host:$public_port"
  echo "    (through the relay control port $host:$relay_port — this machine only dials OUT)"
  echo "    Ctrl-C to stop."
  # exec, not a child: the tunnel's lifetime IS this process's lifetime. Run the
  # python as a child and `kill <the pid you started>` kills only the zsh wrapper,
  # leaving the control connection open and the host still publishing a port whose
  # client is gone. Interactive Ctrl-C signals the whole process group and papers
  # over that; anything supervising ferry by pid does not.
  exec python3 - "$host" "$relay_port" "$local_port" "$public_port" "$token" "$RELAY_TOKEN_FILE" "$(hostname -s 2>/dev/null || echo client)" <<'PYEOF'
import json, os, socket, sys, threading, time

host, relay_port, local_port, public_port, token, token_file, label = sys.argv[1:8]
relay_port, local_port, public_port = int(relay_port), int(local_port), int(public_port)

def read_line(sock, limit=4096):
    """One byte at a time — see the note in the relay half."""
    buf = bytearray()
    while len(buf) < limit:
        b = sock.recv(1)
        if not b:
            return None
        if b == b"\n":
            return bytes(buf)
        buf += b
    return None

def send_json(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode())

def pump(src, dst):
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

def splice(a, b):
    t = threading.Thread(target=pump, args=(a, b), daemon=True)
    t.start()
    pump(b, a)
    t.join(timeout=5)
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass

def serve_one(cid):
    """One inbound public connection: dial our own service, dial the relay, splice."""
    try:
        local = socket.create_connection(("127.0.0.1", local_port), timeout=5)
    except OSError as e:
        print(f"    local service refused ({e}) — dropping connection {cid}", flush=True)
        # Still claim the parked socket so the relay stops holding it open.
        try:
            data = socket.create_connection((host, relay_port), timeout=10)
            send_json(data, {"op": "data", "token": token, "id": cid})
            data.close()
        except OSError:
            pass
        return
    try:
        data = socket.create_connection((host, relay_port), timeout=10)
        send_json(data, {"op": "data", "token": token, "id": cid})
    except OSError as e:
        print(f"    could not open a data channel ({e})", flush=True)
        local.close()
        return
    splice(local, data)

try:
    ctrl = socket.create_connection((host, relay_port), timeout=10)
except OSError as e:
    print(f"Error: cannot reach the relay at {host}:{relay_port} ({e})")
    print("       Is 'ferry relay' running on the host?")
    sys.exit(1)

send_json(ctrl, {"op": "register", "token": token, "public_port": public_port, "label": label})
reply = read_line(ctrl)
if reply is None:
    print("Error: the relay closed the connection during registration.")
    sys.exit(1)
try:
    resp = json.loads(reply.decode())
except ValueError:
    print("Error: unreadable reply from the relay.")
    sys.exit(1)
if not resp.get("ok"):
    print(f"Error: the relay refused the registration: {resp.get('error', 'unknown reason')}")
    sys.exit(1)

# Only now is the token known-good, so only now is it worth keeping.
try:
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    if not os.path.exists(token_file):
        with open(token_file, "w") as f:
            f.write(token + "\n")
        os.chmod(token_file, 0o600)
except OSError:
    pass

print(f"    Published. Visitors reach it at {resp.get('bind')}:{resp['public_port']} on the host.",
      flush=True)

ctrl.settimeout(None)
try:
    while True:
        line = read_line(ctrl)
        if line is None:
            print("\n>>> The relay closed the tunnel (host stopped, or 'ferry down').")
            sys.exit(1)
        try:
            msg = json.loads(line.decode())
        except ValueError:
            continue
        if msg.get("op") == "open":
            threading.Thread(target=serve_one, args=(int(msg["id"]),), daemon=True).start()
except KeyboardInterrupt:
    print("\n>>> Stopped publishing.")
finally:
    try:
        ctrl.close()
    except OSError:
        pass
PYEOF
}
