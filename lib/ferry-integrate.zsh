# _ferry_install_opencode_guardrails — put the local-lane guardrails where
# opencode will actually read them: the /fan-out command and the
# spawning-subagents skill.
#
# These shipped ONLY in client-bootstrap.sh, so every CLIENT got them and the
# HOST never did — even though `ferry opencode` deliberately wires the host to
# its own endpoint, so the host drives local lanes exactly like a client does.
# On this host that meant the documented mitigation for malformed/looping `task`
# calls had never been installed on the machine reporting the problem.
#
# opencode documents these as `~/.config/opencode/command(s)/<name>.md` and
# `~/.config/opencode/skill(s)/<name>/SKILL.md` — both spellings are accepted,
# and both are GLOBAL paths, independent of $OPENCODE_CONFIG. So this installs
# to the stock location even on a host whose config lives elsewhere.
#
# Source of truth is the repo (opencode/command, opencode/skills). On a client
# there is no checkout, so this no-ops and client-bootstrap.sh's embedded copies
# remain the client path.
_ferry_install_opencode_guardrails() {
  local src_cmd="$APP_DIR/opencode/command/fan-out.md"
  local src_skill="$APP_DIR/opencode/skills/spawning-subagents/SKILL.md"
  if [[ ! -f "$src_cmd" || ! -f "$src_skill" ]]; then
    return 0   # no checkout here (a client) — nothing to install from
  fi
  local dst_cmd="$HOME/.config/opencode/command"
  local dst_skill="$HOME/.config/opencode/skill/spawning-subagents"
  mkdir -p "$dst_cmd" "$dst_skill"
  cp "$src_cmd"   "$dst_cmd/fan-out.md"
  cp "$src_skill" "$dst_skill/SKILL.md"
  echo ">>> opencode guardrails installed:"
  echo "    $dst_cmd/fan-out.md"
  echo "    $dst_skill/SKILL.md"
  echo "    (the recipe must ride in the USER message — that is what /fan-out does;"
  echo "     putting it in system instructions measured WORSE.)"
}

# _ferry_install_host_wrappers — put the `opencode-cloud` / `opencode-local` /
# `opencode-super` shell functions in the HOST's ~/.zshrc.
#
# The same gap as the guardrails above, one layer out: the wrappers were written
# ONLY by client-bootstrap.sh, so every client got them and the host never did.
# Confirmed by absence rather than assumed — host-bootstrap.sh contains no
# occurrence of "opencode" at all, and host-reset.sh writes the profile JSONs
# but never touches ~/.zshrc. So the host ended up with the FILES the wrappers
# select between and no way to select between them.
#
# Marker discipline is the whole point. client-bootstrap.sh strips a previous
# block by EXACT string compare on "# >>> ferry opencode profiles >>>", and
# client-cleanup.sh compares the same way. Writing a host block under any other
# marker means neither tool can see it, and the next client bootstrap appends a
# SECOND block defining the same functions (the later definition wins, so the
# duplicate is invisible until the two disagree). This writes the canonical
# marker for exactly that reason, and additionally absorbs the "(host)" variant
# that hand-wiring produced before this function existed.
#
# Named wrappers only, no bare `opencode()`. This matches the client's
# --profiles-only scope: a host that exports OPENCODE_CONFIG has chosen its
# default deliberately, and wrapping bare `opencode` would fight that choice.
FERRY_OC_MARK_START="# >>> ferry opencode profiles >>>"
FERRY_OC_MARK_END="# <<< ferry opencode profiles <<<"

_ferry_install_host_wrappers() {
  local rc="$HOME/.zshrc"
  touch "$rc"

  # Strip the canonical block AND the legacy "(host)" variant, then re-add. Both
  # spellings go, or absorbing the legacy one would just leave two again.
  python3 - "$rc" "$FERRY_OC_MARK_START" "$FERRY_OC_MARK_END" <<'PYEOF'
import sys
rc, start, end = sys.argv[1], sys.argv[2], sys.argv[3]

# The hand-wired spelling this function exists to absorb. Neither
# client-bootstrap.sh nor client-cleanup.sh can match it, because both compare
# marker lines for exact equality.
legacy_start = "# >>> ferry opencode profiles (host) >>>"
legacy_end = "# <<< ferry opencode profiles (host) <<<"

with open(rc) as f:
    lines = f.readlines()

out, skip = [], False
for ln in lines:
    s = ln.rstrip("\n")
    if s in (start, legacy_start):
        skip = True
        continue
    if s in (end, legacy_end):
        skip = False
        continue
    if not skip:
        out.append(ln)

# A stray `alias opencode-cloud=` / `alias opencode-local=` / `alias
# opencode-super=` ABOVE a function of the same name makes zsh expand the alias
# inside `name() {`, which is a parse error on every subsequent
# `source ~/.zshrc`. client-bootstrap.sh strips these for the same reason.
def is_legacy_alias(l):
    t = l.lstrip()
    return (t.startswith("alias opencode-cloud=")
            or t.startswith("alias opencode-local=")
            or t.startswith("alias opencode-super="))

out = [l for l in out if not is_legacy_alias(l)]

while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
PYEOF
  if (( $? != 0 )); then
    echo "    WARNING: could not rewrite $rc; leaving the shell wrappers alone." >&2
    return 1
  fi

  # QUOTED heredoc: written verbatim, so the $HOME and $@ inside the functions
  # survive into the file instead of being expanded now.
  cat <<'EOF' >> "$rc"
# >>> ferry opencode profiles >>>
# Installed by `ferry update` / host-reset.sh on the HOST. The profile FILES
# these select between are written by `ferry opencode` in the same pass.
#
# There is deliberately no bare `opencode` function: an explicit OPENCODE_CONFIG
# is the host's own choice and ferry does not override it.
unalias opencode-cloud opencode-local opencode-super 2>/dev/null

# opencode-cloud: the CLOUD pair — heavy drives (build/plan), flash runs the
# fan-out, super-flash handles title/summary/compaction.
opencode-cloud() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-cloud.json" command opencode "$@"
}

# opencode-local: the GPU pair — local-orch drives, local-sub runs the fan-out.
# Nothing leaves this machine.
opencode-local() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-local.json" command opencode "$@"
}

# opencode-super: heavy drives; super-flash runs the fan-out AND the
# housekeeping. The cheapest cloud profile.
opencode-super() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-super.json" command opencode "$@"
}
# <<< ferry opencode profiles <<<
EOF

  echo ">>> opencode shell wrappers installed in $rc:"
  echo "    opencode-cloud   -> cloud pair: heavy drives, flash fans out"
  echo "    opencode-super   -> cloud pair: heavy drives, super-flash fans out"
  echo "    opencode-local   -> GPU pair:   local-orch drives, local-sub fans out"
  echo "    (bare 'opencode' is untouched — run: source $rc)"
}

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
  # A ferry client knows three ROLES, never a model:
  #
  #   driver       build / plan                      heavy       local-orch
  #   worker       general / explore                 flash       local-sub
  #   housekeeper  title / summary / compaction      super-flash local-sub
  #
  # The housekeeper is split out because those three agents behave nothing like a
  # fan-out: they fire on their own schedule, and a compaction call carries the
  # ENTIRE transcript. Pointing them at the worker lane makes them queue behind
  # whatever the fan-out is doing, on the lane least likely to have headroom.
  # On the GPU pair there is no third lane, so the housekeeper shares local-sub.
  #
  # A real model id must NEVER reach a client config. The host re-points a lane
  # whenever the economics change; a client that named the model would keep
  # asking for something the catalogue no longer advertises. Which model sits
  # behind a lane is the host's business and is not discoverable from here.
  #
  # TAKEOVER, not merge. Four keys are ferry's and get replaced outright:
  #   permission  -> "allow"
  #   model       -> ferry/<driver>
  #   small_model -> ferry/<worker>
  #   agent       -> all seven built-ins pinned (see the AGENTS lists below)
  # provider.ferry.options.headers is ours too, rewritten every run alongside
  # baseURL/apiKey (see the prov["ferry"] block below) - it carries this
  # machine's identity and a one-shot fleet override, never a real model id.
  # `plugin` gets the goal plugin appended only when no entry already IS that
  # plugin - which includes a LOCAL PATH to a fork of it, since opencode accepts
  # a filesystem path and a private fork can only be named that way. Every OTHER key in the
  # file is left exactly as it was, and the whole original is snapshotted to
  # <name>.<UTC>.jsonc first, so a takeover is always reversible.
  local oc_host="${CLIENT_HOST:-}" oc_port="${CLIENT_PORT:-8090}"
  # v1.22.0: the front door can run litellm behind a master_key. The bearer
  # baked into the generated configs (and sent on the catalogue check) resolves
  # --key > client.json's master_key (boot-loaded as CLIENT_MASTER_KEY) > unset
  # (the legacy 'local' token, so keyless LAN setups are unchanged). The key is
  # only written into files / request headers, never printed.
  local oc_key=""
  # opencode resolves its config from $OPENCODE_CONFIG when that is set, so
  # honour it here too. Writing the hardcoded default on a machine that sets
  # OPENCODE_CONFIG edits a file opencode never reads: the command reports
  # success, and nothing changes.
  local oc_config="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.json}"
  local force_model="" force_small="" force_house="" set_default=1 prefer_local=0 force_write=0 keep_snaps=10

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)         oc_host="$2"; shift 2 ;;
      --port)         oc_port="$2"; shift 2 ;;
      --config)       oc_config="$2"; shift 2 ;;
      --key)          oc_key="$2"; shift 2 ;;
      --model)        force_model="$2"; shift 2 ;;
      --small-model)  force_small="$2"; shift 2 ;;
      --housekeeper)  force_house="$2"; shift 2 ;;
      # --super: the cheapest cloud profile — heavy still drives, super-flash
      # takes BOTH the worker and housekeeper lanes. Set here at parse time so a
      # later --small-model / --housekeeper overrides exactly one half.
      --super)        force_small="super-flash"; force_house="super-flash"; shift ;;
      --keep)         keep_snaps="$2"; shift 2 ;;
      --no-default)   set_default=0; shift ;;
      --local)        prefer_local=1; shift ;;
      # Retained for compatibility: --force used to bypass a refusal to rewrite
      # a commented (JSONC) config. That refusal is gone — the snapshot keeps the
      # original verbatim, comments included — so the flag is now a no-op.
      --force)        force_write=1; shift ;;
      --cloud)        prefer_local=0; shift ;;
      # Install the ~/.zshrc wrappers and nothing else. host-reset.sh calls this
      # once, after writing the profile files the wrappers select between —
      # doing it inside the normal path would re-run it once per config.
      --wrappers)     _ferry_install_host_wrappers; return $? ;;
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

  # Flag wins over the boot-loaded profile key; both unset keeps the legacy token.
  [[ -z "$oc_key" ]] && oc_key="${CLIENT_MASTER_KEY:-}"

  python3 - "$oc_host" "$oc_port" "$oc_config" "$force_model" "$force_small" "$set_default" "$prefer_local" "$force_write" "$keep_snaps" "$force_house" "$oc_key" "$CLIENT_NAME" <<'PYEOF'
import datetime, json, os, re, sys, shutil, urllib.request

host, port, cfg_path, force_model, force_small = sys.argv[1:6]
set_default  = sys.argv[6] == "1"
prefer_local = sys.argv[7] == "1"
force_write  = sys.argv[8] == "1"   # no-op; see the --force note above
keep_snaps   = int(sys.argv[9])
force_house  = sys.argv[10]
oc_key       = sys.argv[11]
client_name  = sys.argv[12]
cfg_path = os.path.expanduser(cfg_path)
base = f"http://{host}:{port}/v1"

SCHEMA = "https://opencode.ai/config.json"
GOAL_PLUGIN = "@prevalentware/opencode-goal-plugin"
# The package's own directory name, used to recognise a LOCAL PATH pointing at
# the same plugin. opencode accepts a filesystem path as a plugin entry, and Bun
# cannot resolve a PRIVATE repo over `github:` - so a hard fork of this plugin
# can only be named by path. A path never equals the npm name, so a presence
# check on the name alone re-appended upstream on EVERY run, leaving opencode
# loading both the fork and the very package the fork exists to replace.
GOAL_PLUGIN_DIR = GOAL_PLUGIN.rsplit("/", 1)[-1]

# opencode 1.18.23 ships SEVEN built-in agents. Verified two ways so a future
# rename gets caught: the published schema's $defs.Config.properties.agent names
# exactly plan/build/general/explore/title/summary/compaction, and the installed
# binary contains each of those strings. `scout` is in NEITHER (0 occurrences in
# the 144MB binary) — ferry pinned it for months and the pin did nothing, because
# an unknown key just lands in `agent`'s additionalProperties and is never read.
DRIVER_AGENTS = ("build", "plan")                        # primary -> driver lane
WORKER_AGENTS = ("general", "explore")                   # fan-out -> worker lane
HOUSE_AGENTS  = ("title", "summary", "compaction")       # background -> housekeeper

# --- The three lanes. Never a real model id. ---
# The local lanes cap KV at 131072 (128k) tokens, so a 100k-token prompt plus
# opencode's 32k output reservation tips over into a clean 400 (max_tokens is
# reserved against the KV budget). 8k output keeps prompts up to ~123k
# admissible; a compaction summary never needs 32k anyway.
if prefer_local:
    # No third GPU lane exists — the housekeeper shares the worker.
    driver, worker, house = "local-orch", "local-sub", "local-sub"
    limits = {"limit": {"context": 131072, "output": 8192}}
else:
    driver, worker, house = "heavy", "flash", "super-flash"
    limits = {}
driver = force_model or driver
worker = force_small or worker
house  = force_house or house

# --- Validate the pair against the host catalogue; never populate FROM it. ---
# The catalogue does NOT advertise the fallback deployments (flash-luna,
# super-flash-luna, ...): they route by name but are not `public`, so they never
# appear in /v1/models. Those are reached by the ROUTER on overflow, not by a
# client picking one out of a menu, so they stay out of the config.
served = []
try:
    # An authed front door rejects a bare catalogue request, which would read
    # as "host down" and wire the lane pair unchecked — so carry the same
    # bearer the generated configs will use. No key => no header (unchanged).
    req = urllib.request.Request(f"{base}/models")
    if oc_key:
        req.add_header("Authorization", "Bearer %s" % oc_key)
    with urllib.request.urlopen(req, timeout=4) as r:
        served = [m.get("id") for m in json.load(r).get("data", []) if m.get("id")]
except Exception as e:
    print(f"    (Could not query {base}/models: {e}; wiring the lane pair unchecked)")
if served:
    # Only the driver and worker are checked, and any lane equal to the
    # housekeeper value is exempt. A lane hidden from /v1/models cannot be
    # catalogue-checked: the housekeeper is absent from the catalogue by design,
    # and under --super the worker IS the housekeeper lane, so the old check
    # warned on every correct --super setup. (On the GPU pair the housekeeper
    # shares the worker, so local-sub goes unchecked here too — there is no way
    # to be precise about hidden-ness from the client without a public-lane
    # registry, which ferry deliberately does not have.)
    #
    # The housekeeper used to be hidden because it was a `model_group_alias` marked
    # `hidden: true`. It is a real `model_name` as of 2026-08-29 — an alias
    # silently loses its whole fallback chain (litellm looks fallbacks up by the
    # raw client string before resolving aliases, router.py:6411), and a failed
    # compaction drops the entire transcript. It stays out of the catalogue by
    # omitting `model_info: {public: true}`, which ferry_front.py filters on.
    missing = [l for l in (driver, worker) if l not in served and l != house]
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

# --- provider.ferry: the WIRING is ours; the picker's contents are not. ---
# npm/options/limits are rewritten every run — that is the drift this command
# exists to end. Two things here are NOT ours, and survive a takeover:
#
#   1. Extra lanes. A host config typically declares local-orch/local-sub too, so
#      the GPU pair is selectable from the picker without a hand edit. Rebuilding
#      `models` wholesale deleted them, and the deletion was invisible: the
#      command still reported success, and the lanes still resolved if you typed
#      one — they were just gone from the menu.
#   2. A hand-written `name` on any lane. It is the label a human reads in the
#      status bar, and /v1/models carries only the lane id, so ferry has no
#      better one to offer: never invented, never overwritten.
#
# A label naming the MODEL behind a lane goes stale by design — the whole point
# of the lane-name contract is that the model swaps host-side without touching a
# client config — so a label should name the lane's ROLE.
prov = cfg.setdefault("provider", {})
prev_ferry = prov.get("ferry") if isinstance(prov.get("ferry"), dict) else {}
prev_models = prev_ferry.get("models") if isinstance(prev_ferry.get("models"), dict) else {}
prev_options = prev_ferry.get("options") if isinstance(prev_ferry.get("options"), dict) else {}

# dict.fromkeys: on the GPU pair the housekeeper IS the worker, and declaring
# the same lane twice would be a duplicate key.
models = {}
for lane in dict.fromkeys((driver, worker, house)):
    spec = dict(limits)
    prev_name = (prev_models.get(lane) or {}).get("name")
    if isinstance(prev_name, str) and prev_name:
        spec["name"] = prev_name
    models[lane] = spec
for lane, spec in prev_models.items():
    if lane not in models and isinstance(spec, dict):
        models[lane] = spec
extra_lanes = [l for l in models if l not in (driver, worker, house)]

# Only baseURL/apiKey/headers are ours; every other options key a user
# hand-added (or a previous run wrote) survives untouched.
options = dict(prev_options)
# The bearer the front door expects: the master key when one is configured
# (client.json / --key), else the legacy 'local' placeholder.
options["baseURL"] = base
options["apiKey"] = oc_key or "local"
# Fleet identity, rewritten fresh every run - see front/ferry_front.py's
# resolver (docs/superpowers/specs/2026-09-04-fleets-design.md §4/§6).
# "{env:FERRY_FLEET}" is opencode's OWN env-substitution syntax; ferry must
# never resolve it, so a one-shot `FERRY_FLEET=international opencode-super`
# is read at opencode's load time, not at config-write time.
options["headers"] = {
    "X-Ferry-Client": client_name,
    "X-Ferry-Fleet": "{env:FERRY_FLEET}",
}

prov["ferry"] = {
    "npm": "@ai-sdk/openai-compatible",
    # Regenerated, not preserved: this one is DERIVED from --host, and a name
    # carried over from a previous host would label the picker with a box the
    # baseURL no longer points at.
    "name": f"Ferry ({host})",
    "options": options,
    "models": models,
}

if set_default:
    # --- The takeover. Four keys replaced outright, one appended to. ---
    cfg["permission"] = "allow"          # schema: PermissionConfig accepts the
                                         # bare enum "ask" | "allow" | "deny"
    cfg["model"] = f"ferry/{driver}"
    # small_model follows the HOUSEKEEPER, not the worker. opencode's own schema
    # describes it as "small model to use for tasks like title generation", which
    # is the housekeeping role exactly; leaving it on the worker would send every
    # small task opencode has not got a named agent for to the fan-out lane.
    cfg["small_model"] = f"ferry/{house}"

    # Replaced WHOLESALE, not merged: a stale pin left behind here (a compaction
    # agent still naming a retired model id, say) is exactly the drift this
    # command exists to end. Anything custom is recoverable from the snapshot.
    agent = {a: {"model": f"ferry/{driver}"} for a in DRIVER_AGENTS}
    agent.update({a: {"model": f"ferry/{worker}"} for a in WORKER_AGENTS})
    agent.update({a: {"model": f"ferry/{house}"} for a in HOUSE_AGENTS})
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

    def is_goal_plugin(entry):
        name = pkg_name(entry)
        if not isinstance(name, str):
            return False
        if name == GOAL_PLUGIN:
            return True
        # Match the package's directory name as a whole PATH SEGMENT, with or
        # without a file extension, so ".../opencode-goal-plugin/dist/server.js"
        # and ".../opencode-goal-plugin.js" both count as present while a
        # neighbouring ".../opencode-goal-plugin-extras/..." does not.
        segs = [s for s in name.split("/") if s]
        return GOAL_PLUGIN_DIR in segs or GOAL_PLUGIN_DIR in (
            os.path.splitext(s)[0] for s in segs)

    goal_entry = next((e for e in plugins if is_goal_plugin(e)), None)
    if goal_entry is None:
        plugins.append(GOAL_PLUGIN)
        goal_entry = GOAL_PLUGIN
    cfg["plugin"] = plugins

os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print(f"    Wired opencode -> {base}")
print(f"    Provider: ferry   Lanes: {driver} (driver), {worker} (worker), {house} (housekeeper)")
if extra_lanes:
    print(f"    Kept in picker: {', '.join(extra_lanes)} (declared in the config, not pinned by ferry)")
if set_default:
    print(f"    model={cfg['model']}  small_model={cfg['small_model']}  permission=allow")
    print(f"    Agents pinned:  {'/'.join(DRIVER_AGENTS)} -> ferry/{driver}")
    print(f"                    {'/'.join(WORKER_AGENTS)} -> ferry/{worker}")
    print(f"                    {'/'.join(HOUSE_AGENTS)} -> ferry/{house}")
    # Report the entry that actually SATISFIES the requirement, not the package
    # we would have added. Printing GOAL_PLUGIN unconditionally claimed an
    # install that never happened whenever a local fork was already present.
    label = goal_entry[0] if isinstance(goal_entry, list) and goal_entry else goal_entry
    print(f"    Plugin:         {label}")
    if label != GOAL_PLUGIN:
        print(f"                    (counts as {GOAL_PLUGIN}; upstream not added)")
else:
    print("    --no-default: provider wired; permission/model/agent left alone.")
if snap:
    print(f"    Snapshot:       {snap}")
print(f"    Config written: {cfg_path}")
PYEOF
}
