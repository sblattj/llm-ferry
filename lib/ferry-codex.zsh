# ferry codex — point the OpenAI Codex CLI at the ferry endpoint by lane name.
#
# Codex speaks the Responses API (POST <base_url>/responses) and nothing else —
# `wire_api = "chat"` is removed in 0.158 and config load fails on it — and the
# ferry front door serves /v1/responses. Everything Codex needs rides on `-c`
# overrides at the command line:
#
#   * NOT CODEX_HOME: a separate CODEX_HOME loses the user's ChatGPT login,
#     history and sessions.
#   * NOT legacy [profiles]: 0.158 rejects them; the new-style profile is a
#     file inside ~/.codex, which this command must never touch.
#   * The key travels in the FERRY_CODEX_KEY env var (config's `env_key`),
#     never argv, so it stays out of `ps`.
#
# The user's ~/.codex/config.toml and CODEX_HOME are NEVER read or written
# here. An unknown lane name makes Codex print a non-fatal "Model metadata for
# `X` not found. Defaulting to fallback metadata" warning; the request is
# still sent, so it is acceptable for lane names.
#
# Host and port are BAKED into the wrappers at install time on purpose: the
# function must work with ferry down, so there is no runtime lookup to fail.
# Re-running `ferry codex --host ...` rewrites them; that is the whole point of
# the marker strip below.

FERRY_CX_MARK_START="# >>> ferry codex profiles >>>"
FERRY_CX_MARK_END="# <<< ferry codex profiles <<<"

_ferry_install_codex_wrappers() {
  local cx_host="$1" cx_port="$2" cx_key="${3:-}"
  # Empty / absent key keeps the legacy 'local' bearer, so a front door without
  # a litellm master_key is unchanged. Anything else is baked in verbatim.
  [[ -z "$cx_key" ]] && cx_key="local"
  local rc="$HOME/.zshrc"
  touch "$rc"

  # Strip the canonical block, then re-add (same strip as the claude block).
  python3 - "$rc" "$FERRY_CX_MARK_START" "$FERRY_CX_MARK_END" <<'PYEOF'
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
  if (( $? != 0 )); then
    echo "    WARNING: could not rewrite $rc; leaving the codex wrappers alone." >&2
    return 1
  fi

  # QUOTED heredoc so $HOME / $@ / $FERRY_FLEET survive verbatim; host, port,
  # key and client name ride as placeholder tokens baked in right after.
  local block
  block=$(cat <<'EOF'
# >>> ferry codex profiles >>>
# Installed by `ferry codex` / host-reset.sh. Codex is pointed at ferry with
# `-c` overrides, never CODEX_HOME or [profiles]: the user's ~/.codex
# (config.toml, ChatGPT login, history) is left completely alone. The base URL
# carries /v1 because Codex appends /responses itself. The key travels in the
# FERRY_CODEX_KEY env var, never argv. User args come LAST, so `-m other-lane`
# or their own `-c` overrides the lane picked here.
#
# Host, port and key are baked at install time: the wrapper must work with
# ferry down. There is deliberately no bare `codex` function. `codex` is
# resolved by name at run time so a user's own function/alias still applies.
unalias codex-ferry codex-ferry-flash codex-ferry-local 2>/dev/null

_codex_ferry_run() {
  local model="$1"; shift
  local hdrs='{"X-Ferry-Client"="__FERRY_CX_NAME__"'
  [[ -n "${FERRY_FLEET:-}" ]] && hdrs+=",\"X-Ferry-Fleet\"=\"${FERRY_FLEET}\""
  hdrs+='}'
  FERRY_CODEX_KEY=__FERRY_CX_KEY__ codex \
      -c model_provider=ferry \
      -c 'model_providers.ferry.name="Ferry"' \
      -c 'model_providers.ferry.base_url="http://__FERRY_CX_HOST__:__FERRY_CX_PORT__/v1"' \
      -c 'model_providers.ferry.env_key="FERRY_CODEX_KEY"' \
      -c 'model_providers.ferry.wire_api="responses"' \
      -c "model_providers.ferry.http_headers=${hdrs}" \
      -c "model=\"${model}\"" \
      "$@"
}

# codex-ferry: the cloud driver lane.
codex-ferry() { _codex_ferry_run heavy "$@"; }
# codex-ferry-flash: the cloud worker lane.
codex-ferry-flash() { _codex_ferry_run flash "$@"; }
# codex-ferry-local: the GPU lane — nothing leaves this machine.
codex-ferry-local() { _codex_ferry_run local-orch "$@"; }
# <<< ferry codex profiles <<<
EOF
)
  block="${block//__FERRY_CX_HOST__/$cx_host}"
  block="${block//__FERRY_CX_PORT__/$cx_port}"
  block="${block//__FERRY_CX_KEY__/${(qq)cx_key}}"
  block="${block//__FERRY_CX_NAME__/$CLIENT_NAME}"
  print -r -- "$block" >> "$rc"

  echo ">>> codex shell wrappers installed in $rc:"
  echo "    codex-ferry        -> cloud driver lane: heavy"
  echo "    codex-ferry-flash  -> cloud worker lane: flash"
  echo "    codex-ferry-local  -> GPU lane:          local-orch"
  echo "    (bare 'codex' and ~/.codex are untouched — run: source $rc)"
}

_ferry_codex_usage() {
  cat <<'EOF'
ferry codex — point the OpenAI Codex CLI at the ferry endpoint by lane name.

Usage:
  ferry codex [--host H] [--port P] [--key K] [--wrappers]

  (no flags)   Install the ~/.zshrc wrappers (codex-ferry / codex-ferry-flash /
               codex-ferry-local) and write ~/.config/ferry/codex.json
               recording the lane map. Host resolves from --host, else
               ~/.config/ferry/client.json; on a host machine (no client.json)
               it defaults to 127.0.0.1:8090.
  --wrappers   Install ONLY the zshrc wrappers (used by host-reset.sh).
  --host H     Endpoint host to bake into the wrappers.
  --port P     Endpoint port (default 8090).
  --key K      Bearer token baked into the wrappers (sent via the
               FERRY_CODEX_KEY env var, never argv). Default: client.json's
               api_key, falling back to master_key; else 'local'.

Codex is wired with `-c` overrides at launch (Responses API, /v1/responses);
~/.codex/config.toml and CODEX_HOME are never touched.

Lane map:  codex-ferry=heavy  codex-ferry-flash=flash  codex-ferry-local=local-orch
EOF
}

cmd_codex() {
  local cx_host="" cx_port="" cx_key="" _wrappers_only=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)     cx_host="$2"; shift 2 ;;
      --port)     cx_port="$2"; shift 2 ;;
      --key)      cx_key="$2"; shift 2 ;;
      --wrappers) _wrappers_only=1; shift ;;
      --help|-h)  _ferry_codex_usage; return 0 ;;
      *) echo "Unknown option for 'ferry codex': $1"; exit 1 ;;
    esac
  done

  # Host/port: --host wins, else the client profile, read fresh (host-reset.sh
  # may run `ferry codex --wrappers` before/outside a normal CLI boot).
  if [[ -z "$cx_host" || -z "$cx_port" ]]; then
    local prof ph pp
    prof=$(python3 -c "import json, os, sys
try:
    d = json.load(open(os.path.expanduser(sys.argv[1])))
    print(d.get('host') or '', d.get('port') or '')
except Exception:
    pass" "$HOME/.config/ferry/client.json" 2>/dev/null)
    read -r ph pp <<< "$prof" 2>/dev/null
    if [[ -z "$cx_host" ]]; then
      cx_host="${ph:-}"
      if [[ -z "$cx_host" ]]; then
        if (( CLIENT_MODE )); then
          echo "Error: ~/.config/ferry/client.json has no 'host'. Re-run the client"
          echo "bootstrap, or pass --host <mdns-or-ip> explicitly."
          exit 1
        fi
        cx_host="127.0.0.1"
        echo ">>> No --host and no client profile: this is the HOST, so wiring"
        echo "    Codex to its own proxy at http://127.0.0.1:8090."
      fi
    fi
    if [[ -z "$cx_port" ]]; then
      cx_port="$pp"
    fi
  fi
  cx_port="${cx_port:-8090}"

  # Key precedence: --key, else client.json api_key (v1.39.0 device key), else
  # master_key, else CLIENT_MASTER_KEY, else empty (installer bakes 'local').
  # The label records WHICH source won, for codex.json.
  local cx_key_src=""
  if [[ -n "$cx_key" ]]; then
    cx_key_src="flag"
  else
    local kv
    kv=$(python3 -c "import json, os, sys
try:
    c = json.load(open(os.path.expanduser(sys.argv[1])))
    if c.get('api_key'):
        print('api_key', c['api_key'])
    elif c.get('master_key'):
        print('master_key', c['master_key'])
except Exception:
    pass" "$HOME/.config/ferry/client.json" 2>/dev/null)
    if [[ -n "$kv" ]]; then
      cx_key_src="${kv%% *}"
      cx_key="${kv#* }"
    fi
  fi
  if [[ -z "$cx_key" && -n "${CLIENT_MASTER_KEY:-}" ]]; then
    cx_key="$CLIENT_MASTER_KEY"
    cx_key_src="master_key"
  fi

  _ferry_install_codex_wrappers "$cx_host" "$cx_port" "$cx_key"
  local _rc=$?
  if (( _wrappers_only )); then
    return $_rc
  fi

  python3 - "$cx_host" "$cx_port" "$cx_key" "$cx_key_src" "$HOME/.config/ferry/codex.json" <<'PYEOF'
import json, os, sys

host, port, key, src, path = sys.argv[1:6]
path = os.path.expanduser(path)
cfg = {
    "host": host,
    "port": port,
    "base_url": f"http://{host}:{port}/v1",
    "wire_api": "responses",
    "provider": "ferry",
    "env_key": "FERRY_CODEX_KEY",
    "lanes": {
        "codex-ferry": "heavy",
        "codex-ferry-flash": "flash",
        "codex-ferry-local": "local-orch",
    },
}
# Mirrored only when it is a real key ('local'/empty is the keyless default).
# The key's own source label names the field, so a device key is never
# mislabeled as the master.
if key and key != "local":
    label = src if src in ("api_key", "master_key") else (
        "api_key" if key.startswith("fk-") else "master_key")
    cfg["key_source"] = label
    cfg[label] = key
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.chmod(path, 0o600)
print(f"    Wired codex     -> http://{host}:{port}/v1 (Responses API)")
print("    Lanes: codex-ferry=heavy codex-ferry-flash=flash codex-ferry-local=local-orch")
print(f"    Config written: {path}")
PYEOF

  if ! command -v codex >/dev/null 2>&1; then
    echo "    NOTE: Codex isn't installed on this machine yet — wrappers are"
    echo "    in place regardless, so nothing more is needed once it is."
  fi
}
