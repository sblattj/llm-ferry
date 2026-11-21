# ferry agents: the shared wiring contract for the agentic coding CLIs that
# issue #3 adds (pi, gemini, aider, goose, ...).
#
# opencode / claude / cline / codex / prime each grew their own resolve, lane
# check, snapshot and cleanup code. Fourteen more clients cannot each carry a
# hand-written copy of all that through client-bootstrap.sh, client-reset.sh
# and client-cleanup.sh, so this module owns the parts that are the same for
# every client and the client scripts loop over one registry:
#
#   FERRY_AGENT_CLIENTS  name:binary pairs. `ferry agents --list` prints the
#                        names; the client scripts read that, never a hand list.
#   _agent_main          the common CLI of every `ferry <client>`: --host,
#                        --port, --key, --model, --unwire, --dry-run, --help.
#                        Resolves the endpoint exactly the way `ferry cline`
#                        does (flag, else client.json, else 127.0.0.1:8090 on
#                        the host), checks the lane against GET /v1/models and
#                        then calls the module's own _<client>_wire.
#   _agent_python        runs a module's Python with FERRY_AGENT_PY (snapshot,
#                        load/dump, owned-file writes, the record) preloaded.
#   ~/.config/ferry/<client>.json
#                        the record of what was wired: host, port, lane and the
#                        exact files ferry CREATED (with their sha256) or EDITED
#                        (with the member it owns). `ferry <client> --unwire`
#                        undoes exactly that and nothing else: a created file
#                        the user has since changed is kept, an edited file only
#                        loses ferry's own member.
#   ~/.config/ferry/agents.key
#                        the bearer, mode 0600, read at RUN time by the
#                        `<client>-ferry` wrappers, so the key never sits in
#                        argv, in a shell rc file or in a world-readable config.
#
# The key resolves like cline's: --key, else client.json's api_key (device key)
# or master_key, else the legacy 'local' placeholder (several clients refuse an
# empty key outright).

# Registry. Order is the issue's order; the binary is what "installed" means.
FERRY_AGENT_CLIENTS=(
  pi:pi
  gemini:gemini
  openhands:openhands
  interpreter:interpreter
  goose:goose
  aider:aider
  codewhale:codewhale
  reasonix:reasonix
  omp:omp
  crush:crush
  qwen:qwen
  kilo:kilo
  kimi:kimi
  copilot:copilot
)

_agents_help() {
  cat <<'EOF'
ferry agents: the agentic coding CLIs ferry can wire (issue #3).

Usage:
  ferry agents               table: client, binary, installed?, wired?
  ferry agents --list        client names, one per line
  ferry agents --installed   client names whose binary is on PATH

Wire one with `ferry <client>`, undo it with `ferry <client> --unwire`.
EOF
}

cmd_agents() {
  # `ferry agents`          a table: client, binary, installed, wired
  # `ferry agents --list`   the client names, one per line (for the scripts)
  # `ferry agents --installed`  only the clients whose binary is on PATH
  local mode="table" e n b
  case "${1:-}" in
    --list) mode="list" ;;
    --installed) mode="installed" ;;
    "") ;;
    --help|-h) _agents_help; return 0 ;;
    *) echo "Unknown option for 'ferry agents': $1"; exit 1 ;;
  esac
  for e in "${FERRY_AGENT_CLIENTS[@]}"; do
    n="${e%%:*}"; b="${e#*:}"
    case "$mode" in
      list) print -r -- "$n" ;;
      installed) command -v "$b" >/dev/null 2>&1 && print -r -- "$n" ;;
      table)
        printf "  %-12s %-12s %-10s %s\n" "$n" "$b" \
          "$(command -v "$b" >/dev/null 2>&1 && echo installed || print -r -- -)" \
          "$([[ -f $HOME/.config/ferry/$n.json ]] && echo wired || print -r -- -)" ;;
    esac
  done
  return 0
}

# Help for one client: the module's own text, then the flags every client
# shares. Also registered as _ferry_<client>_usage, because the dispatcher's
# central --help guard (lib/ferry-main.zsh _ferry_help_for) answers --help
# before cmd_<client> ever runs.
_agent_help() {
  local client="$1"
  if (( $+functions[_${client}_help] )); then
    "_${client}_help"
  else
    echo "ferry ${client} - wire ${client} to the ferry endpoint."
  fi
  cat <<EOF

Common flags (every 'ferry <client>'):
  --host H      Endpoint host. Default: ~/.config/ferry/client.json, else
                127.0.0.1 on the host machine.
  --port P      Endpoint port (default 8090).
  --key K       Bearer to send. Default: client.json api_key / master_key,
                else 'local'. Stored in ~/.config/ferry/agents.key (0600).
  --model LANE  Default lane (default heavy; falls back to a served lane).
  --keep N      Snapshots to keep per edited file (default 10).
  --unwire      Undo what 'ferry ${client}' wrote (record-driven; files you
                changed since are kept). --dry-run shows it without acting.
EOF
}

_ferry_agents_usage() { _agents_help; }
() {
  local e
  for e in "${FERRY_AGENT_CLIENTS[@]}"; do
    functions[_ferry_${e%%:*}_usage]="_agent_help ${e%%:*}"
  done
}

_agent_binary() {   # <client> -> its binary name
  local e
  for e in "${FERRY_AGENT_CLIENTS[@]}"; do
    [[ "${e%%:*}" == "$1" ]] && { print -r -- "${e#*:}"; return 0; }
  done
  return 1
}

# _agent_main <client> "$@"
#   Parses the common flags, then either unwires (generic, record-driven, plus
#   the module's optional _<client>_unwire hook for edited files) or resolves
#   the endpoint and calls _<client>_wire. The module's help text comes from
#   _<client>_help; extra module flags are collected in AG_EXTRA.
#   Globals set for the wire function:
#     AG_CLIENT AG_HOST AG_PORT AG_BASE (http://h:p/v1) AG_ROOT (http://h:p)
#     AG_KEY AG_NAME AG_FLEET AG_LANE AG_LANES (served lane ids, space-sep)
#     AG_RECORD (~/.config/ferry/<client>.json) AG_KEYFILE AG_DRY AG_KEEP
_agent_main() {
  local client="$1"; shift
  AG_CLIENT="$client"; AG_HOST=""; AG_PORT=""; AG_KEY=""; AG_LANE=""
  AG_DRY=0; AG_KEEP=10; AG_EXTRA=()
  local unwire=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)    AG_HOST="$2"; shift 2 ;;
      --port)    AG_PORT="$2"; shift 2 ;;
      --key)     AG_KEY="$2"; shift 2 ;;
      --model)   AG_LANE="$2"; shift 2 ;;
      --keep)    AG_KEEP="$2"; shift 2 ;;
      --unwire)  unwire=1; shift ;;
      --dry-run) AG_DRY=1; shift ;;
      --help|-h) _agent_help "$client"; return 0 ;;
      *) AG_EXTRA+=("$1"); shift ;;
    esac
  done

  AG_RECORD="$HOME/.config/ferry/${client}.json"
  AG_KEYFILE="$HOME/.config/ferry/agents.key"

  if (( unwire )); then
    _agent_unwire "$client"
    return $?
  fi

  _agent_resolve || return 1

  # Clients the module can only pre-seed say so; a missing binary is a note.
  local bin
  bin=$(_agent_binary "$client") || bin="$client"
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "    NOTE: '$bin' is not on PATH yet. The wiring is written anyway, so"
    echo "    installing it later is enough."
  fi

  "_${client}_wire" || return 1
  _agent_set_mode "$client" full
  return 0
}

_agent_resolve() {
  # Host / port / key from client.json, read fresh (the scripts call this
  # outside a normal CLI boot), same precedence as cmd_cline.
  local prof ph pp pk
  prof=$(python3 -c "import json, os, sys
try:
    d = json.load(open(os.path.expanduser(sys.argv[1])))
    print(d.get('host') or '-', d.get('port') or '-', d.get('api_key') or d.get('master_key') or '-')
except Exception:
    print('- - -')" "$HOME/.config/ferry/client.json" 2>/dev/null) || prof="- - -"
  read -r ph pp pk <<< "$prof"
  [[ "$ph" == "-" ]] && ph=""; [[ "$pp" == "-" ]] && pp=""; [[ "$pk" == "-" ]] && pk=""
  if [[ -z "$AG_HOST" ]]; then
    AG_HOST="$ph"
    if [[ -z "$AG_HOST" ]]; then
      if (( ${CLIENT_MODE:-0} )) && [[ -f "$HOME/.config/ferry/client.json" ]]; then
        echo "Error: ~/.config/ferry/client.json has no 'host'. Re-run the client"
        echo "bootstrap, or pass --host <mdns-or-ip> explicitly."
        return 1
      fi
      AG_HOST="127.0.0.1"
    fi
  fi
  AG_PORT="${AG_PORT:-${pp:-8090}}"
  AG_KEY="${AG_KEY:-${pk:-${CLIENT_MASTER_KEY:-}}}"
  [[ -z "$AG_KEY" ]] && AG_KEY="local"
  AG_NAME="${CLIENT_NAME:-host}"
  AG_FLEET="${FERRY_FLEET:-}"
  AG_ROOT="http://${AG_HOST}:${AG_PORT}"
  AG_BASE="${AG_ROOT}/v1"
  AG_LANE="${AG_LANE:-heavy}"

  # Lane check, never fatal (a ferry-down host still gets wired). Line 1 of
  # the output is the lane to use, line 2 the served catalogue.
  local out
  out=$(python3 - "$AG_BASE" "$AG_KEY" "$AG_LANE" <<'PYEOF'
import json, sys, urllib.request
base, key, model = sys.argv[1:4]
served = []
try:
    req = urllib.request.Request(f"{base}/models")
    req.add_header("Authorization", "Bearer %s" % key)
    with urllib.request.urlopen(req, timeout=4) as r:
        served = [m.get("id") for m in json.load(r).get("data", []) if m.get("id")]
except Exception as e:
    print(f"    WARNING: could not verify the lane catalogue at {base}/models",
          file=sys.stderr)
    print(f"    ({e}); wiring lane '{model}' unchecked.", file=sys.stderr)
if served and model not in served:
    new = "heavy" if "heavy" in served else served[0]
    print(f"    WARNING: the host does not serve lane '{model}'; falling back",
          file=sys.stderr)
    print(f"    to lane '{new}'.", file=sys.stderr)
    model = new
print(model)
print(" ".join(served))
PYEOF
) || out=""
  # (@f) keeps empty lines, so a one-element result stays an array (a bare
  # (f) on "heavy\n" collapses to a scalar and [1] would take its first CHAR).
  local -a ag_out; ag_out=("${(@f)out}")
  AG_LANE="${ag_out[1]:-$AG_LANE}"
  AG_LANES="${ag_out[2]:-}"
  [[ -z "$AG_LANES" ]] && AG_LANES="$AG_LANE"

  # The shared bearer file. 'local' is still written: wrappers always find a
  # value, and 'local' is no secret.
  mkdir -p "$HOME/.config/ferry"
  ( umask 077; print -rn -- "$AG_KEY" > "$AG_KEYFILE" )
  chmod 600 "$AG_KEYFILE"
  return 0
}

_agent_set_mode() {   # <client> <full|none>: keep client.json's <client>_mode
  # true to what is wired, so client-reset.sh re-applies a client the user
  # wired by hand and stops re-applying one they unwired. Host machines have
  # no client.json and nothing to record.
  local cj="$HOME/.config/ferry/client.json"
  [[ -f "$cj" ]] || return 0
  python3 - "$cj" "$1" "$2" <<'PYEOF' || true
import json, os, sys
path, client, mode = sys.argv[1:4]
try:
    with open(path) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
if d.get(f"{client}_mode") == mode:
    sys.exit(0)
d[f"{client}_mode"] = mode
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
os.chmod(tmp, os.stat(path).st_mode & 0o777)
os.replace(tmp, path)
PYEOF
}

# Python preloaded into every module's write step. Modules call:
#   snapshot(path)                 -> copy to <path>.<UTC>.ferry.bak, prune
#   load_json(path) / dump_json(path, obj, mode=None)
#   write_owned(path, text, mode)  -> create a ferry-owned file; refuses to
#                                     replace a file ferry did not create
#   wrapper(path, exports, argv0, argv_prefix=())
#                                  -> a ~/.local/bin/<client>-ferry script
#   note_created(path) / note_edited(path, kind, **owner)
#   save_record(extra={})          -> ~/.config/ferry/<client>.json
# with CLIENT, HOST, PORT, BASE, ROOT, KEYFILE, NAME, FLEET, LANE, LANES,
# HEADERS ({"X-Ferry-Client": ..., "X-Ferry-Fleet": ...}), HOME, DRY.
FERRY_AGENT_PY='
import datetime, hashlib, json, os, re, shutil, stat, sys
_E = os.environ
CLIENT = _E["AG_CLIENT"]; HOST = _E["AG_HOST"]; PORT = _E["AG_PORT"]
BASE = _E["AG_BASE"]; ROOT = _E["AG_ROOT"]; KEYFILE = _E["AG_KEYFILE"]
NAME = _E.get("AG_NAME", "host"); FLEET = _E.get("AG_FLEET", "")
LANE = _E["AG_LANE"]; LANES = [l for l in _E.get("AG_LANES", "").split() if l] or [LANE]
RECORD = _E["AG_RECORD"]; KEEP = int(_E.get("AG_KEEP", "10") or 10)
HOME = os.path.expanduser("~")
HEADERS = {"X-Ferry-Client": NAME}
if FLEET:
    HEADERS["X-Ferry-Fleet"] = FLEET
MARKER = f"ferry-managed: {CLIENT}"
_TS = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_SNAP_RE = r"\.\d{8}T\d{6}Z(-\d+)?\.ferry\.bak$"
_created, _edited = [], []

def snapshot(path):
    if not os.path.exists(path):
        return None
    d = os.path.dirname(path) or "."
    stem = os.path.basename(path)
    snap = os.path.join(d, f"{stem}.{_TS}.ferry.bak")
    n = 1
    while os.path.exists(snap):
        snap = os.path.join(d, f"{stem}.{_TS}-{n}.ferry.bak"); n += 1
    shutil.copy2(path, snap)
    if KEEP > 0:
        pat = re.compile("^" + re.escape(stem) + _SNAP_RE)
        for old in sorted(f for f in os.listdir(d) if pat.match(f))[:-KEEP]:
            os.remove(os.path.join(d, old))
    print(f"    Snapshot:       {snap}")
    return snap

def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return None          # unparseable: the caller must not overwrite it

def dump_json(path, obj, mode=None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".ferry-tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    elif os.path.exists(path):
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
    os.replace(tmp, path)

def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def _prior_created():
    try:
        with open(RECORD) as f:
            return {c["path"] for c in json.load(f).get("created", [])}
    except Exception:
        return set()

def write_owned(path, text, mode=0o644):
    """Create (or refresh) a file ferry owns outright. A file at that path
    that ferry did not create (no marker, not in the last record) is never
    replaced: that is the user config this whole design leaves alone."""
    path = os.path.abspath(path)
    if os.path.exists(path):
        try:
            with open(path) as f:
                cur = f.read()
        except Exception:
            cur = ""
        if MARKER not in cur and path not in _prior_created():
            raise SystemExit(f"    ERROR: {path} exists and is not ferry-managed; "
                             "leaving it alone. Move it aside and re-run.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".ferry-tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    note_created(path)
    return path

def wrapper(path, exports, argv0, argv_prefix=(), pre=""):
    """A zsh wrapper that exports the client env (values may reference
    $FERRY_KEY, read from the 0600 key file at run time) and execs argv0."""
    import shlex
    lines = ["#!/usr/bin/env zsh", f"# {MARKER}",
             f"# Generated by `ferry {CLIENT}`; `ferry {CLIENT} --unwire` removes it.",
             f"FERRY_KEY=\"$(cat {shlex.quote(KEYFILE)} 2>/dev/null)\"",
             ": \"${FERRY_KEY:=local}\""]
    if pre:
        lines.append(pre.rstrip("\n"))
    for k, v in exports.items():
        lines.append(f"export {k}={v}")
    pre_args = " ".join(shlex.quote(a) for a in argv_prefix)
    lines.append(f"exec {shlex.quote(argv0)} {pre_args} \"$@\"".replace("  ", " "))
    return write_owned(path, "\n".join(lines) + "\n", 0o755)

def note_created(path):
    path = os.path.abspath(path)
    _created[:] = [c for c in _created if c["path"] != path]
    _created.append({"path": path, "sha256": _sha(path)})

def note_edited(path, kind, **owner):
    path = os.path.abspath(path)
    _edited[:] = [e for e in _edited if e["path"] != path]
    _edited.append(dict(path=path, kind=kind, **owner))

def save_record(extra=None):
    rec = {"client": CLIENT, "host": HOST, "port": PORT, "base": BASE,
           "model": LANE, "created": _created, "edited": _edited}
    rec.update(extra or {})
    os.makedirs(os.path.dirname(RECORD), exist_ok=True)
    if os.path.exists(RECORD):
        shutil.copy2(RECORD, f"{RECORD}.{_TS}.bak")
    dump_json(RECORD, rec, 0o600)
    print(f"    Config written: {RECORD}")

def json_member_set(path, keys, value):
    """Set obj[k1][k2]...=value in a plain-JSON file, keeping every other key.
    Records the member as ferry-owned so --unwire removes only it."""
    d = load_json(path)
    if d is None:
        raise SystemExit(f"    ERROR: {path} is not valid JSON; leaving it alone.")
    snapshot(path)
    cur = d
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value
    dump_json(path, d)
    note_edited(path, "json-member", keys=list(keys), base=BASE)
'

# _agent_python [args...] <<'PYEOF' ... PYEOF
#   Runs the heredoc with FERRY_AGENT_PY preloaded; args land in sys.argv[1:].
_agent_python() {
  local body
  body="$(cat)"
  AG_CLIENT="$AG_CLIENT" AG_HOST="$AG_HOST" AG_PORT="$AG_PORT" \
  AG_BASE="$AG_BASE" AG_ROOT="$AG_ROOT" AG_KEYFILE="$AG_KEYFILE" \
  AG_NAME="$AG_NAME" AG_FLEET="$AG_FLEET" AG_LANE="$AG_LANE" \
  AG_LANES="$AG_LANES" AG_RECORD="$AG_RECORD" AG_KEEP="$AG_KEEP" \
    python3 -c "${FERRY_AGENT_PY}"$'\n'"${body}" "$@"
}

# Generic, record-driven unwire. Created files go only if their sha256 still
# matches the record (else they are the user's now and stay, with a note).
# Edited files: kind "json-member" is stripped here; any other kind is handed
# to the module's _<client>_unwire <path> <edit-json> hook when it defines one.
# The record goes last, and client.json's <client>_mode flips to none.
_agent_unwire() {
  local client="$1"
  local rec="$HOME/.config/ferry/${client}.json"
  if [[ ! -f "$rec" ]]; then
    echo ">>> ${client}: nothing wired by ferry (no $rec), skipping."
    return 0
  fi
  echo ">>> Unwiring ${client} (from $rec)..."
  local plan rc=0
  plan=$(python3 - "$rec" "$AG_DRY" <<'PYEOF'
import hashlib, json, os, sys
rec_path, dry = sys.argv[1], sys.argv[2] == "1"
rec = json.load(open(rec_path))
for c in rec.get("created", []):
    p = c["path"]
    if not os.path.exists(p):
        continue
    with open(p, "rb") as f:
        same = hashlib.sha256(f.read()).hexdigest() == c.get("sha256")
    if not same:
        print(f"    Kept {p}: changed since ferry wrote it.", file=sys.stderr)
    elif dry:
        print(f"    [dry-run] would remove {p}", file=sys.stderr)
    else:
        os.remove(p)
        print(f"    Removed {p}", file=sys.stderr)
for e in rec.get("edited", []):
    p = e["path"]
    if not os.path.exists(p):
        continue
    if e.get("kind") != "json-member":
        print("HOOK\t" + json.dumps(e))
        continue
    try:
        d = json.load(open(p))
    except Exception:
        print(f"    Kept {p}: no longer valid JSON; remove ferry's entry by hand.",
              file=sys.stderr)
        continue
    keys, cur, chain = e["keys"], d, []
    for k in keys[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            cur = None
            break
        chain.append((cur, k)); cur = cur[k]
    if not isinstance(cur, dict) or keys[-1] not in cur:
        continue
    if dry:
        print(f"    [dry-run] would remove {'.'.join(keys)} from {p}", file=sys.stderr)
        continue
    del cur[keys[-1]]
    for parent, k in reversed(chain):      # drop containers ferry emptied
        if parent[k] == {}:
            del parent[k]
        else:
            break
    with open(p + ".ferry-tmp", "w") as f:
        json.dump(d, f, indent=2); f.write("\n")
    os.chmod(p + ".ferry-tmp", os.stat(p).st_mode & 0o777)
    os.replace(p + ".ferry-tmp", p)
    print(f"    Removed {'.'.join(keys)} from {p}", file=sys.stderr)
PYEOF
) || rc=$?
  local line
  for line in "${(@f)plan}"; do
    [[ "$line" == HOOK$'\t'* ]] || continue
    if (( $+functions[_${client}_unwire] )); then
      "_${client}_unwire" "${line#HOOK$'\t'}" || rc=1
    else
      echo "    Kept an edit ferry cannot undo generically: ${line#HOOK$'\t'}"
    fi
  done
  if (( AG_DRY )); then
    echo "    [dry-run] would remove $rec"
    return $rc
  fi
  rm -f "$rec"
  echo "    Removed $rec"
  _agent_set_mode "$client" none
  return $rc
}
