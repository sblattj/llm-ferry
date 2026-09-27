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
    echo "Usage: ferry offer [--as NAME] [--deref|--no-deref] <path> ... [--replace]"
    echo "       ferry offer --selftest"
    exit 1
  fi
  local cfg_dir="$HOME/.config/ferry"
  local offered="$cfg_dir/offered.json"
  mkdir -p "$cfg_dir"

  # Merge the given paths into offered.json via python for safe JSON.
  #
  # The manifest KEY is the name a client asks for (`GET /file/<name>`,
  # `ferry get <name>`). It used to be the path's basename, unconditionally,
  # with no way to choose it: two different paths sharing a basename silently
  # clobbered each other, and a consumer that listed a payload name separately
  # from its source path got a 404 (or someone else's file). `--as NAME` now
  # names the next path explicitly, and a key that already points at a
  # DIFFERENT path is refused unless `--replace` is given.
  #
  # A value is a plain path string (the historical shape, still written when no
  # override is given) or {"path": ..., "deref": bool} when `--deref` /
  # `--no-deref` pins how the share server tars it. Absent = auto: symlinks are
  # dereferenced (right for the HF cache) except inside a *.app bundle, whose
  # code signature seals its symlinks (see _tar_stream in lib/ferry-share.zsh).
  #
  # `--selftest` builds a tiny fixture under ~/.cache/ferry/offer-selftest (a
  # plain file, a tree with a symlink, an ad-hoc-signed .app) and offers it, so
  # client-side fetch code can be exercised against a real /file/<name>.
  python3 - "$offered" "$MDNS_NAME" "$SHARE_PORT" "$@" <<'PYEOF'
import json, os, shutil, subprocess, sys, tempfile
offered_path, mdns, share_port = sys.argv[1], sys.argv[2], sys.argv[3]
args = sys.argv[4:]

def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)

def build_selftest():
    root = os.path.expanduser("~/.cache/ferry/offer-selftest")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    with open(os.path.join(root, "hello.txt"), "w") as f:
        f.write("ferry offer selftest: plain file\n")
    tree = os.path.join(root, "tree")
    os.makedirs(tree)
    with open(os.path.join(tree, "real.txt"), "w") as f:
        f.write("ferry offer selftest: symlink target\n")
    os.symlink("real.txt", os.path.join(tree, "link.txt"))
    app = os.path.join(root, "FerrySelftest.app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    os.makedirs(os.path.join(app, "Contents", "Resources"))
    with open(os.path.join(app, "Contents", "Info.plist"), "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '<key>CFBundleExecutable</key><string>ferry-selftest</string>\n'
                '<key>CFBundleIdentifier</key><string>dev.llm-ferry.offer-selftest</string>\n'
                '<key>CFBundlePackageType</key><string>APPL</string>\n'
                '</dict></plist>\n')
    # A real Mach-O main executable: a script executable keeps its signature in
    # xattrs, which tar drops, so it would fail verification for the wrong reason.
    exe = os.path.join(app, "Contents", "MacOS", "ferry-selftest")
    shutil.copy(shutil.which("true") or "/usr/bin/true", exe)
    with open(os.path.join(app, "Contents", "Resources", "data.txt"), "w") as f:
        f.write("sealed resource\n")
    # The sealed symlink: dereferencing it on the way out voids the signature.
    os.symlink("data.txt", os.path.join(app, "Contents", "Resources", "current"))
    signed = False
    if shutil.which("codesign"):
        r = subprocess.run(["codesign", "--force", "-s", "-", app],
                           capture_output=True, text=True)
        signed = r.returncode == 0
        if not signed:
            print(f"    WARNING: codesign failed, the .app is unsigned: {r.stderr.strip()}")
    print(f">>> Selftest fixture built: {root}" + ("  (.app ad-hoc signed)" if signed else ""))
    return [("ferry-selftest-file", os.path.join(root, "hello.txt"), None),
            ("ferry-selftest-tree", tree, None),
            ("ferry-selftest-app", app, None)]

# Parse: per-path modifiers (--as, --deref/--no-deref) bind to the NEXT path.
entries, replace = [], False
pending_name, pending_deref = None, None
i = 0
while i < len(args):
    a = args[i]
    if a == "--as":
        if i + 1 >= len(args):
            die("--as needs a NAME")
        pending_name = args[i + 1]; i += 2; continue
    if a.startswith("--as="):
        pending_name = a[len("--as="):]; i += 1; continue
    if a in ("--deref", "--no-deref"):
        pending_deref = (a == "--deref"); i += 1; continue
    if a == "--replace":
        replace = True; i += 1; continue
    if a == "--selftest":
        entries.extend(build_selftest()); i += 1; continue
    if a.startswith("--"):
        die(f"unknown option: {a}")
    entries.append((pending_name, a, pending_deref))
    pending_name, pending_deref = None, None
    i += 1
if pending_name is not None or pending_deref is not None:
    die("--as / --deref / --no-deref must be followed by a path")
if not entries:
    die("no paths given")

data = {}
if os.path.exists(offered_path):
    try:
        data = json.load(open(offered_path))
    except Exception:
        data = {}

def entry_path(v):
    return v.get("path") if isinstance(v, dict) else v

# Validate everything before writing anything: a refused collision must leave
# the manifest exactly as it was.
staged, errors = [], []
for name, p, deref in entries:
    ap = os.path.abspath(os.path.expanduser(p))
    if not os.path.exists(ap):
        print(f"    WARNING: path not found, skipping: {p}")
        continue
    if name is None:
        name = os.path.basename(ap.rstrip("/"))
    if not name or "/" in name or name in (".", ".."):
        errors.append(f"invalid name {name!r} for {p} (no '/', not '.' or '..')")
        continue
    prior = data.get(name)
    if prior is not None and entry_path(prior) != ap and not replace:
        errors.append(f"'{name}' is already offered as {entry_path(prior)}; "
                      f"refusing to repoint it at {ap}. Pick another name "
                      f"(--as NAME) or pass --replace.")
        continue
    if any(n == name and s != ap for n, s, _ in staged):
        errors.append(f"'{name}' is given twice in this command for different paths")
        continue
    staged.append((name, ap, deref))
if errors:
    for e in errors:
        print(f"Error: {e}", file=sys.stderr)
    print(">>> Nothing written; the offered manifest is unchanged.", file=sys.stderr)
    sys.exit(1)

for name, ap, deref in staged:
    data[name] = ap if deref is None else {"path": ap, "deref": deref}
os.makedirs(os.path.dirname(offered_path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(offered_path), prefix=".offered.")
with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, offered_path)
for name, ap, deref in staged:
    mode = "" if deref is None else ("  [deref]" if deref else "  [no-deref]")
    print(f"    Offered: {name}  ->  {ap}{mode}")
    print(f"             GET http://{mdns}:{share_port}/file/{name}   (ferry get {name})")
print(f">>> Offered manifest saved: {offered_path}")
if staged:
    print(f"    (port {share_port} is the configured default; 'ferry share' moves up if it is busy)")
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

