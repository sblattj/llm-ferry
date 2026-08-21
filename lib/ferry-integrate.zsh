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
  # [Client] Wire this laptop's opencode config to route through the host's ferry endpoint.
  # Auto-detects the host's served models: if the host serves `orchestrator` + a worker
  # (e.g. gemini-3.7-flash), it sets up the orchestrator/worker split; otherwise it wires
  # whatever single model the host serves. Non-destructive: backs up and merges.
  local oc_host="${CLIENT_HOST:-}" oc_port="${CLIENT_PORT:-8090}"
  local oc_config="$HOME/.config/opencode/opencode.json"
  local force_model="" force_small="" set_default=1

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)         oc_host="$2"; shift 2 ;;
      --port)         oc_port="$2"; shift 2 ;;
      --config)       oc_config="$2"; shift 2 ;;
      --model)        force_model="$2"; shift 2 ;;
      --small-model)  force_small="$2"; shift 2 ;;
      --no-default)   set_default=0; shift ;;
      *) echo "Unknown option for 'ferry opencode': $1"; exit 1 ;;
    esac
  done

  if [[ -z "$oc_host" ]]; then
    echo "Error: no host known. Pass --host <mdns-or-ip> (or run this on a bootstrapped"
    echo "client where ~/.config/ferry/client.json exists)."
    exit 1
  fi

  python3 - "$oc_host" "$oc_port" "$oc_config" "$force_model" "$force_small" "$set_default" <<'PYEOF'
import json, os, re, sys, shutil, urllib.request

host, port, cfg_path, force_model, force_small = sys.argv[1:6]
set_default = sys.argv[6] == "1"
cfg_path = os.path.expanduser(cfg_path)
base = f"http://{host}:{port}/v1"

# --- Discover what the host is serving ---
served = []
try:
    with urllib.request.urlopen(f"{base}/models", timeout=4) as r:
        served = [m.get("id") for m in json.load(r).get("data", []) if m.get("id")]
except Exception as e:
    print(f"    (Could not query {base}/models: {e}; wiring from flags/defaults)")

# --- Decide main + worker models ---
main = force_model or ("orchestrator" if "orchestrator" in served else (served[0] if served else "orchestrator"))
small = force_small or ("gemini-3.7-flash" if "gemini-3.7-flash" in served else "")

model_ids = list(dict.fromkeys(served + [main] + ([small] if small else [])))
models = {mid: {} for mid in model_ids}

# --- Load existing opencode config (JSONC-tolerant; back up + reset if unparseable) ---
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
            print("    (Existing opencode config had JSONC comments; parsed tolerantly)")
        except json.JSONDecodeError as e:
            shutil.copy(cfg_path, cfg_path + ".bak")
            print(f"    WARNING: existing config unparseable ({e}); backed up to {cfg_path}.bak")
            cfg = None

if cfg is None:
    cfg = {"$schema": "https://opencode.ai/config.json"}
elif os.path.exists(cfg_path):
    shutil.copy(cfg_path, cfg_path + ".bak")   # keep a backup before we rewrite

# --- Merge the ferry provider (preserving everything else) ---
prov = cfg.setdefault("provider", {})
prov["ferry"] = {
    "npm": "@ai-sdk/openai-compatible",
    "name": f"Ferry ({host})",
    "options": {"baseURL": base, "apiKey": "local"},
    "models": models,
}

if set_default:
    cfg["model"] = f"ferry/{main}"
    if small:
        cfg["small_model"] = f"ferry/{small}"
    # Pin opencode's built-in agents so the fan-out uses the cheap worker:
    #   primary build/plan -> orchestrator (main); subagents general/explore/scout -> worker (small).
    # setdefault preserves any existing per-agent prompt/permissions; we only set model.
    agents = cfg.setdefault("agent", {})
    for a in ("build", "plan"):
        agents.setdefault(a, {})["model"] = f"ferry/{main}"
    if small:
        for a in ("general", "explore", "scout"):
            agents.setdefault(a, {})["model"] = f"ferry/{small}"

os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
json.dump(cfg, open(cfg_path, "w"), indent=2)

print(f"    Wired opencode -> {base}")
print(f"    Provider: ferry   Served models: {', '.join(served) if served else '(none detected)'}")
print(f"    Default model:       ferry/{main}")
if set_default and small:
    print(f"    Default small_model: ferry/{small}")
if set_default:
    print(f"    Pinned agents:       build/plan -> ferry/{main}" + (f"; general/explore/scout -> ferry/{small}" if small else ""))
print(f"    Config written: {cfg_path}")
PYEOF
}
