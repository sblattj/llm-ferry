cmd_env() {
  # Emit shell 'export' lines so a client routes its downloads through the host's
  # forward proxy (see 'ferry serve-proxy'). Designed for:  eval "$(ferry env ...)".
  # stdout stays PURELY eval-able; any human hint goes to stderr.
  local host="" proxy_port="" hf_port="" do_write=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)       host="$2"; shift 2 ;;
      --proxy-port) proxy_port="$2"; shift 2 ;;
      --hf-port)    hf_port="$2"; shift 2 ;;
      --write)      do_write=1; shift ;;
      *)            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
  done

  # Resolve the host: --host wins, else the client profile's CLIENT_HOST.
  local H="${host:-$CLIENT_HOST}"
  if [[ -z "$H" ]]; then
    echo "Error: no host given. Pass --host H, or configure ~/.config/ferry/client.json first." >&2
    exit 1
  fi
  local PP="${proxy_port:-$PROXY_PORT}"
  local HFP="${hf_port:-$HF_PORT}"

  # The export block. Built with an expanding heredoc so $H/$PP/$HFP interpolate.
  local block
  block=$(cat <<EOF
export HTTP_PROXY="http://$H:$PP"
export HTTPS_PROXY="http://$H:$PP"
export http_proxy="http://$H:$PP"
export https_proxy="http://$H:$PP"
export ALL_PROXY="http://$H:$PP"
export HF_ENDPOINT="http://$H:$HFP"
export NO_PROXY="localhost,127.0.0.1,::1,$H"
export no_proxy="localhost,127.0.0.1,::1,$H"
EOF
)

  if (( do_write )); then
    local rc="$HOME/.zshrc"
    local start="# >>> ferry env >>>"
    local end="# <<< ferry env <<<"
    touch "$rc"
    # Strip any existing ferry env block (inclusive of its markers) before re-adding.
    python3 - "$rc" "$start" "$end" <<'PYEOF'
import sys
rc, start, end = sys.argv[1], sys.argv[2], sys.argv[3]
with open(rc) as f:
    lines = f.readlines()
out, skip = [], False
for ln in lines:
    s = ln.rstrip("\n")
    if s == start:
        skip = True
        continue
    if s == end:
        skip = False
        continue
    if not skip:
        out.append(ln)
while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
PYEOF
    {
      echo "$start"
      echo "$block"
      echo "$end"
    } >> "$rc"
    echo ">>> Appended ferry env block to $rc (host $H). Run: source $rc"
  else
    print -r -- "$block"
    echo "# eval \"\$(ferry env)\" then run your uv/hf tool (downloads route via $H)" >&2
  fi
}

cmd_opencode() {
  # [Client] Take this machine's opencode config over so EVERY agent routes
  # through the host's ferry endpoint, addressed by LANE NAME only.
  #
  # A ferry client knows exactly two models: a DRIVER lane and a WORKER lane.
  # `orch` + `flash` in the cloud, `local-orch` + `local-sub` on the host GPU
  # (--local). A real model id (`gemini-3.7-flash`, `glm-5.3-flash`, ...) must
  # NEVER reach a client config: the host re-points a lane whenever the
  # economics change, and a client that named the model would silently keep
  # asking for something the catalogue no longer advertises.
  #
  # TAKEOVER, not merge. Four keys are ferry's and get replaced outright:
  #   permission  -> "allow"
  #   model       -> ferry/<driver>
  #   small_model -> ferry/<worker>
  #   agent       -> all seven built-ins pinned (see the AGENTS lists below)
  # `plugin` gets the goal plugin appended if absent. Every OTHER key in the
  # file is left exactly as it was, and the whole original is snapshotted to
  # <name>.<UTC>.jsonc first, so a takeover is always reversible.
  local oc_host="${CLIENT_HOST:-}" oc_port="${CLIENT_PORT:-8090}"
  # opencode resolves its config from $OPENCODE_CONFIG when that is set, so
  # honour it here too. Writing the hardcoded default on a machine that sets
  # OPENCODE_CONFIG edits a file opencode never reads: the command reports
  # success, and nothing changes.
  local oc_config="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.json}"
  local force_model="" force_small="" set_default=1 prefer_local=0 force_write=0 keep_snaps=10

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)         oc_host="$2"; shift 2 ;;
      --port)         oc_port="$2"; shift 2 ;;
      --config)       oc_config="$2"; shift 2 ;;
      --model)        force_model="$2"; shift 2 ;;
      --small-model)  force_small="$2"; shift 2 ;;
      --keep)         keep_snaps="$2"; shift 2 ;;
      --no-default)   set_default=0; shift ;;
      --local)        prefer_local=1; shift ;;
      # Retained for compatibility: --force used to bypass a refusal to rewrite
      # a commented (JSONC) config. That refusal is gone — the snapshot keeps the
      # original verbatim, comments included — so the flag is now a no-op.
      --force)        force_write=1; shift ;;
      --cloud)        prefer_local=0; shift ;;
      *) echo "Unknown option for 'ferry opencode': $1"; exit 1 ;;
    esac
  done

  if [[ -z "$oc_host" ]]; then
    if (( CLIENT_MODE )); then
      # A bootstrapped client whose profile exists but carries no host.
      echo "Error: ~/.config/ferry/client.json has no 'host'. Re-run the client"
      echo "bootstrap, or pass --host <mdns-or-ip> explicitly."
      exit 1
    fi
    # HOST: the proxy is on this very machine, so loopback is the right default.
    # Wiring the host to its own endpoint is the point of running one — every
    # local tool then shares the lanes, the fallback chain, and the observability,
    # and no tool on this box needs its own copy of a provider key.
    oc_host="127.0.0.1"
    echo ">>> No --host and no client profile: this is the HOST, so wiring it to its"
    echo "    own proxy at http://127.0.0.1:$oc_port/v1."
  fi

  python3 - "$oc_host" "$oc_port" "$oc_config" "$force_model" "$force_small" "$set_default" "$prefer_local" "$force_write" "$keep_snaps" <<'PYEOF'
import datetime, json, os, re, sys, shutil, urllib.request

host, port, cfg_path, force_model, force_small = sys.argv[1:6]
set_default  = sys.argv[6] == "1"
prefer_local = sys.argv[7] == "1"
force_write  = sys.argv[8] == "1"   # no-op; see the --force note above
keep_snaps   = int(sys.argv[9])
cfg_path = os.path.expanduser(cfg_path)
base = f"http://{host}:{port}/v1"

SCHEMA = "https://opencode.ai/config.json"
GOAL_PLUGIN = "@prevalentware/opencode-goal-plugin"

# opencode 1.18.23 ships SEVEN built-in agents. Verified two ways so a future
# rename gets caught: the published schema's $defs.Config.properties.agent names
# exactly plan/build/general/explore/title/summary/compaction, and the installed
# binary contains each of those strings. `scout` is in NEITHER (0 occurrences in
# the 144MB binary) — ferry pinned it for months and the pin did nothing, because
# an unknown key just lands in `agent`'s additionalProperties and is never read.
DRIVER_AGENTS = ("build", "plan")          # primary agents -> the driver lane
WORKER_AGENTS = ("general", "explore",     # subagents + the housekeeping models
                 "title", "summary", "compaction")

# --- The lane pair. Two, always, and never a real model id. ---
# The local lanes cap KV at 131072 (128k) tokens, so a 100k-token prompt plus
# opencode's 32k output reservation tips over into a clean 400 (max_tokens is
# reserved against the KV budget). 8k output keeps prompts up to ~123k
# admissible; a compaction summary never needs 32k anyway.
if prefer_local:
    driver, worker = "local-orch", "local-sub"
    limits = {"limit": {"context": 131072, "output": 8192}}
else:
    driver, worker = "orch", "flash"
    limits = {}
driver = force_model or driver
worker = force_small or worker

# --- Validate the pair against the host catalogue; never populate FROM it. ---
# The catalogue also advertises the fallback deployments (flash-gem, orch-deepseek,
# ...). Those are reached by the ROUTER on overflow, not by a client picking one
# out of a menu, so they stay out of the config.
served = []
try:
    with urllib.request.urlopen(f"{base}/models", timeout=4) as r:
        served = [m.get("id") for m in json.load(r).get("data", []) if m.get("id")]
except Exception as e:
    print(f"    (Could not query {base}/models: {e}; wiring the lane pair unchecked)")
if served:
    missing = [l for l in (driver, worker) if l not in served]
    if missing:
        print(f"    WARNING: host does not serve {', '.join(missing)}.")
        print(f"    Catalogue: {', '.join(served)}")

# --- Snapshot: the whole original, verbatim, before we touch anything. ---
# .jsonc because opencode's schema sets allowComments/allowTrailingCommas, so a
# hand-maintained config legitimately carries comments that json.dump cannot
# round-trip. The snapshot is where they survive.
SNAP_RE_TPL = r"^{stem}\.\d{{8}}T\d{{6}}Z(-\d+)?\.jsonc$"

def snapshot(path, keep):
    if not os.path.exists(path):
        return None
    d = os.path.dirname(path) or "."
    stem = os.path.splitext(os.path.basename(path))[0]
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = os.path.join(d, f"{stem}.{ts}.jsonc")
    n = 1
    while os.path.exists(snap):     # two runs inside one second must not collide
        snap = os.path.join(d, f"{stem}.{ts}-{n}.jsonc")
        n += 1
    shutil.copy2(path, snap)
    if keep > 0:
        # Match only OUR snapshots: the timestamp shape, anchored to this stem.
        # A plain "{stem}.*.jsonc" glob would happily delete a user's own
        # opencode.notes.jsonc sitting in the same directory.
        pat = re.compile(SNAP_RE_TPL.format(stem=re.escape(stem)))
        olds = sorted(f for f in os.listdir(d) if pat.match(f))
        for old in olds[:-keep]:
            os.remove(os.path.join(d, old))
    return snap

# --- Load whatever is there (JSONC-tolerant). ---
cfg = None
if os.path.exists(cfg_path):
    raw = open(cfg_path).read()
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        stripped = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                          lambda m: m.group(1) or '', raw, flags=re.S)
        stripped = re.sub(r',\s*([}\]])', r'\1', stripped)
        try:
            cfg = json.loads(stripped)
            print("    (config is JSONC; its comments survive in the snapshot, not in the rewrite)")
        except json.JSONDecodeError as e:
            print(f"    WARNING: existing config is unparseable ({e}); starting from a fresh one.")
            print("    The original is preserved verbatim in the snapshot below.")
            cfg = None

snap = snapshot(cfg_path, keep_snaps)
if cfg is None:
    cfg = {}
cfg.setdefault("$schema", SCHEMA)

# --- provider.ferry: ours to own. Exactly the two lanes. ---
prov = cfg.setdefault("provider", {})
prov["ferry"] = {
    "npm": "@ai-sdk/openai-compatible",
    "name": f"Ferry ({host})",
    "options": {"baseURL": base, "apiKey": "local"},
    "models": {driver: dict(limits), worker: dict(limits)},
}

if set_default:
    # --- The takeover. Four keys replaced outright, one appended to. ---
    cfg["permission"] = "allow"          # schema: PermissionConfig accepts the
                                         # bare enum "ask" | "allow" | "deny"
    cfg["model"] = f"ferry/{driver}"
    cfg["small_model"] = f"ferry/{worker}"

    # Replaced WHOLESALE, not merged: a stale pin left behind here (a compaction
    # agent still naming a retired model id, say) is exactly the drift this
    # command exists to end. Anything custom is recoverable from the snapshot.
    agent = {a: {"model": f"ferry/{driver}"} for a in DRIVER_AGENTS}
    agent.update({a: {"model": f"ferry/{worker}"} for a in WORKER_AGENTS})
    cfg["agent"] = agent

    # Additive — a plugin list belongs to the user; we only ensure ours is in it.
    plugins = cfg.get("plugin")
    if not isinstance(plugins, list):
        plugins = []

    def pkg_name(entry):
        if isinstance(entry, list) and entry:      # the ["pkg", {opts}] form
            entry = entry[0]
        if not isinstance(entry, str):
            return None
        # Strip a trailing @version without eating the leading scope @.
        return entry.rsplit("@", 1)[0] if entry.count("@") > 1 else entry

    if not any(pkg_name(e) == GOAL_PLUGIN for e in plugins):
        plugins.append(GOAL_PLUGIN)
    cfg["plugin"] = plugins

os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print(f"    Wired opencode -> {base}")
print(f"    Provider: ferry   Lanes: {driver} (driver), {worker} (worker)")
if set_default:
    print(f"    model={cfg['model']}  small_model={cfg['small_model']}  permission=allow")
    print(f"    Agents pinned:  {'/'.join(DRIVER_AGENTS)} -> ferry/{driver}")
    print(f"                    {'/'.join(WORKER_AGENTS)} -> ferry/{worker}")
    print(f"    Plugin:         {GOAL_PLUGIN}")
else:
    print("    --no-default: provider wired; permission/model/agent left alone.")
if snap:
    print(f"    Snapshot:       {snap}")
print(f"    Config written: {cfg_path}")
PYEOF
}
