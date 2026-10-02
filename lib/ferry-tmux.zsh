
# ============================================================================
# tmux over ssh, through the relay
#
# The HOST wants a shell on a CLIENT laptop. The client never accepts inbound
# connections, so it publishes its own sshd through the reverse relay
# (`ferry expose-tmux`, tagged kind "tmux"), and the host ssh's to the published
# port and attaches a tmux session (`ferry tmux`). ssh is what authenticates and
# encrypts: the relay token only authenticates the PUBLISHER, so the client needs
# Remote Login on and the host needs a key or password the client's sshd accepts.
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

# cmd_expose_tmux — `ferry expose 22 --as 8101` with a tmux + sshd preflight and
# a kind tag, so the host can list this machine for `ferry tmux`.
cmd_expose_tmux() {
  local local_port="22" passthrough=() have_as=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --local)
        if [[ $# -lt 2 ]]; then echo "Error: --local needs a port number"; exit 1; fi
        local_port="$2"; shift 2 ;;
      --as) have_as=1; passthrough+=("$1"); shift
            if [[ $# -gt 0 ]]; then passthrough+=("$1"); shift; fi ;;
      -h|--help)
        echo "Usage: ferry expose-tmux [--local PORT] [--as PUBLIC] [--host H] [--port P] [--token T]"
        echo "  --local PORT  the sshd on THIS machine [default: 22]"
        echo "  --as PUBLIC   the port it is served from on the host [default: $TMUX_PORT]"
        echo "  (every other flag is passed to 'ferry expose'; see 'ferry expose --help')"
        echo "Needs Remote Login turned on (System Settings > General > Sharing) and tmux"
        echo "installed (brew install tmux). The host then runs 'ferry tmux'."
        echo "Runs in the foreground — Ctrl-C stops publishing."
        return 0 ;;
      *) passthrough+=("$1"); shift ;;
    esac
  done
  if [[ -n "${local_port//[0-9]/}" || -z "$local_port" ]]; then
    echo "Error: --local must be a port number (got '$local_port')"
    exit 1
  fi
  if ! command -v tmux >/dev/null 2>&1; then
    echo "Error: tmux is not installed on this machine (the host attaches a tmux session here)."
    echo "       Install it:  brew install tmux"
    exit 1
  fi
  ssh_preflight "$local_port" || exit 1
  (( have_as )) || passthrough=(--as "$TMUX_PORT" "${passthrough[@]}")
  local kind="tmux"
  cmd_expose "$local_port" "${passthrough[@]}"
}

# cmd_tmux — the host half: pick a published tmux client from the relay state and
# `ssh -t` into it, attaching (or creating) a tmux session.
cmd_tmux() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry tmux' is only available on the LLM-Ferry Host Mac."
    echo "       The client side is 'ferry expose-tmux'."
    exit 1
  fi
  local client="" session="ferry" user="" list=0 print_only=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --list)    list=1; shift ;;
      --print)   print_only=1; shift ;;
      --session) if [[ $# -lt 2 ]]; then echo "Error: --session needs a name"; exit 1; fi
                 session="$2"; shift 2 ;;
      --user)    if [[ $# -lt 2 ]]; then echo "Error: --user needs a name"; exit 1; fi
                 user="$2"; shift 2 ;;
      -h|--help)
        echo "Usage: ferry tmux [CLIENT] [--list] [--session NAME] [--user U] [--print]"
        echo "  CLIENT        a client's label or its published port [default: the only one]"
        echo "  --list        list clients that ran 'ferry expose-tmux' and exit"
        echo "  --session N   the tmux session to attach or create [default: ferry]"
        echo "  --user U      the login on the client [default: what the client reported]"
        echo "  --print       print the ssh command instead of running it"
        echo "ssh authenticates you: the client needs Remote Login on and must accept your key."
        return 0 ;;
      -*) echo "Unknown option for 'ferry tmux': $1"; exit 1 ;;
      *)  if [[ -z "$client" ]]; then client="$1"; shift
          else echo "Unexpected argument: $1"; exit 1; fi ;;
    esac
  done

  local out rc=0
  out="$(python3 - "$RELAY_STATE_FILE" "$list" "$client" "$session" "$user" <<'PYEOF'
import json, re, shlex, sys

state_file, list_mode, client, session, user = sys.argv[1:6]
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

bind = str(info.get("bind") or "")
addr = "127.0.0.1" if bind in ("", "0.0.0.0") else bind
if not re.fullmatch(r"[A-Za-z0-9.:-]+", addr) or addr.startswith("-"):
    fail(f"the relay reports an unusable bind address '{bind}'", show=False)

# HostKeyAlias keeps known_hosts entries per client: a reused port must not
# collide with the previous client's host key.
alias = "ferry-tmux-" + re.sub(r"[^A-Za-z0-9._-]", "_", str(info.get("label") or port))
# Non-interactive macOS ssh has no Homebrew on PATH, so tmux would not be found.
remote = f"PATH=/opt/homebrew/bin:/usr/local/bin:$PATH exec tmux new-session -A -s {session}"
argv = ["ssh", "-p", str(port), "-o", f"HostKeyAlias={alias}", "-t", f"{user}@{addr}", remote]
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
