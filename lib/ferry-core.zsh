# ferry — Unified LAN AI Relay and Proxy Manager (macOS + Linux).
# Consolidates local GPU model execution (macOS / Apple Silicon only), cloud API
# proxies, and LAN sharing. On Linux the cloud/route/client/dash/share/transfer
# features all work; local GPU serving is macOS-only (use --route/--cloud/--model).
# Supports both Host Mode and Client Mode (on connecting laptops).
#
# Usage:
#   ferry install                # Provision dependencies, models, and global link
#   ferry up [options]           # [Host] Start local GPU server or cloud proxy (Gemini)
#   ferry down                   # [Host] Stop all relay servers, proxies, and shares
#   ferry status                 # [Dual] View active status, LAN IPs, and test connections
#   ferry share                  # [Host] Expose client-bootstrap.sh over LAN
#   ferry msg <text>             # [Client] Send a direct text message to host's ~/.config/ferry/client_logs.txt
#   ferry log                    # [Client] Pipe stdin log stream directly back to host
#   ferry offer <path>...        # [Host] Offer files/dirs for clients to fetch over the LAN
#   ferry pull <model-id>        # [Client] Pull a model from the host cache (http|hf|nc transports)
#   ferry get <name>             # [Client] Fetch an offered file/dir from the host
#   ferry send <path> <client>   # [Host] Push a file/dir to a listening client via netcat
#   ferry receive                # [Client] Listen for and receive a netcat tar stream
#   ferry serve-hf               # [Host] Start an EXPERIMENTAL HuggingFace pass-through proxy
#   ferry serve-proxy            # [Host] Start a general HTTP(S) forward proxy for client downloads
#   ferry env                    # [Client] Emit shell exports so this laptop downloads via the host proxy
#   ferry opencode               # [Client] Auto-wire opencode to route through the host (detects served models)

set -eu

APP_DIR="$(dirname "${0:A}")"
# The script's own resolved path, captured HERE at load time: inside a function
# `$0` is the function's name, so a command that needs to re-invoke ferry (the
# relay backgrounding itself) cannot compute this for itself.
FERRY_BIN_PATH="${0:A}"

# ---- OS detection & portable host helpers (macOS + Linux) ----
case "$(uname -s)" in
  Darwin) IS_MAC=1 ;;
  *)      IS_MAC=0 ;;
esac

# detect_lan_ip — echo one primary, non-loopback IPv4 address for this host.
#   macOS: parse ifconfig directly (bypasses macOS ipconfig getifaddr quirks),
#          preferring RFC 1918 private subnets, then any non-Tailscale IP, then
#          the first address found.
#   Linux: ifconfig is often absent on Ubuntu — use iproute2 (`ip`), then
#          `hostname -I`, then ifconfig as a last resort.
detect_lan_ip() {
  if (( IS_MAC )); then
    # Parse all active non-loopback IPv4 addresses directly from ifconfig.
    local ip_list=($(ifconfig 2>/dev/null | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}'))
    if [[ ${#ip_list[@]} -gt 0 ]]; then
      # 1. Prefer standard RFC 1918 private subnets (192.168.x.x, 10.x.x.x, 172.16-31.x)
      for ip in "${ip_list[@]}"; do
        if [[ "$ip" == 192.168.* || "$ip" == 10.* || "$ip" == 172.1[6-9].* || "$ip" == 172.2[0-9].* || "$ip" == 172.3[0-1].* ]]; then
          echo "$ip"
          return
        fi
      done
      # 2. Next, prefer any IP that isn't Tailscale (starts with 100.)
      for ip in "${ip_list[@]}"; do
        if [[ "$ip" != 100.* ]]; then
          echo "$ip"
          return
        fi
      done
      # 3. Fall back to first IP found (which could be Tailscale)
      echo "${ip_list[1]}"
      return
    fi
    echo "Unknown-IP"
  else
    # Linux: iproute2 first, then hostname -I, then ifconfig.
    local ip
    ip=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
    [[ -z "$ip" ]] && ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [[ -z "$ip" ]] && ip=$(ifconfig 2>/dev/null | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | head -1)
    [[ -n "$ip" ]] && echo "$ip" || echo "Unknown-IP"
  fi
}

# detect_mdns_name — this host's advertised .local name, lowercased.
#   macOS: scutil --get LocalHostName + ".local".
#   Linux: <hostname -s> + ".local" (avahi advertises <hostname>.local).
#   Falls back to "localhost.local" if the short name can't be resolved.
detect_mdns_name() {
  local base=""
  if (( IS_MAC )); then
    base="$(scutil --get LocalHostName 2>/dev/null | tr 'A-Z' 'a-z')"
  else
    base="$(hostname -s 2>/dev/null | tr 'A-Z' 'a-z')"
  fi
  [[ -z "$base" ]] && base="localhost"
  echo "${base}.local"
}

# ---- Ports ----
# In STACK mode (plain `ferry up`) PORT is the ONE door clients use: litellm sits
# there and fans out to the cloud lanes plus the two MLX backends below, which
# listen on their own ports and are NOT meant to be addressed directly by clients.
PORT="8090"               # litellm front door — the single LAN endpoint
SHARE_PORT="8095"
HF_PORT="8096"
PROXY_PORT="8097"
RELAY_PORT="8098"         # reverse-expose control port — clients dial IN to publish OUT
LOCAL_ORCH_PORT="8092"    # MLX backend for the `local-orch` lane
LOCAL_SUB_PORT="8093"     # MLX backend for the `local-sub` lane
# NOTE: 8091 is deliberately skipped — `ferry dash` binds it. The stack and the
# dashboard are meant to run together, so the lanes start above it.

# ---- The five served lanes ----
# Lane names are the STABLE contract clients bind to; the model behind a lane is
# swappable without touching a single client config.
#
#   heavy        -> GPT-5.6 Sol via the ChatGPT subscription, no fallback chain
#                   (legacy names `orch`/`orchestrator` still resolve to it)
#   flash        -> Gemini 3.8 Flash via OpenRouter, 1 fallback hop to GPT-5.6 Luna
#   super-flash  -> same Gemini 3.8 Flash shape, minimal reasoning (housekeeping),
#                   1 fallback hop to GPT-5.6 Luna with reasoning off
#   local-orch   -> Qwen3.8-27B-nvfp4 on the host GPU (+ MTP speculative draft)
#   local-sub    -> NVIDIA Nemotron 3 Nano 30B A3B NVFP4 on the host GPU
#
# The cloud lanes live in the litellm route config; the two local lanes are the
# MLX servers this script launches, wired into that same config as
# openai-compatible backends on 127.0.0.1.

# Local ORCHESTRATOR lane — the "smart" local model: dense-ish 27B at nvfp4.
# MTP speculative decoding ON with UNQUANTIZED KV @64k (see the per-lane note
# below for the crash that rules out draft+quantized-KV).
LOCAL_MODEL_ORCH="mlx-community/Qwen3.8-27B-nvfp4"
LOCAL_DRAFT_ORCH="mlx-community/Qwen3.8-27B-MTP-8bit"

# Local SUBAGENT lane — nemotron_h hybrid MoE (6/52 full-attention layers, 2 KV
# heads) => ~6KB KV/token and ~3B active params, so several concurrent subagents
# stay fast and cheap on memory. No MTP draft is published for it -> no
# --draft-model on this lane.
LOCAL_MODEL_SUB="mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"
LOCAL_DRAFT_SUB=""

# Back-compat: the single-lane `--local` flag predates the stack and still means
# "serve the local orchestrator model on its own".
LOCAL_MODEL="$LOCAL_MODEL_ORCH"
LOCAL_DRAFT="$LOCAL_DRAFT_ORCH"

# KV-cache memory governor (measured 2026-08-25, 128GB M5 Max): the stock launch
# kept the full fp16 KV of EVERY request in the APC prefix cache — one 121k-token
# opencode session peaked the server at 97GB phys_footprint (GPU wired ceiling is
# ~90-100GB on a 128GB Mac => the "caps out around 30k tokens" wall under
# concurrent agents; `ps` RSS is blind to this, use `footprint <pid>`). With
# 4-bit KV + a bounded APC pool + a concurrent-seq cap, the SAME session peaked
# 56GB, idled at 35GB, and decoded ~60% faster (32 vs 20 tok/s at 64k context).
# Set any of these to "" to drop the flag from the launch line.
#
# STACK MODE runs BOTH local lanes at these same generous settings (~15GB + ~18GB
# of resident weights before any KV). That is a deliberate choice for best
# single-lane latency; the tradeoff is that two simultaneously-busy deep-context
# lanes CAN approach the wired ceiling. Watch it with `ferry status`, and shrink a
# lane by exporting the per-lane overrides below.
LOCAL_KV_BITS="4"         # --kv-bits: 4-bit KV cache quant (weights stay nvfp4)
LOCAL_MAX_KV="131072"     # --max-kv-size: prompt+max_tokens over this => clean 400, not OOM
LOCAL_MAX_SEQS="4"        # --max-num-seqs: max concurrent sequences (subagent fan-out)
LOCAL_APC_BLOCKS="512"    # APC_NUM_BLOCKS: retained prefix-pool size (x16 tokens)

# Per-lane overrides — export any of these to govern ONE lane without touching the
# other (e.g. LOCAL_SUB_MAX_KV=65536 to shrink the subagent lane's context budget).
#
# local-orch runs MTP speculative decoding + UNQUANTIZED KV @64k (measured
# 2026-08-25): the draft-verify path crashes on any quantized cache (tuple keys,
# AttributeError — turn 2 of every conversation 500s), but draft + full KV is
# stable (3/3 cache-hit requests 200) and decodes ~53% faster (37.8 vs 24.8
# tok/s). Full KV @64k = 16GB (256KB/token, 64 layers x 4 kv heads x 256 dim).
# To revert to the quantized no-draft lane: LOCAL_DRAFT_ORCH="" + kv-bits 4.
LOCAL_ORCH_KV_BITS=""
LOCAL_ORCH_MAX_KV="65536"
LOCAL_ORCH_MAX_SEQS="${LOCAL_ORCH_MAX_SEQS:-$LOCAL_MAX_SEQS}"
LOCAL_ORCH_APC_BLOCKS="${LOCAL_ORCH_APC_BLOCKS:-$LOCAL_APC_BLOCKS}"
LOCAL_SUB_KV_BITS="${LOCAL_SUB_KV_BITS:-$LOCAL_KV_BITS}"
LOCAL_SUB_MAX_KV="${LOCAL_SUB_MAX_KV:-$LOCAL_MAX_KV}"
LOCAL_SUB_MAX_SEQS="${LOCAL_SUB_MAX_SEQS:-$LOCAL_MAX_SEQS}"
LOCAL_SUB_APC_BLOCKS="${LOCAL_SUB_APC_BLOCKS:-$LOCAL_APC_BLOCKS}"
MDNS_NAME="$(detect_mdns_name)"

# Default cloud model for `ferry serve --cloud`.
#
# This one HAS to be a real provider/model id, not a lane name: --cloud runs a
# bare `litellm --model "$DEFAULT_CLOUD_MODEL"` with no route config at all, so
# there is no lane table to resolve against. It rides OpenRouter deliberately —
# one env var (OPENROUTER_API_KEY) and the stock api_base.
#
# Was `gemini/gemini-3.7-flash` (as DEFAULT_GEMINI) until v1.8.4. v1.8.0 retired
# the multi-project Gemini key pool, so `--cloud` had been defaulting to a key
# the host no longer holds and failing its own preflight check.
#
# Was `openrouter/z-ai/glm-5.3-flash` from v1.8.4 until 2026-09-04, when the
# route config was simplified and the `flash` lane it mirrors moved to
# OpenRouter Gemini 3.8 Flash.
DEFAULT_CLOUD_MODEL="openrouter/google/gemini-3.8-flash"

# Route mode: serve multiple models (orchestrator + failover workers) from a litellm config
DEFAULT_ROUTE_CONFIG="$HOME/.config/ferry/litellm.yaml"
ROUTE_TEMPLATE="$APP_DIR/litellm-route-example.yaml"

# Robust local IP discovery (OS-aware; see detect_lan_ip near the top of this
# script). macOS keeps the RFC-1918-prioritizing ifconfig parse; Linux uses the
# iproute2 path since ifconfig is often absent on Ubuntu.
get_lan_ip() {
  detect_lan_ip
}
LAN_IP=$(get_lan_ip)

# Dual-Mode Configuration: Detect if running on a Client laptop
CLIENT_MODE=0
CLIENT_HOST=""
CLIENT_PORT="8090"
CLIENT_SHARE_PORT="8095"
# v1.22.0: optional litellm master_key (LITELLM_MASTER_KEY) on the front door.
# Absent => the generators bake the legacy 'local' bearer, so a keyless LAN
# setup is unchanged. Held in a variable, never echoed.
CLIENT_MASTER_KEY=""
# v1.26.0: CLIENT_NAME identifies this caller to the front door's fleet
# resolver (X-Ferry-Client). On the host it is the literal 'host', matching
# the loopback identity the front door already assigns; on a client it comes
# from client.json's 'name', falling back to the short hostname (lower-cased)
# so a profile bootstrapped before this field existed still resolves an
# identity.
CLIENT_NAME="host"

CLIENT_CONF="$HOME/.config/ferry/client.json"
if [[ -f "$CLIENT_CONF" ]]; then
  CLIENT_MODE=1
  CLIENT_HOST=$(python3 -c "import json, os; print(json.load(open(os.path.expanduser('$CLIENT_CONF'))).get('host', ''))" 2>/dev/null || echo "")
  CLIENT_PORT=$(python3 -c "import json, os; print(json.load(open(os.path.expanduser('$CLIENT_CONF'))).get('port', '8090'))" 2>/dev/null || echo "8090")
  CLIENT_SHARE_PORT=$(python3 -c "import json, os; print(json.load(open(os.path.expanduser('$CLIENT_CONF'))).get('share_port', '8095'))" 2>/dev/null || echo "8095")
  CLIENT_MASTER_KEY=$(python3 -c "import json, os; print(json.load(open(os.path.expanduser('$CLIENT_CONF'))).get('master_key') or '')" 2>/dev/null || echo "")
  CLIENT_NAME=$(python3 -c "import json, os; print(json.load(open(os.path.expanduser('$CLIENT_CONF'))).get('name') or '')" 2>/dev/null || echo "")
  [[ -z "$CLIENT_NAME" ]] && CLIENT_NAME=$(hostname -s 2>/dev/null | tr 'A-Z' 'a-z')
fi

# Logging locations (Host Mode only)
LOG_DIR="${TMPDIR:-/tmp}/ferry-logs"
mkdir -p "$LOG_DIR"
LOCAL_LOG="$LOG_DIR/local-gpu-$PORT.log"
CLOUD_LOG="$LOG_DIR/cloud-proxy-$PORT.log"
# Stack mode gives each MLX lane its own log so a crash is attributable to a lane.
LOCAL_ORCH_LOG="$LOG_DIR/local-orch-$LOCAL_ORCH_PORT.log"
LOCAL_SUB_LOG="$LOG_DIR/local-sub-$LOCAL_SUB_PORT.log"
SHARE_LOG="$LOG_DIR/share-$SHARE_PORT.log"

# Client telemetry (`ferry msg` / `ferry log` -> the share server's /hq) lands here.
# NOT under $LOG_DIR: this one must outlive the checkout the share server was
# launched from and the temp dir the lane logs live in, so `ferry inbox` can still
# read it after a worktree is removed (v1.8.10). The share server's embedded handler
# carries this same path as a literal, because it runs as its own process — a test
# pins the two spellings together.
HQ_LOG="$HOME/.config/ferry/client_logs.txt"

# Reverse-expose state. The token is the only thing separating "a client of yours"
# from "anything that can reach the LAN", so it is 0600 and never logged. The
# published-ports file is what `ferry status` reads, written atomically by the
# relay so a status read never catches it half-written.
RELAY_TOKEN_FILE="$HOME/.config/ferry/relay-token"
RELAY_STATE_FILE="$HOME/.config/ferry/relay-published.json"
RELAY_LOG="$LOG_DIR/relay-$RELAY_PORT.log"

# Load local secrets if present (e.g. GEMINI_API_KEY). Export the variable in your
# shell, or drop it in ~/.config/ferry/secrets.env — never commit real API keys.
if [[ -f "$HOME/.config/ferry/secrets.env" ]]; then
  source "$HOME/.config/ferry/secrets.env" >/dev/null 2>&1 || true
fi
