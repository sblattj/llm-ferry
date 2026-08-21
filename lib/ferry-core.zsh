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
#   ferry msg <text>             # [Client] Send a direct text message to host's client_logs.txt
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

PORT="8090"
SHARE_PORT="8095"
HF_PORT="8096"
PROXY_PORT="8097"
LOCAL_MODEL="mlx-community/Qwen3.8-27B-nvfp4"
LOCAL_DRAFT="mlx-community/Qwen3.8-27B-MTP-8bit"
MDNS_NAME="$(detect_mdns_name)"

# Default cloud model associations
DEFAULT_GEMINI="gemini/gemini-3.7-flash"

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

CLIENT_CONF="$HOME/.config/ferry/client.json"
if [[ -f "$CLIENT_CONF" ]]; then
  CLIENT_MODE=1
  CLIENT_HOST=$(python3 -c "import json, os; print(json.load(open(os.path.expanduser('$CLIENT_CONF'))).get('host', ''))" 2>/dev/null || echo "")
  CLIENT_PORT=$(python3 -c "import json, os; print(json.load(open(os.path.expanduser('$CLIENT_CONF'))).get('port', '8090'))" 2>/dev/null || echo "8090")
  CLIENT_SHARE_PORT=$(python3 -c "import json, os; print(json.load(open(os.path.expanduser('$CLIENT_CONF'))).get('share_port', '8095'))" 2>/dev/null || echo "8095")
fi

# Logging locations (Host Mode only)
LOG_DIR="${TMPDIR:-/tmp}/ferry-logs"
mkdir -p "$LOG_DIR"
LOCAL_LOG="$LOG_DIR/local-gpu-$PORT.log"
CLOUD_LOG="$LOG_DIR/cloud-proxy-$PORT.log"
SHARE_LOG="$LOG_DIR/share-$SHARE_PORT.log"

# Load local secrets if present (e.g. GEMINI_API_KEY). Export the variable in your
# shell, or drop it in ~/.config/ferry/secrets.env — never commit real API keys.
if [[ -f "$HOME/.config/ferry/secrets.env" ]]; then
  source "$HOME/.config/ferry/secrets.env" >/dev/null 2>&1 || true
fi
