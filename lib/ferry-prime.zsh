# ferry prime — point Prime Agent (PrimeIntellect-ai/prime-agent) at the ferry
# endpoint.
#
# Prime Agent keeps custom providers in <agent dir>/models.json — default
# ~/.prime/agent, relocated by $PRIME_AGENT_CODING_AGENT_DIR — so wiring it is
# one surgical edit of that file plus a ~/.config/ferry/prime.json record. Five
# facts from the prime-agent source (main @ 2026-09-30) shape the write:
#   - The file is {"providers": {"<id>": {...}}}, NOT a bare {"<id>": {...}}
#     (crates/pa-core/src/models/custom.rs ModelsConfig / parse_models_config).
#   - It is JSONC: // and /* */ comments and trailing commas are tolerated
#     (custom.rs strip_json_comments), so users DO keep comments in it.
#   - baseUrl is the .../v1 root: the openai-completions provider appends
#     "/chat/completions" itself (crates/pa-ai/src/providers/openai_completions/
#     stream.rs: format!("{}/chat/completions", base_url.trim_end_matches('/'))).
#   - A custom provider needs baseUrl + apiKey + api (custom.rs validate_config);
#     per-model only `id` is required (name defaults to id, contextWindow to
#     128000, maxTokens to 16384 — load_custom_models).
#   - `authHeader: true` injects "Authorization: Bearer <apiKey>"
#     (crates/pa-core/src/models/registry.rs get_api_key_and_headers).
# A custom URL gets the generic OpenAI compat defaults (store:false, developer
# role, max_completion_tokens — openai_completions/mod.rs detect_compat), which
# a LiteLLM-fronted local lane may reject, so the provider pins the plain
# dialect via `compat`. Models are selected as `--provider ferry --model <lane>`
# (command_registry.rs) / the provider/id selector `ferry/<lane>`.
#
# The edit is TEXT-surgical, not a JSON round trip: the ferry member is
# replaced in place (or appended to "providers"), so the user's comments,
# trailing commas, other providers and formatting survive byte for byte. The
# result is re-parsed and compared to the original before anything is written;
# a file that cannot be parsed or edited safely is left untouched, with the
# snippet to add by hand printed and a non-zero exit. prime-agent need not be
# installed: pre-seeding a machine before first install is the supported flow.

cmd_prime() {
  local pr_host="" pr_port="" pr_key="" pr_model="" pr_dir="" pr_keep=10

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)      pr_host="$2"; shift 2 ;;
      --port)      pr_port="$2"; shift 2 ;;
      --key)       pr_key="$2"; shift 2 ;;
      --model)     pr_model="$2"; shift 2 ;;
      --agent-dir) pr_dir="$2"; shift 2 ;;
      --keep)      pr_keep="$2"; shift 2 ;;
      --help|-h)
        cat <<'EOF'
ferry prime — point Prime Agent at the ferry endpoint.

Usage:
  ferry prime [--host H] [--port P] [--key K] [--model LANE] [--agent-dir DIR] [--keep N]

  (no flags)      Add a 'ferry' custom provider (OpenAI-compatible, every
                  served lane listed as a model) to Prime Agent's models.json
                  and record ~/.config/ferry/prime.json. Other providers and
                  comments in an existing models.json are left untouched; a
                  file that cannot be edited safely is NOT overwritten (the
                  snippet to add by hand is printed, exit 1). Host resolves from
                  --host, else ~/.config/ferry/client.json; on a host machine
                  (no client.json) it defaults to 127.0.0.1:8090.
  --host H        Endpoint host to bake into the provider.
  --port P        Endpoint port (default 8090).
  --key K         API key sent as the bearer. Default: client.json's api_key
                  (device key), else master_key, else 'local'.
  --model LANE    Lane to suggest in the run hint and list first (default heavy).
  --agent-dir DIR Prime Agent state dir (default $PRIME_AGENT_CODING_AGENT_DIR,
                  else ~/.prime/agent).
  --keep N        Snapshots to keep of models.json (default 10).

Then:  prime-agent --provider ferry --model heavy
EOF
        return 0 ;;
      *) echo "Unknown option for 'ferry prime': $1"; exit 1 ;;
    esac
  done

  # Host/port: --host wins, else the client profile (read fresh, not from the
  # boot-time CLIENT_HOST — host-reset.sh / client-reset.sh call this outside
  # a normal CLI boot). Same resolution as `ferry cline`.
  if [[ -z "$pr_host" || -z "$pr_port" ]]; then
    local prof ph pp
    prof=$(python3 -c "import json, os, sys
try:
    d = json.load(open(os.path.expanduser(sys.argv[1])))
    print(d.get('host') or '', d.get('port') or '')
except Exception:
    pass" "$HOME/.config/ferry/client.json" 2>/dev/null)
    read -r ph pp <<< "$prof" 2>/dev/null
    if [[ -z "$pr_host" ]]; then
      pr_host="${ph:-}"
      if [[ -z "$pr_host" ]]; then
        if (( CLIENT_MODE )); then
          echo "Error: ~/.config/ferry/client.json has no 'host'. Re-run the client"
          echo "bootstrap, or pass --host <mdns-or-ip> explicitly."
          exit 1
        fi
        pr_host="127.0.0.1"
        echo ">>> No --host and no client profile: this is the HOST, so wiring"
        echo "    Prime Agent to its own proxy at http://127.0.0.1:8090."
      fi
    fi
    [[ -z "$pr_port" ]] && pr_port="$pp"
  fi
  pr_port="${pr_port:-8090}"

  # Key precedence: --key, else the profile's api_key (device key), else its
  # master_key, else the boot-loaded global, else 'local' (authHeader needs a
  # non-empty key). Only ever lands in files, never stdout.
  if [[ -z "$pr_key" ]]; then
    pr_key=$(python3 -c "import json, os, sys
try:
    c = json.load(open(os.path.expanduser(sys.argv[1])))
    print(c.get('api_key') or c.get('master_key') or '')
except Exception:
    pass" "$HOME/.config/ferry/client.json" 2>/dev/null)
  fi
  pr_key="${pr_key:-${CLIENT_MASTER_KEY:-}}"
  [[ -z "$pr_key" ]] && pr_key="local"

  local pr_name="${CLIENT_NAME:-}"
  pr_model="${pr_model:-heavy}"
  pr_dir="${pr_dir:-${PRIME_AGENT_CODING_AGENT_DIR:-$HOME/.prime/agent}}"

  python3 - "$pr_host" "$pr_port" "$pr_key" "$pr_model" "$pr_name" "$pr_dir" "$pr_keep" "${FERRY_FLEET:-}" "$HOME/.config/ferry/prime.json" <<'PYEOF'
import datetime, json, os, re, shutil, stat, sys, urllib.request

host, port, key, model, name, agent_dir, keep = sys.argv[1:8]
fleet = sys.argv[8]
record = os.path.abspath(os.path.expanduser(sys.argv[9]))
agent_dir = os.path.abspath(os.path.expanduser(agent_dir))
keep = int(keep)
base = f"http://{host}:{port}/v1"
now = datetime.datetime.now(datetime.timezone.utc)
ts = now.strftime("%Y%m%dT%H%M%SZ")
models_path = os.path.join(agent_dir, "models.json")

# Lanes: the served catalogue when reachable (never fatal — ferry-down hosts
# still get wired), else the fixed lane set ferry ships.
FIXED_LANES = ["heavy", "flash", "super-flash", "local-orch", "local-sub"]
lanes = []
try:
    req = urllib.request.Request(f"{base}/models")
    if key:
        req.add_header("Authorization", "Bearer %s" % key)
    with urllib.request.urlopen(req, timeout=4) as r:
        lanes = [m.get("id") for m in json.load(r).get("data", []) if m.get("id")]
except Exception as e:
    print(f"    WARNING: could not read the lane catalogue at {base}/models",
          file=sys.stderr)
    print(f"    ({e}); listing the standard lanes unchecked.", file=sys.stderr)
if not lanes:
    lanes = list(FIXED_LANES)
elif model not in lanes:
    print(f"    WARNING: the host does not serve lane '{model}'; falling back.",
          file=sys.stderr)
    print(f"    Catalogue: {', '.join(lanes)}", file=sys.stderr)
    model = "heavy" if "heavy" in lanes else lanes[0]
if model in lanes:
    lanes.remove(model)
    lanes.insert(0, model)

headers = {"X-Ferry-Client": name}
if fleet:
    headers["X-Ferry-Fleet"] = fleet

provider = {
    "name": "ferry",
    "baseUrl": base,
    "apiKey": key,
    "api": "openai-completions",
    "authHeader": True,
    "headers": headers,
    # A custom URL otherwise gets generic OpenAI defaults (store, developer
    # role, max_completion_tokens) that a LiteLLM-fronted local lane may reject.
    "compat": {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "maxTokensField": "max_tokens",
    },
    "models": [{"id": lane, "name": f"ferry {lane}"} for lane in lanes],
}

# ---------------------------------------------------------------- JSONC ----
class Bad(Exception):
    pass

def skip_ws(t, i):
    n = len(t)
    while i < n:
        c = t[i]
        if c in " \t\r\n﻿":
            i += 1
        elif t.startswith("//", i):
            j = t.find("\n", i)
            i = n if j < 0 else j + 1
        elif t.startswith("/*", i):
            j = t.find("*/", i + 2)
            if j < 0:
                raise Bad("unterminated /* comment")
            i = j + 2
        else:
            break
    return i

def end_string(t, i):
    j = i + 1
    while j < len(t):
        if t[j] == "\\":
            j += 2
        elif t[j] == '"':
            return j + 1
        else:
            j += 1
    raise Bad("unterminated string")

SCALAR = re.compile(r"-?\d[\d.eE+\-]*|true|false|null")

def parse_value(t, i):
    """End index of the value starting at i (after whitespace)."""
    i = skip_ws(t, i)
    if i >= len(t):
        raise Bad("unexpected end of file")
    c = t[i]
    if c == "{":
        return parse_object(t, i)[0]
    if c == "[":
        i += 1
        while True:
            i = skip_ws(t, i)
            if i >= len(t):
                raise Bad("unterminated array")
            if t[i] == "]":
                return i + 1
            i = skip_ws(t, parse_value(t, i))
            if i < len(t) and t[i] == ",":
                i += 1
            elif i < len(t) and t[i] != "]":
                raise Bad("expected , or ] in array")
    if c == '"':
        return end_string(t, i)
    m = SCALAR.match(t, i)
    if not m:
        raise Bad(f"unexpected character {c!r}")
    return m.end()

def parse_object(t, i):
    """(index just past the closing brace, members) for the object at i; each
    member is (key, value_start, value_end)."""
    members = []
    i += 1
    while True:
        i = skip_ws(t, i)
        if i >= len(t):
            raise Bad("unterminated object")
        if t[i] == "}":
            return i + 1, members
        if t[i] != '"':
            raise Bad("expected a string key")
        ke = end_string(t, i)
        k = json.loads(t[i:ke])
        i = skip_ws(t, ke)
        if i >= len(t) or t[i] != ":":
            raise Bad("expected ':' after a key")
        vs = skip_ws(t, i + 1)
        ve = parse_value(t, vs)
        members.append((k, vs, ve))
        i = skip_ws(t, ve)
        if i < len(t) and t[i] == ",":
            i += 1
        elif i < len(t) and t[i] != "}":
            raise Bad("expected , or } in object")

def strip_jsonc(t):
    """Comments and trailing commas removed — a strict-JSON twin of t."""
    out, i, n = [], 0, len(t)
    while i < n:
        c = t[i]
        if c == '"':
            j = end_string(t, i)
            out.append(t[i:j])
            i = j
        elif c == "/" or c in " \t\r\n﻿":
            j = skip_ws(t, i)
            if j > i:
                out.append(" ")
                i = j
            else:                       # a lone '/' — invalid JSON, let loads() say so
                out.append(c)
                i += 1
        elif c == ",":
            j = skip_ws(t, i + 1)
            if not (j < n and t[j] in "}]"):
                out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)

def loads(t):
    try:
        return json.loads(strip_jsonc(t))
    except (ValueError, IndexError) as e:
        raise Bad(str(e))

def block(indent, key, val):
    """A '"key": value' member, multi-line, every line padded by `indent`."""
    pad = " " * indent
    body = json.dumps(val, indent=2).replace("\n", "\n" + pad)
    return f"{pad}{json.dumps(key)}: {body}"

def insert_member(t, obj_start, indent, key, val):
    """Insert a member just before the closing brace of the object at
    obj_start, keeping every existing byte; returns the new text."""
    end, members = parse_object(t, obj_start)
    close = end - 1
    member = block(indent, key, val)
    closepad = " " * (indent - 2)
    if not members:
        return t[:close] + "\n" + member + "\n" + closepad + t[close:]
    last_end = members[-1][2]
    k = skip_ws(t, last_end)
    trailing = k < len(t) and t[k] == ","
    p = close                      # back up over the closing brace's indent
    while p > 0 and t[p - 1] in " \t":
        p -= 1
    head = t[:p]
    new = (head + ("" if head.endswith("\n") else "\n")
           + member + ("," if trailing else "") + "\n" + closepad + t[close:])
    if not trailing:
        # the comma goes right after the previous last value, so a same-line
        # comment after it stays a comment
        new = new[:last_end] + "," + new[last_end:]
    return new

def merged_text(text):
    """text with providers.ferry set to `provider`; raises Bad when the file
    is not safely editable."""
    old = loads(text)
    if not isinstance(old, dict):
        raise Bad("top level is not a JSON object")
    start = skip_ws(text, 0)
    _, members = parse_object(text, start)
    prov = next((m for m in members if m[0] == "providers"), None)
    if prov is None:
        new = insert_member(text, start, 2, "providers", {"ferry": provider})
    else:
        if not isinstance(old.get("providers"), dict):
            raise Bad('"providers" is not an object')
        _, pmembers = parse_object(text, prov[1])
        fm = next((m for m in pmembers if m[0] == "ferry"), None)
        if fm is None:
            new = insert_member(text, prov[1], 4, "ferry", provider)
        else:
            body = json.dumps(provider, indent=2).replace("\n", "\n    ")
            new = text[:fm[1]] + body + text[fm[2]:]
    # Proof before write: the edit must parse, carry our provider, and leave
    # every other key and provider exactly as it was.
    want = json.loads(json.dumps(old))
    want.setdefault("providers", {})["ferry"] = provider
    if loads(new) != want:
        raise Bad("the edit did not round-trip")
    return new

# ------------------------------------------------------------- snapshots ----
SNAP_RE = r"\.\d{8}T\d{6}Z(-\d+)?\.ferry\.bak$"

def snapshot(path):
    d, stem = os.path.dirname(path), os.path.basename(path)
    snap = os.path.join(d, f"{stem}.{ts}.ferry.bak")
    n = 1
    while os.path.exists(snap):     # two runs inside one second must not collide
        snap = os.path.join(d, f"{stem}.{ts}-{n}.ferry.bak")
        n += 1
    shutil.copy2(path, snap)
    if keep > 0:
        pat = re.compile("^" + re.escape(stem) + SNAP_RE)
        for old in sorted(f for f in os.listdir(d) if pat.match(f))[:-keep]:
            os.remove(os.path.join(d, old))
    return snap

def load_record():
    try:
        with open(record) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

# ------------------------------------------------------------------ main ----
existing = None
if os.path.exists(models_path):
    with open(models_path, encoding="utf-8") as f:
        existing = f.read()

if existing is None or not existing.strip():
    new_text = json.dumps({"providers": {"ferry": provider}}, indent=2) + "\n"
else:
    try:
        new_text = merged_text(existing)
    except (Bad, IndexError) as e:
        print(f"    ERROR: {models_path} was NOT modified: {e}.", file=sys.stderr)
        print('    Add this provider under "providers" by hand:', file=sys.stderr)
        print(block(4, "ferry", provider), file=sys.stderr)
        sys.exit(1)

# v1.39.0 migration discipline: a device key replacing a master key redacts the
# master in the snapshots (never deletes them).
REDACTED = "<redacted: replaced by ferry device key>"
replaced = ""
if key.lower().startswith("fk-"):
    cands = [load_record().get("master_key")]
    if existing:
        try:
            cands.insert(0, (loads(existing).get("providers", {})
                             .get("ferry", {}) or {}).get("apiKey"))
        except Exception:
            pass
    for old in cands:
        if (isinstance(old, str) and old and old != "local"
                and not old.lower().startswith("fk-") and old != key):
            replaced = old
            break

os.makedirs(agent_dir, exist_ok=True)
changed = new_text != existing
if existing is not None and changed:
    print(f"    Snapshot:       {snapshot(models_path)}")
if changed:
    fd = os.open(models_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text)
if stat.S_IMODE(os.stat(models_path).st_mode) & 0o177:   # it holds the API key
    os.chmod(models_path, 0o600)

if os.path.exists(record):
    snap = f"{record}.{ts}.bak"
    n = 1
    while os.path.exists(snap):
        snap = f"{record}.{ts}-{n}.bak"
        n += 1
    shutil.copy2(record, snap)
    print(f"    Snapshot:       {snap}")

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
    pat = re.compile("^models\\.json" + SNAP_RE)
    snaps = [os.path.join(agent_dir, f) for f in os.listdir(agent_dir) if pat.match(f)]
    rd, rbase = os.path.split(record)
    rpat = re.compile("^" + re.escape(rbase) + r"\.\d{8}T\d{6}Z(-\d+)?\.bak$")
    snaps += [os.path.join(rd, f) for f in os.listdir(rd) if rpat.match(f)]
    for s in snaps:
        redact(s)
    print("    The replaced master key is redacted in the prime backups.")

cfg = {"host": host, "port": port, "model": model, "agent_dir": agent_dir,
       "models_path": models_path, "provider": "ferry", "lanes": lanes}
if key and key != "local":
    cfg["api_key" if key.startswith("fk-") else "master_key"] = key
os.makedirs(os.path.dirname(record), exist_ok=True)
with open(record, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print(f"    Wired prime     -> {base} (openai-completions; lanes: {', '.join(lanes)})")
print(f"    File written:   {models_path} (mode 0600)" + ("" if changed else " (unchanged)"))
print(f"    Config written: {record}")
print(f"    Run:            prime-agent --provider ferry --model {model}")
PYEOF
  local pr_rc=$?

  # prime-agent absent is a NOTE, never an error: the config is pre-seeded and
  # picked up when it is installed. The installer is printed, never run.
  if (( pr_rc == 0 )) && ! command -v prime-agent >/dev/null 2>&1; then
    echo "    NOTE: prime-agent isn't on this machine yet. The config is pre-seeded,"
    echo "    so installing it later is enough:"
    echo "      curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh"
  fi
  return $pr_rc
}
