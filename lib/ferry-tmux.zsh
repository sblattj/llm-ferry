
# ============================================================================
# tmux over ssh, through the relay
#
# The HOST wants a shell on a CLIENT laptop. The client never accepts inbound
# connections, so it publishes its own sshd through the reverse relay
# (`ferry expose-tmux`, tagged kind "tmux"), and the host ssh's to the published
# port and attaches a tmux session (`ferry tmux`). ssh is what authenticates and
# encrypts: the relay token only authenticates the PUBLISHER. The client's sshd is
# either the system one (Remote Login, needs admin) or, by default when that is off,
# a no-sudo sshd `ferry expose-tmux` runs itself that trusts only the host's ferry
# key (TMUX_KEY_FILE, minted by `ferry relay`, fetched over the relay's hostkey op).
# ============================================================================

# ssh_preflight <port> — refuse to publish a port that is not speaking SSH.
# An sshd greets FIRST ("SSH-2.0-OpenSSH_x"), so one read settles it. As with
# rfb_preflight, refused-connect and wrong-greeting are different failures.
ssh_preflight() {
  python3 - "$1" <<'PYEOF'
import socket, sys
port = int(sys.argv[1])
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
except OSError as e:
    print(f"Error: nothing is listening on 127.0.0.1:{port} ({e}).")
    print("       Turn on Remote Login (System Settings > General > Sharing > Remote Login),")
    print("       then run this again.")
    sys.exit(1)
with s:
    s.settimeout(3)
    try:
        greeting = s.recv(64)
    except OSError:
        greeting = b""
    if not greeting.startswith(b"SSH-"):
        print(f"Error: 127.0.0.1:{port} is not an SSH server (no SSH- greeting; got {greeting[:16]!r}).")
        sys.exit(1)
    banner = greeting.decode(errors="replace").splitlines()[0]
    print(f"    SSH server on 127.0.0.1:{port} greets {banner}")
PYEOF
}

# _ssh_wait <port> <seconds> — poll 127.0.0.1:<port> for an "SSH-" greeting. Quiet:
# exit 0 once one arrives, 1 on timeout. Used both to probe for a system sshd and
# to wait for our own to come up.
_ssh_wait() {
  python3 - "$1" "$2" <<'PYEOF'
import socket, sys, time
port, secs = int(sys.argv[1]), float(sys.argv[2])
end = time.time() + secs
while True:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
            s.settimeout(1)
            if s.recv(64).startswith(b"SSH-"):
                sys.exit(0)
    except OSError:
        pass
    if time.time() >= end:
        sys.exit(1)
    time.sleep(0.1)
PYEOF
}

# _free_port — a free 127.0.0.1 port, found at runtime (bind 0, read it, close).
_free_port() {
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

# _tmux_fetch_hostkey <host> <relay-port> <token> — print the host's ferry tmux
# public key, asked of the relay with a token-authenticated {"op":"hostkey"}.
# rc 0 key on stdout; rc 1 anything else, with the reason on stdout.
_tmux_fetch_hostkey() {
  python3 - "$1" "$2" "$3" <<'PYEOF'
import json, socket, sys

host, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]

def read_line(sock, limit=8192):
    buf = bytearray()
    while len(buf) < limit:
        b = sock.recv(1)
        if not b:
            return None
        if b == b"\n":
            return bytes(buf)
        buf += b
    return None

try:
    s = socket.create_connection((host, port), timeout=10)
except OSError as e:
    print(f"Error: cannot reach the relay at {host}:{port} ({e})")
    print("       Is 'ferry relay' running on the host?")
    sys.exit(1)
with s:
    s.settimeout(10)
    try:
        s.sendall((json.dumps({"op": "hostkey", "token": token}) + "\n").encode())
        line = read_line(s)
    except (ConnectionResetError, BrokenPipeError):
        line = None     # closed on us: the same "no answer" as a clean EOF
    except OSError as e:
        print(f"Error: the relay did not answer the hostkey request ({e})")
        sys.exit(1)
if line is None:
    # An older relay does not know the op and just closes the connection.
    print("Error: the host's relay predates this feature (it closed without answering).")
    print("       On the host run 'ferry update', then restart the relay")
    print("       ('ferry down' and 'ferry relay', or kill ferry-relay-marker').")
    sys.exit(1)
try:
    resp = json.loads(line.decode())
except ValueError:
    print("Error: unreadable reply from the relay.")
    sys.exit(1)
if not resp.get("ok"):
    err = resp.get("error", "unknown reason")
    print(f"Error: the relay refused the hostkey request: {err}")
    if err == "bad token":
        print("       Run 'ferry relay --token' on the host and pass --token <token>.")
    sys.exit(1)
key = resp.get("pubkey")
if (not isinstance(key, str) or "\n" in key or "\r" in key
        or not key.startswith("ssh-") or len(key.split()) < 2):
    print("Error: the relay returned something that is not a one-line ssh public key.")
    sys.exit(1)
print(key)
PYEOF
}

# _tmux_user_sshd_start <host> <relay-port> <token> — start the no-sudo sshd.
# Sets REPLY_PORT and REPLY_PID in the caller's scope (the caller declares them).
_tmux_user_sshd_start() {
  local host="$1" relay_port="$2" token="$3"
  local dir="$TMUX_SSHD_DIR" pubkey
  if [[ ! -x /usr/sbin/sshd ]]; then
    echo "Error: /usr/sbin/sshd not found; the no-sudo sshd needs OpenSSH's server binary."
    return 1
  fi
  pubkey="$(_tmux_fetch_hostkey "$host" "$relay_port" "$token")" || { echo "$pubkey"; return 1; }

  mkdir -p "$dir" && chmod 700 "$dir"
  # One host key, made once: the host's known_hosts then stays valid across runs.
  if [[ ! -f "$dir/hostkey" ]]; then
    ssh-keygen -q -t ed25519 -N '' -C "ferry-tmux-sshd" -f "$dir/hostkey" >/dev/null || return 1
    chmod 600 "$dir/hostkey"
  fi
  print -r -- "$pubkey" > "$dir/authorized_keys"; chmod 600 "$dir/authorized_keys"

  # A previous run that crashed leaves its sshd behind; only kill a process that is
  # provably ours (its command line names OUR config), never whatever reused the pid.
  if [[ -f "$dir/sshd.pid" ]]; then
    local old; old="$(<"$dir/sshd.pid")"
    if [[ "$old" == <-> ]] && ps -p "$old" -o command= 2>/dev/null | grep -qF "$dir/sshd_config"; then
      kill "$old" 2>/dev/null
      sleep 0.3
    fi
  fi

  REPLY_PORT="$(_free_port)"
  cat > "$dir/sshd_config" <<EOF
Port $REPLY_PORT
ListenAddress 127.0.0.1
HostKey $dir/hostkey
AuthorizedKeysFile $dir/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM no
StrictModes no
PermitRootLogin no
AllowTcpForwarding no
X11Forwarding no
PermitTunnel no
PidFile $dir/sshd.pid
EOF
  : > "$dir/sshd.log"
  /usr/sbin/sshd -D -f "$dir/sshd_config" -E "$dir/sshd.log" &
  REPLY_PID=$!
  disown 2>/dev/null
  if ! _ssh_wait "$REPLY_PORT" 5; then
    echo "Error: the no-sudo sshd did not come up on 127.0.0.1:$REPLY_PORT. Its log ($dir/sshd.log):"
    tail -n 15 "$dir/sshd.log" | sed 's/^/    /'
    kill "$REPLY_PID" 2>/dev/null
    return 1
  fi
  return 0
}

# cmd_expose_tmux — `ferry expose <sshd port> --as 8101` with a tmux + sshd
# preflight and a kind tag, so the host can list this machine for `ferry tmux`.
# The sshd is the system one on 22 (Remote Login) if it is there, else a no-sudo
# sshd of our own on a free high port that trusts only the host's ferry key.
cmd_expose_tmux() {
  local local_port="" mode="" passthrough=() have_as=0 host="${CLIENT_HOST:-}" relay_port="$RELAY_PORT" token=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --local)
        if [[ $# -lt 2 ]]; then echo "Error: --local needs a port number"; exit 1; fi
        local_port="$2"; shift 2 ;;
      --user-sshd)   mode="user"; shift ;;
      --system-sshd) mode="system"; shift ;;
      --as) have_as=1; passthrough+=("$1"); shift
            if [[ $# -gt 0 ]]; then passthrough+=("$1"); shift; fi ;;
      --host)  host="$2"; passthrough+=("$1" "$2"); shift 2 ;;
      --port)  relay_port="$2"; passthrough+=("$1" "$2"); shift 2 ;;
      --token) token="$2"; passthrough+=("$1" "$2"); shift 2 ;;
      -h|--help)
        echo "Usage: ferry expose-tmux [--user-sshd | --system-sshd | --local PORT] [--as PUBLIC] [--host H] [--port P] [--token T]"
        echo "  Default: use this machine's sshd on 22 if Remote Login is on, else run a no-sudo sshd"
        echo "  --user-sshd    always run our own sshd (127.0.0.1, pubkey-only, only the host's ferry key)"
        echo "  --system-sshd  require the sshd on port 22 (needs Remote Login in System Settings)"
        echo "  --local PORT   publish the sshd already listening on PORT"
        echo "  --as PUBLIC    the port it is served from on the host [default: $TMUX_PORT]"
        echo "  (--host/--port/--token and the rest go to 'ferry expose'; tmux must be installed)"
        echo "The host then runs 'ferry tmux'. Runs in the foreground; Ctrl-C stops publishing."
        return 0 ;;
      *) passthrough+=("$1"); shift ;;
    esac
  done
  if [[ -n "$local_port" && ( -n "${local_port//[0-9]/}" ) ]]; then
    echo "Error: --local must be a port number (got '$local_port')"
    exit 1
  fi
  if [[ -n "$local_port" && "$mode" == "user" ]]; then
    echo "Error: --local publishes an sshd that already exists; it cannot be combined with --user-sshd"
    exit 1
  fi
  if ! command -v tmux >/dev/null 2>&1; then
    echo "Error: tmux is not installed on this machine (the host attaches a tmux session here)."
    echo "       Install it:  brew install tmux"
    exit 1
  fi
  (( have_as )) || passthrough=(--as "$TMUX_PORT" "${passthrough[@]}")

  # Which sshd. An explicit --local or --system-sshd keeps the original behaviour.
  if [[ -n "$local_port" ]]; then
    mode="local"
  elif [[ "$mode" == "system" ]]; then
    local_port="22"
  elif [[ -z "$mode" ]]; then
    if _ssh_wait 22 1; then
      mode="system"; local_port="22"
      echo ">>> Using this machine's sshd on port 22 (Remote Login is on)."
      echo "    '--user-sshd' avoids Remote Login by running a no-sudo sshd instead."
    else
      mode="user"
      echo ">>> Remote Login is off (nothing greets on 127.0.0.1:22): running a no-sudo sshd instead."
    fi
  fi

  local kind="tmux" expose_sshd="" expose_companion_pid=""
  if [[ "$mode" != "user" ]]; then
    ssh_preflight "$local_port" || exit 1
    cmd_expose "$local_port" "${passthrough[@]}"
    return
  fi

  # The same host and token a bare `ferry expose` would settle on, checked up front
  # so a missing one fails BEFORE an sshd is started that nothing would stop.
  if [[ -z "$host" ]]; then
    echo "Error: no host. Pass --host <mdns-or-ip>, or bootstrap this machine first"
    echo "       (a client profile at ~/.config/ferry/client.json supplies one)."
    exit 1
  fi
  token="$(_relay_token_resolve "$token")"
  if [[ -z "$token" ]]; then
    echo "Error: no relay token. Run 'ferry relay --token' on the host, then:"
    echo "       ferry expose-tmux --token <token>"
    exit 1
  fi
  local REPLY_PORT="" REPLY_PID=""
  _tmux_user_sshd_start "$host" "$relay_port" "$token" || exit 1
  echo "    No-sudo sshd on 127.0.0.1:$REPLY_PORT (pid $REPLY_PID); it stops with this command."
  expose_sshd="user"
  expose_companion_pid="$REPLY_PID"
  cmd_expose "$REPLY_PORT" "${passthrough[@]}"
}

# cmd_tmux — the host half: pick a published tmux client from the relay state and
# `ssh -t` into it, attaching (or creating) a tmux session.
cmd_tmux() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry tmux' is only available on the LLM-Ferry Host Mac."
    echo "       The client side is 'ferry expose-tmux'."
    exit 1
  fi
  local client="" session="ferry" user="" list=0 print_only=0 dir_args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --list)    list=1; shift ;;
      --print)   print_only=1; shift ;;
      --session) if [[ $# -lt 2 ]]; then echo "Error: --session needs a name"; exit 1; fi
                 session="$2"; shift 2 ;;
      --user)    if [[ $# -lt 2 ]]; then echo "Error: --user needs a name"; exit 1; fi
                 user="$2"; shift 2 ;;
      --dir)     if [[ $# -lt 2 ]]; then echo "Error: --dir needs a path"; exit 1; fi
                 dir_args=("$2"); shift 2 ;;
      -h|--help)
        echo "Usage: ferry tmux [CLIENT] [--list] [--session NAME] [--user U] [--dir PATH] [--print]"
        echo "  CLIENT        a client's label or its published port [default: the only one]"
        echo "  --list        list clients that ran 'ferry expose-tmux' and exit"
        echo "  --session N   the tmux session to attach or create [default: ferry]"
        echo "  --user U      the login on the client [default: what the client reported]"
        echo "  --dir PATH    where a NEW session starts (tmux -c); PATH is a path on the client, and a"
        echo "                leading ~ or ~/ means the client user's home. Ignored when the session already"
        echo "                exists (tmux ignores -c when -A attaches)"
        echo "  --print       print the ssh command instead of running it"
        echo "ssh authenticates you: with the host's ferry key (~/.config/ferry/tmux_ed25519, made by"
        echo "'ferry relay') if present, else your own keys. A client on '--user-sshd' accepts only the former."
        return 0 ;;
      -*) echo "Unknown option for 'ferry tmux': $1"; exit 1 ;;
      *)  if [[ -z "$client" ]]; then client="$1"; shift
          else echo "Unexpected argument: $1"; exit 1; fi ;;
    esac
  done

  local out rc=0
  out="$(python3 - "$RELAY_STATE_FILE" "$list" "$client" "$session" "$user" "$TMUX_KEY_FILE" "${dir_args[@]}" <<'PYEOF'
import json, os, re, shlex, sys

state_file, list_mode, client, session, user, key_file = sys.argv[1:7]
dir_given = len(sys.argv) > 7
start_dir = sys.argv[7] if dir_given else ""
SAFE = re.compile(r"[A-Za-z0-9._-]+")

try:
    with open(state_file) as f:
        pub = json.load(f)
    if not isinstance(pub, dict):
        pub = {}
except (OSError, ValueError):
    pub = {}
seats = sorted(((int(p), i) for p, i in pub.items()
                if str(p).isascii() and str(p).isdigit() and isinstance(i, dict)
                and i.get("kind") == "tmux"), key=lambda kv: kv[0])

def line(port, i):
    return (f"  {i.get('label') or '-'}  {i.get('client', '?')}  port {port}  "
            f"user {i.get('user') or '-'}  since {i.get('since', '?')}")

def fail(msg, show=True):
    print(f"Error: {msg}")
    if show:
        for port, i in seats:
            print(line(port, i))
    sys.exit(1)

if list_mode == "1":
    if not seats:
        print("No clients have published tmux (client: ferry expose-tmux).")
    for port, i in seats:
        print(f"{i.get('label') or '-'}  {i.get('client', '?')}  {port}  "
              f"{i.get('user') or '-'}  {i.get('since', '?')}")
    sys.exit(0)

if not seats:
    fail("no client has published tmux. On the client run: ferry expose-tmux", show=False)

if client:
    hit = [(p, i) for p, i in seats if i.get("label") == client or str(p) == client]
    if not hit:
        fail(f"no tmux client matches '{client}'. Published:")
    if len(hit) > 1:
        fail(f"'{client}' matches several clients; pass the port number instead:")
    port, info = hit[0]
elif len(seats) == 1:
    port, info = seats[0]
else:
    fail("several clients are published; name one (label or port):")

user = user or info.get("user") or ""
if not user:
    fail(f"the relay has no login name for '{info.get('label') or port}'; pass --user NAME", show=False)
if not SAFE.fullmatch(user) or user.startswith("-"):
    fail(f"--user '{user}' is not a plain login name ([A-Za-z0-9._-], no leading '-')", show=False)
if not SAFE.fullmatch(session):
    fail(f"--session '{session}' must match [A-Za-z0-9._-]+", show=False)

# --dir is a path on the CLIENT. A leading ~ or ~/ is the client user's home, left
# for the remote shell to expand as "$HOME"; everything else is single-quoted.
dir_opt = ""
if dir_given:
    if start_dir == "":
        fail("--dir needs a non-empty path", show=False)
    if any(c in start_dir for c in "\n\r\0"):
        fail("--dir must not contain a newline, carriage return or NUL", show=False)
    if start_dir == "~":
        dir_opt = ' -c "$HOME"'
    elif start_dir.startswith("~/"):
        rest = start_dir[2:]
        dir_opt = ' -c "$HOME"' + ("/" + shlex.quote(rest) if rest else "/")
    elif start_dir.startswith("~"):
        fail(f"--dir '{start_dir}': only ~ and ~/... are supported (not ~otheruser)", show=False)
    else:
        dir_opt = " -c " + shlex.quote(start_dir)

bind = str(info.get("bind") or "")
addr = "127.0.0.1" if bind in ("", "0.0.0.0") else bind
if not re.fullmatch(r"[A-Za-z0-9.:-]+", addr) or addr.startswith("-"):
    fail(f"the relay reports an unusable bind address '{bind}'", show=False)

# HostKeyAlias keeps known_hosts entries per client: a reused port must not
# collide with the previous client's host key.
alias = "ferry-tmux-" + re.sub(r"[^A-Za-z0-9._-]", "_", str(info.get("label") or port))
# A client's no-sudo sshd has its own host key, not the system sshd's on the same
# laptop; sharing one alias would trip "REMOTE HOST IDENTIFICATION HAS CHANGED".
if info.get("sshd") == "user":
    alias += "-user"
# Non-interactive macOS ssh has no Homebrew on PATH, so tmux would not be found.
remote = f"PATH=/opt/homebrew/bin:/usr/local/bin:$PATH exec tmux new-session -A -s {session}{dir_opt}"
argv = ["ssh", "-p", str(port), "-o", f"HostKeyAlias={alias}"]
# The host's ferry key, when `ferry relay` has made one. No IdentitiesOnly: a system
# sshd must still be offered the user's default keys.
if os.path.exists(key_file):
    argv += ["-i", key_file]
argv += ["-t", f"{user}@{addr}", remote]
print(shlex.join(argv))
PYEOF
)" || rc=$?
  if (( rc != 0 )); then
    echo "$out"
    exit 1
  fi
  if (( list )); then
    echo "$out"
    return 0
  fi
  if (( print_only )); then
    echo "$out"
    return 0
  fi
  # Split the shlex-quoted line back into words (zsh (z) then (Q)): no eval.
  local -a av
  av=( "${(@Q)${(@z)out}}" )
  exec "${av[@]}"
}
