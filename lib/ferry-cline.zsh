# ferry cline — point Cline (VS Code) at the ferry endpoint.
#
# Cline keeps ALL provider config in plain files under its data dir — no
# SecretStorage, no `cline.*` VS Code settings — so wiring it is three JSON
# writes plus a ~/.config/ferry/cline.json record for ferry's own tooling.
# Two facts from the Cline source (v4.1.21) shape every write below:
#   - Provider id duality: extension state (globalState.json) calls the
#     OpenAI-compatible provider "openai"; the SDK-shared mirror
#     (settings/providers.json) calls it "openai-compatible". Both files must
#     be written or half the surface ignores the new provider.
#   - `actModeApiProvider` defaults to "openrouter" when absent, so writing
#     ONLY providers.json is silently ignored: globalState.json is the file
#     that flips the actual selection.
# The key is never left empty: Cline only sends an Authorization header when
# a key exists, so a keyless LAN setup gets the legacy 'local' placeholder
# baked in rather than an auth-less client. Files should land while VS Code
# is closed (the extension caches config in memory and flushes it back over
# our write) — we warn and continue, because pre-seeding a machine before
# Cline is ever installed is a supported first-run flow. Cline's own model
# picker calls GET {baseUrl}/models, which the front door already serves as
# the lane catalogue, so the same endpoint doubles as our lane check.

cmd_cline() {
  # Wire Cline (VS Code) to the ferry endpoint: write its three provider
  # files (data/globalState.json, data/secrets.json, data/settings/
  # providers.json) under the Cline data dir and record ~/.config/ferry/
  # cline.json — the machine-readable twin other ferry tooling
  # (client-reset.sh, client-cleanup.sh) reads back instead of parsing JSON.
  local cl_host="" cl_port="" cl_key="" cl_model="" cl_data="" cl_keep=10

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)     cl_host="$2"; shift 2 ;;
      --port)     cl_port="$2"; shift 2 ;;
      --key)      cl_key="$2"; shift 2 ;;
      --model)    cl_model="$2"; shift 2 ;;
      --data-dir) cl_data="$2"; shift 2 ;;
      --keep)     cl_keep="$2"; shift 2 ;;
      --help|-h)
        cat <<'EOF'
ferry cline — point Cline (VS Code) at the ferry endpoint.

Usage:
  ferry cline [--host H] [--port P] [--key K] [--model LANE] [--data-dir DIR] [--keep N]

  (no flags)      Write Cline's provider config (an OpenAI Compatible provider
                  on a ferry lane) under the Cline data dir and record
                  ~/.config/ferry/cline.json. Host resolves from --host, else
                  ~/.config/ferry/client.json; on a host machine (no
                  client.json) it defaults to 127.0.0.1:8090.
  --host H        Endpoint host to bake into the Cline config.
  --port P        Endpoint port (default 8090).
  --key K         API key Cline sends as its bearer. Default: client.json's
                  master_key when set, else 'local' (never empty: Cline only
                  sends Authorization when a key exists).
  --model LANE    Lane id for BOTH the act and plan modes (default heavy).
  --data-dir DIR  Cline data dir (default $CLINE_DATA_DIR, else ~/.cline).
  --keep N        Snapshots to keep per config file (default 10).
EOF
        return 0 ;;
      *) echo "Unknown option for 'ferry cline': $1"; exit 1 ;;
    esac
  done

  # Resolve host: --host wins, else the client profile. Read client.json fresh
  # rather than trusting the boot-time CLIENT_HOST: `ferry cline` runs from
  # host-reset.sh / client-reset.sh, which may run outside a normal CLI boot.
  if [[ -z "$cl_host" || -z "$cl_port" ]]; then
    local prof ph pp
    prof=$(python3 -c "import json, os, sys
try:
    d = json.load(open(os.path.expanduser(sys.argv[1])))
    print(d.get('host') or '', d.get('port') or '')
except Exception:
    pass" "$HOME/.config/ferry/client.json" 2>/dev/null)
    read -r ph pp <<< "$prof" 2>/dev/null
    if [[ -z "$cl_host" ]]; then
      cl_host="${ph:-}"
      if [[ -z "$cl_host" ]]; then
        if (( CLIENT_MODE )); then
          echo "Error: ~/.config/ferry/client.json has no 'host'. Re-run the client"
          echo "bootstrap, or pass --host <mdns-or-ip> explicitly."
          exit 1
        fi
        # HOST: the proxy is on this very machine, so loopback is the right
        # default — same reasoning as `ferry opencode` / `ferry claude`.
        cl_host="127.0.0.1"
        echo ">>> No --host and no client profile: this is the HOST, so wiring"
        echo "    Cline to its own proxy at http://127.0.0.1:8090."
      fi
    fi
    if [[ -z "$cl_port" ]]; then
      cl_port="$pp"
    fi
  fi
  cl_port="${cl_port:-8090}"

  # Key precedence: --key, else the client profile's api_key (a v1.39.0 device
  # key), else its optional master_key, else the boot-loaded global. Unlike
  # claude, empty still resolves to the literal 'local' here: Cline sends NO
  # Authorization header when the key is empty, and the written config must
  # always carry one. Read fresh from client.json — same reason host/port are.
  # The value only lands in files, never stdout.
  if [[ -z "$cl_key" ]]; then
    cl_key=$(python3 -c "import json, os, sys
try:
    c = json.load(open(os.path.expanduser(sys.argv[1])))
    print(c.get('api_key') or c.get('master_key') or '')
except Exception:
    pass" "$HOME/.config/ferry/client.json" 2>/dev/null)
  fi
  cl_key="${cl_key:-${CLIENT_MASTER_KEY:-}}"
  [[ -z "$cl_key" ]] && cl_key="local"

  # X-Ferry-Client identity: same source cmd_claude bakes into the wrappers —
  # the boot-loaded CLIENT_NAME (client.json 'name' when present, else the
  # short hostname on a client, 'host' on the host).
  local cl_name="${CLIENT_NAME:-}"

  cl_model="${cl_model:-heavy}"
  # CLINE_DATA_DIR only relocates Cline's files (the one knob Cline honors),
  # so it is the default default; an explicit --data-dir always wins.
  cl_data="${cl_data:-${CLINE_DATA_DIR:-$HOME/.cline}}"

  # Lane check against the host catalogue — never fatal. A ferry-down host
  # still gets wired, just unchecked (Cline has to work with ferry down, the
  # same rule that makes the claude wrappers bake their host at install time).
  # If the catalogue IS reachable but does not serve the chosen lane, fall
  # back to heavy, else the first served lane id. Warnings go to stderr so the
  # resolved lane on stdout survives the command substitution.
  local cl_lane
  cl_lane=$(python3 - "$cl_host" "$cl_port" "$cl_key" "$cl_model" <<'PYEOF'
import json, sys, urllib.request

host, port, key, model = sys.argv[1:5]
base = f"http://{host}:{port}/v1"
served = []
try:
    # An authed front door rejects a bare catalogue request, which would read
    # as "host down" — so carry the same bearer the written config will use.
    req = urllib.request.Request(f"{base}/models")
    if key:
        req.add_header("Authorization", "Bearer %s" % key)
    with urllib.request.urlopen(req, timeout=4) as r:
        served = [m.get("id") for m in json.load(r).get("data", []) if m.get("id")]
except Exception as e:
    print(f"    WARNING: could not verify the lane catalogue at {base}/models",
          file=sys.stderr)
    print(f"    ({e}); wiring lane '{model}' unchecked.", file=sys.stderr)
    print(model)
    sys.exit(0)
if model not in served:
    new = "heavy" if "heavy" in served else (served[0] if served else model)
    if new != model:
        print(f"    WARNING: the host does not serve lane '{model}'; falling back",
              file=sys.stderr)
        print(f"    to lane '{new}'. Catalogue: {', '.join(served)}", file=sys.stderr)
        model = new
    else:
        print(f"    WARNING: the host serves no lanes at all; keeping '{model}'.",
              file=sys.stderr)
print(model)
PYEOF
)
  cl_model="${cl_lane:-$cl_model}"

  # Cline caches provider config in memory and flushes it back over changed
  # files, so writing while VS Code is open can be silently undone minutes
  # later. Warn and continue — closing the editor on a user's behalf is not
  # ours to do, and a pre-seed before VS Code even exists is a supported flow.
  if pgrep -f "Visual Studio Code" >/dev/null 2>&1; then
    echo "    WARNING: Visual Studio Code appears to be running. Cline keeps its"
    echo "    provider config in memory and may overwrite these files — quit VS"
    echo "    Code, then re-run 'ferry cline' for the change to stick."
  fi

  # The three Cline files (merge — every unknown key survives) plus ferry's
  # own record. Any failure here fails the command; the marketplace hint below
  # only prints on the success path.
  python3 - "$cl_host" "$cl_port" "$cl_key" "$cl_model" "$cl_name" "$cl_data" "$cl_keep" "${FERRY_FLEET:-}" "$HOME/.config/ferry/cline.json" <<'PYEOF'
import datetime, json, os, re, shutil, stat, sys

host, port, key, model, name, data_dir, keep = sys.argv[1:8]
fleet = sys.argv[8]
record = os.path.abspath(os.path.expanduser(sys.argv[9]))
data_dir = os.path.abspath(os.path.expanduser(data_dir))
keep = int(keep)
base = f"http://{host}:{port}/v1"
now = datetime.datetime.now(datetime.timezone.utc)
ts = now.strftime("%Y%m%dT%H%M%SZ")

# The fleet header is BAKED at run time, not resolved at Cline launch time:
# the config file is the only channel Cline has — no shell wrapper to expand
# $FERRY_FLEET the way the claude profiles do.
headers = {"X-Ferry-Client": name}
if fleet:
    headers["X-Ferry-Fleet"] = fleet

SNAP_RE = r"\.\d{8}T\d{6}Z(-\d+)?\.ferry\.bak$"

def snapshot(path):
    """Copy path to <file>.<UTC>.ferry.bak before its first modify this run,
    then prune that file's own snapshots to the newest `keep`. Matching is
    anchored to the full stem plus the timestamp shape, so a user's own
    globalState.bak sitting in the same directory is never swept up."""
    if not os.path.exists(path):
        return None
    d = os.path.dirname(path) or "."
    stem = os.path.basename(path)
    snap = os.path.join(d, f"{stem}.{ts}.ferry.bak")
    n = 1
    while os.path.exists(snap):     # two runs inside one second must not collide
        snap = os.path.join(d, f"{stem}.{ts}-{n}.ferry.bak")
        n += 1
    shutil.copy2(path, snap)
    if keep > 0:
        pat = re.compile("^" + re.escape(stem) + SNAP_RE)
        olds = sorted(f for f in os.listdir(d) if pat.match(f))
        for old in olds[:-keep]:
            os.remove(os.path.join(d, old))
    return snap

def load(path):
    """The existing JSON object, or {} — unknown keys are preserved by the
    merges below. A corrupt file starts fresh: its bytes survive in the
    snapshot, so nothing is lost either way."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def dump(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")

# v1.39.0 migration discipline: when a device key replaces a master key, the
# master must not survive in the snapshots. It is REDACTED in place — in the
# snapshots written now and in every earlier kept one — never deleted, same
# rule claude.json's backups follow. The old key is read from whichever of the
# three files still carries it, BEFORE anything is rewritten.
REDACTED = "<redacted: replaced by ferry device key>"
replaced = ""
if key.lower().startswith("fk-"):
    def _old_provider_key(d):
        provs = d.get("providers")
        if not isinstance(provs, dict):
            return None
        entry = provs.get("openai-compatible")
        if not isinstance(entry, dict):
            return None
        settings = entry.get("settings")
        return settings.get("apiKey") if isinstance(settings, dict) else None
    for p, getter in (
        (os.path.join(data_dir, "data", "secrets.json"),
         lambda d: d.get("openAiApiKey")),
        (os.path.join(data_dir, "data", "settings", "providers.json"),
         _old_provider_key),
        (record, lambda d: d.get("master_key")),
    ):
        old = getter(load(p))
        if (isinstance(old, str) and old and old != "local"
                and not old.lower().startswith("fk-") and old != key):
            replaced = old
            break

# --- globalState.json: the file that actually flips Cline's selection. ---
# "openai" here is Cline's extension-state id for "OpenAI Compatible" — NOT
# OpenAI the service, and not providers.json's "openai-compatible" either.
gs_path = os.path.join(data_dir, "data", "globalState.json")
os.makedirs(os.path.dirname(gs_path), exist_ok=True)
snap = snapshot(gs_path)
if snap:
    print(f"    Snapshot:       {snap}")
gs = load(gs_path)
gs.update({
    "actModeApiProvider": "openai",
    "planModeApiProvider": "openai",
    "openAiBaseUrl": base,
    "actModeOpenAiModelId": model,
    "planModeOpenAiModelId": model,
    "openAiHeaders": headers,
    "welcomeViewCompleted": True,
})
dump(gs_path, gs)

# --- secrets.json: plain JSON, mode 0600 (Cline's own convention). ---
sec_path = os.path.join(data_dir, "data", "secrets.json")
snap = snapshot(sec_path)
if snap:
    print(f"    Snapshot:       {snap}")
sec = load(sec_path)
sec["openAiApiKey"] = key
dump(sec_path, sec)
mode = stat.S_IMODE(os.stat(sec_path).st_mode)
if mode & 0o177:            # group/other bits set = looser than 0600
    os.chmod(sec_path, 0o600)

# --- settings/providers.json: the SDK-shared mirror. Its id for the same ---
# --- provider is "openai-compatible"; other provider entries survive.     ---
pv_path = os.path.join(data_dir, "data", "settings", "providers.json")
os.makedirs(os.path.dirname(pv_path), exist_ok=True)
snap = snapshot(pv_path)
if snap:
    print(f"    Snapshot:       {snap}")
pv = load(pv_path)
pv["version"] = 1
pv["lastUsedProvider"] = "openai-compatible"
pv["modes"] = {}
if not isinstance(pv.get("providers"), dict):
    pv["providers"] = {}
pv["providers"]["openai-compatible"] = {
    "settings": {
        "provider": "openai-compatible",
        "baseUrl": base,
        "apiKey": key,
        "model": model,
        "headers": headers,
    },
    "updatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tokenSource": "manual",
}
dump(pv_path, pv)

# --- ~/.config/ferry/cline.json: ferry's own record, snapshotted like ---
# --- claude.json (plain .bak, no retention pruning — same convention).  ---
if os.path.exists(record):
    snap = f"{record}.{ts}.bak"
    n = 1
    while os.path.exists(snap):     # two runs inside one second must not collide
        snap = f"{record}.{ts}-{n}.bak"
        n += 1
    shutil.copy2(record, snap)
    print(f"    Snapshot:       {snap}")

# Redaction runs AFTER every snapshot exists: this run's snapshots copied the
# pre-rewrite bytes, so they can still hold the replaced master.
if replaced:
    def redact(p):
        try:
            with open(p, "rb") as f:
                data = f.read()
            if replaced.encode() in data:
                with open(p, "wb") as f:
                    f.write(data.replace(replaced.encode(), REDACTED.encode()))
        except OSError:
            pass
    snaps = []
    for p in (gs_path, sec_path, pv_path):
        d = os.path.dirname(p) or "."
        stem = os.path.basename(p)
        pat = re.compile("^" + re.escape(stem) + SNAP_RE)
        snaps += [os.path.join(d, f) for f in os.listdir(d) if pat.match(f)]
    rd, rbase = os.path.split(record)
    rpat = re.compile("^" + re.escape(rbase) + r"\.\d{8}T\d{6}Z(-\d+)?\.bak$")
    snaps += [os.path.join(rd, f) for f in os.listdir(rd) if rpat.match(f)]
    for s in snaps:
        redact(s)
    print("    The replaced master key is redacted in the cline backups.")

cfg = {
    "host": host,
    "port": port,
    "model": model,
    "data_dir": data_dir,
}
# Mirrored only when it is a real key: 'local' is the keyless LAN default,
# and recording it would make the mirror claim an auth setup that does not
# exist — same rule claude.json follows.
if key and key != "local":
    # A per-device key (v1.39.0) is not the master and must not be named as one.
    cfg["api_key" if key.startswith("fk-") else "master_key"] = key
os.makedirs(os.path.dirname(record), exist_ok=True)
with open(record, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print(f"    Wired cline     -> {base} (OpenAI Compatible; lane '{model}')")
print(f"    Provider ids:   globalState 'openai' | providers.json 'openai-compatible'")
print(f"    Files written:  {gs_path}")
print(f"                     {sec_path} (mode 0600)")
print(f"                     {pv_path}")
print(f"    Config written: {record}")
PYEOF
  local cl_rc=$?

  # Missing VS Code / Cline is a NOTE, never an error: pre-seeding a machine
  # before its first launch is the norm (the config is picked up on first
  # run). Only print the one-time marketplace hint when Cline is not present.
  if (( cl_rc == 0 )); then
    local cl_ext="saoudrizwan.claude-dev"
    if command -v code >/dev/null 2>&1; then
      if ! code --list-extensions 2>/dev/null | grep -qi "^${cl_ext}"; then
        echo "    NOTE: the Cline extension isn't installed in VS Code yet — add it:"
        echo "      code --install-extension ${cl_ext}"
      fi
    else
      echo "    NOTE: VS Code isn't on this machine yet. The config is pre-seeded, so"
      echo "    installing Cline later is enough:  code --install-extension ${cl_ext}"
    fi
  fi
  return $cl_rc
}
