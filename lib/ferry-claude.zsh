# ferry claude — point Claude Code at the ferry endpoint by lane name.
#
# LiteLLM already serves Anthropic /v1/messages for every lane, so Claude Code
# needs only four env vars and two lane picks — no config file, no plugin. The
# cloud pair is heavy/flash (driver/worker), the GPU pair local-orch/local-sub;
# those are the same two roles `ferry opencode` wires, under Claude's own names:
# the main model, the haiku-slot (background/housekeeping calls), and the
# subagent model.
#
# Host and port are BAKED into the wrappers at install time on purpose: the
# function must work with ferry down, so there is no runtime lookup to fail.
# Re-running `ferry claude --host ...` rewrites them; that is the whole point of
# the marker strip below.

FERRY_CL_MARK_START="# >>> ferry claude profiles >>>"
FERRY_CL_MARK_END="# <<< ferry claude profiles <<<"

_ferry_install_claude_wrappers() {
  local cl_host="$1" cl_port="$2" cl_key="${3:-}"
  # Empty / absent key keeps the legacy 'local' bearer, so a front door without
  # a litellm master_key is unchanged. Anything else is baked in verbatim.
  [[ -z "$cl_key" ]] && cl_key="local"
  local rc="$HOME/.zshrc"
  touch "$rc"

  # Strip the canonical block, then re-add. Legacy `alias claude-ferry=` /
  # `alias claude-ferry-local=` / `alias claude-ferry-super=` lines go too: an
  # alias ABOVE a function of the same name makes zsh expand the alias inside
  # `name() {`, a parse error on every subsequent `source ~/.zshrc` (same
  # footgun client-bootstrap.sh strips).
  python3 - "$rc" "$FERRY_CL_MARK_START" "$FERRY_CL_MARK_END" <<'PYEOF'
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

def is_legacy_alias(l):
    t = l.lstrip()
    return (t.startswith("alias claude-ferry=")
            or t.startswith("alias claude-ferry-local=")
            or t.startswith("alias claude-ferry-super="))

out = [l for l in out if not is_legacy_alias(l)]

while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
PYEOF
  if (( $? != 0 )); then
    echo "    WARNING: could not rewrite $rc; leaving the claude wrappers alone." >&2
    return 1
  fi

  # QUOTED heredoc, so the $HOME and $@ inside the functions survive verbatim.
  # Host/port/key ride as placeholder tokens and are baked in by the
  # substitution right after — a quoted heredoc cannot interpolate them itself.
  local block
  block=$(cat <<'EOF'
# >>> ferry claude profiles >>>
# Installed by `ferry claude` / host-reset.sh. ANTHROPIC_BASE_URL carries NO
# /v1 suffix: Claude Code appends /v1/messages itself, and LiteLLM already
# serves that path for every lane. AUTH_TOKEN is 'local' unless the front door
# runs litellm with a master_key (LITELLM_MASTER_KEY) — `ferry claude` bakes
# the real key from ~/.config/ferry/client.json's api_key, else master_key
# (or --key) when
# one is configured, else the 'local' placeholder a keyless LAN setup expects.
#
# Host and port are baked in at install time: the wrapper must work with ferry
# down, so there is no runtime resolution to fail.
#
# There is deliberately no bare `claude` function — that is the user's personal
# tool, and wrapping it would hijack sessions they never asked to route
# anywhere. Env is scoped with `env`, so it reaches the claude child process
# only and never leaks into the interactive shell.
#
# `env -u ANTHROPIC_API_KEY`: with both set, Claude Code sends the token as
# Authorization AND the shell's API key as x-api-key, and the front door
# refuses a device key beside a different credential as ambiguous (401).
unalias claude-ferry claude-ferry-local claude-ferry-super 2>/dev/null

# claude-ferry: the CLOUD pair — heavy drives, flash runs subagents and the
# haiku-slot background calls.
claude-ferry() {
  local nl=$'\n'
  env -u ANTHROPIC_API_KEY ANTHROPIC_BASE_URL="http://__FERRY_CL_HOST__:__FERRY_CL_PORT__" \
      ANTHROPIC_AUTH_TOKEN=__FERRY_CL_KEY__ \
      ANTHROPIC_CUSTOM_HEADERS="X-Ferry-Client: __FERRY_CL_NAME__${FERRY_FLEET:+${nl}X-Ferry-Fleet: $FERRY_FLEET}" \
      ANTHROPIC_MODEL=heavy \
      ANTHROPIC_DEFAULT_HAIKU_MODEL=flash \
      CLAUDE_CODE_SUBAGENT_MODEL=flash \
      CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000 \
      CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      command claude "$@"
}

# claude-ferry-local: the GPU pair — local-orch drives, local-sub fans out.
# Nothing leaves this machine. DISABLE_THINKING=1 because reasoning tokens
# count against max_tokens AND the GPU lanes' KV budget, which is the same
# reason `ferry opencode` caps local-lane output at 8k.
claude-ferry-local() {
  local nl=$'\n'
  env -u ANTHROPIC_API_KEY ANTHROPIC_BASE_URL="http://__FERRY_CL_HOST__:__FERRY_CL_PORT__" \
      ANTHROPIC_AUTH_TOKEN=__FERRY_CL_KEY__ \
      ANTHROPIC_CUSTOM_HEADERS="X-Ferry-Client: __FERRY_CL_NAME__${FERRY_FLEET:+${nl}X-Ferry-Fleet: $FERRY_FLEET}" \
      ANTHROPIC_MODEL=local-orch \
      ANTHROPIC_DEFAULT_HAIKU_MODEL=local-sub \
      CLAUDE_CODE_SUBAGENT_MODEL=local-sub \
      CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000 \
      CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      CLAUDE_CODE_DISABLE_THINKING=1 \
      command claude "$@"
}

# claude-ferry-super: heavy drives; super-flash covers background tasks AND
# subagents. The cheapest cloud profile.
claude-ferry-super() {
  local nl=$'\n'
  env -u ANTHROPIC_API_KEY ANTHROPIC_BASE_URL="http://__FERRY_CL_HOST__:__FERRY_CL_PORT__" \
      ANTHROPIC_AUTH_TOKEN=__FERRY_CL_KEY__ \
      ANTHROPIC_CUSTOM_HEADERS="X-Ferry-Client: __FERRY_CL_NAME__${FERRY_FLEET:+${nl}X-Ferry-Fleet: $FERRY_FLEET}" \
      ANTHROPIC_MODEL=heavy \
      ANTHROPIC_DEFAULT_HAIKU_MODEL=super-flash \
      CLAUDE_CODE_SUBAGENT_MODEL=super-flash \
      CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000 \
      CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      command claude "$@"
}
# <<< ferry claude profiles <<<
EOF
)
  block="${block//__FERRY_CL_HOST__/$cl_host}"
  block="${block//__FERRY_CL_PORT__/$cl_port}"
  block="${block//__FERRY_CL_KEY__/$cl_key}"
  block="${block//__FERRY_CL_NAME__/$CLIENT_NAME}"
  print -r -- "$block" >> "$rc"

  echo ">>> claude shell wrappers installed in $rc:"
  echo "    claude-ferry        -> cloud pair: heavy drives, flash fans out"
  echo "    claude-ferry-local  -> GPU pair:   local-orch drives, local-sub fans out"
  echo "    claude-ferry-super  -> super pair: heavy drives, super-flash covers the rest"
  echo "    (bare 'claude' is untouched — run: source $rc)"
}

# `ferry claude --help` text. A function of its own so the dispatcher in
# lib/ferry-main.zsh can print it WITHOUT entering cmd_claude.
_ferry_claude_usage() {
  cat <<'EOF'
ferry claude — point Claude Code at the ferry endpoint by lane name.

Usage:
  ferry claude [--host H] [--port P] [--key K] [--wrappers]

  (no flags)   Install the ~/.zshrc wrappers (claude-ferry / claude-ferry-local)
               and write ~/.config/ferry/claude.json recording the lane map.
               Host resolves from --host, else ~/.config/ferry/client.json;
               on a host machine (no client.json) it defaults to 127.0.0.1:8090.
  --wrappers   Install ONLY the zshrc wrappers (used by host-reset.sh).
  --host H     Endpoint host to bake into the wrappers.
  --port P     Endpoint port (default 8090).
  --key K      Bearer token baked into the wrappers and sent to the front door.
               Default: client.json's api_key, falling back to master_key;
               else 'local'.

Lane map:  cloud  main=heavy   background=flash
           local  main=local-orch  background=local-sub
EOF
}

cmd_claude() {
  # Wire Claude Code to the ferry endpoint: install the claude-ferry[-local]
  # shell wrappers AND write ~/.config/ferry/claude.json recording which lane
  # plays which role. The JSON is the machine-readable twin of the wrappers —
  # other tooling (host-reset.sh, tests) reads the mapping instead of parsing
  # zshrc — so both are written in one pass and always agree.
  local cl_host="" cl_port="" cl_key="" _wrappers_only=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)     cl_host="$2"; shift 2 ;;
      --port)     cl_port="$2"; shift 2 ;;
      --key)      cl_key="$2"; shift 2 ;;
      --wrappers) _wrappers_only=1; shift ;;
      --help|-h)  _ferry_claude_usage; return 0 ;;
      *) echo "Unknown option for 'ferry claude': $1"; exit 1 ;;
    esac
  done

  # Resolve host: --host wins, else the client profile. Read client.json fresh
  # rather than trusting the boot-time CLIENT_HOST: `ferry claude --wrappers`
  # runs from host-reset.sh, which may run before/outside a normal CLI boot.
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
        # default — same reasoning as `ferry opencode`.
        cl_host="127.0.0.1"
        echo ">>> No --host and no client profile: this is the HOST, so wiring"
        echo "    Claude Code to its own proxy at http://127.0.0.1:8090."
      fi
    fi
    if [[ -z "$cl_port" ]]; then
      cl_port="$pp"
    fi
  fi
  cl_port="${cl_port:-8090}"

  # Key precedence: --key, else the client profile's api_key (a v1.39.0 device
  # key), else its optional master_key, else empty (the installer then bakes the legacy 'local' token). Read fresh from
  # client.json — same reason host/port are — because `ferry claude --wrappers`
  # runs from host-reset.sh, which may run before/outside a normal CLI boot.
  # The value only lands in files, never on stdout.
  if [[ -z "$cl_key" ]]; then
    cl_key=$(python3 -c "import json, os, sys
try:
    c = json.load(open(os.path.expanduser(sys.argv[1])))
    print(c.get('api_key') or c.get('master_key') or '')
except Exception:
    pass" "$HOME/.config/ferry/client.json" 2>/dev/null)
  fi
  cl_key="${cl_key:-${CLIENT_MASTER_KEY:-}}"

  if (( _wrappers_only )); then
    _ferry_install_claude_wrappers "$cl_host" "$cl_port" "$cl_key"
    return $?
  fi

  _ferry_install_claude_wrappers "$cl_host" "$cl_port" "$cl_key"

  # Snapshot any existing claude.json before overwrite, so a lane re-mapping is
  # always reversible (same convention as `ferry opencode`'s config snapshots).
  python3 - "$cl_host" "$cl_port" "$cl_key" "$HOME/.config/ferry/claude.json" <<'PYEOF'
import datetime, json, os, re, shutil, sys

host, port, path = sys.argv[1], sys.argv[2], os.path.expanduser(sys.argv[4])
key = sys.argv[3]

# v1.39.0 migration: when a device key replaces a master key, the master must
# not survive in the backups. It is REDACTED in place — in the backup written
# now and in every earlier claude.json.<UTC>.bak — never deleted, so the
# backups stay useful for everything else.
REDACTED = "<redacted: replaced by ferry device key>"
replaced = ""
if key.lower().startswith("fk-") and os.path.exists(path):
    try:
        old = json.load(open(path)).get("master_key")
    except Exception:
        old = None
    if (isinstance(old, str) and old and old != "local"
            and not old.lower().startswith("fk-") and old != key):
        replaced = old

def redact(p):
    try:
        with open(p, "rb") as f:
            data = f.read()
        if replaced.encode() in data:
            with open(p, "wb") as f:
                f.write(data.replace(replaced.encode(), REDACTED.encode()))
    except OSError:
        pass

if os.path.exists(path):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = f"{path}.{ts}.bak"
    n = 1
    while os.path.exists(snap):     # two runs inside one second must not collide
        snap = f"{path}.{ts}-{n}.bak"
        n += 1
    shutil.copy2(path, snap)
    print(f"    Snapshot:       {snap}")
    if replaced:
        d, base = os.path.split(path)
        pat = re.compile(re.escape(base) + r"\.\d{8}T\d{6}Z(-\d+)?\.bak$")
        for f in os.listdir(d or "."):
            if pat.match(f):
                redact(os.path.join(d, f))
        print("    The replaced master key is redacted in claude.json's backups.")

cfg = {
    "host": host,
    "port": port,
    "lanes": {
        "cloud": {"main": "heavy", "background": "flash"},
        "local": {"main": "local-orch", "background": "local-sub"},
    },
}
# Mirrored only when it is a real key: 'local' (or empty) is the keyless LAN
# default, and recording it would make the mirror claim an auth setup that
# does not exist.
if key and key != "local":
    # A per-device key (v1.39.0) is not the master and must not be named as one.
    cfg["api_key" if key.startswith("fk-") else "master_key"] = key
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"    Wired claude    -> http://{host}:{port}")
print(f"    Lanes: cloud main=heavy background=flash | local main=local-orch background=local-sub")
print(f"    Config written: {path}")
PYEOF

  if ! command -v claude >/dev/null 2>&1; then
    # Informational, not fatal: the wrappers are installed so they are ready
    # the moment Claude Code is.
    echo "    NOTE: Claude Code isn't installed on this machine yet — wrappers are"
    echo "    in place regardless, so nothing more is needed once it is."
  fi
}
