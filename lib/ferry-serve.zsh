
# Dynamic interactive catalog selector querying Gemini API live list
select_model_from_catalog() {
  echo "================================================================="
  echo "               FETCHING LIVE GEMINI MODELS LIST..."
  echo "================================================================="

  # Ensure API key is present
  if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    echo "Error: GEMINI_API_KEY is not set in your environment or ~/.config/ferry/secrets.env."
    echo "Please set GEMINI_API_KEY to access cloud models dynamically."
    echo "Fallback: Launching Local GPU model instead."
    LAUNCH_MODE="local"
    return
  fi

  # Call Gemini REST endpoint to grab the active model list
  local raw_models
  if ! raw_models=$(curl -fsS -m 5 "https://generativelanguage.googleapis.com/v1beta/models?key=${GEMINI_API_KEY}" 2>/dev/null); then
    echo "WARNING: Could not connect to Gemini's server to retrieve live list."
    echo "Fallback: Launching Local GPU model instead."
    LAUNCH_MODE="local"
    return
  fi

  # Run Python script to parse, filter for generateContent, sort newest models first, and display a menu
  local chosen_model
  chosen_model=$(python3 - "$raw_models" <<'PYEOF'
import json, sys, re

# Read API response
try:
    data = json.loads(sys.argv[1])
except Exception:
    print("__ERROR:Failed to parse response JSON__")
    sys.exit(0)

models_list = data.get("models", [])

# Filter for text/generation models
chat_models = []
for m in models_list:
    name = m.get("name", "")
    # Remove prefix "models/"
    short_name = name.split("/")[-1] if "/" in name else name
    
    # Filter out embedding, semantic, translation, or legacy models
    methods = m.get("supportedGenerationMethods", [])
    if "generateContent" in methods and "embedContent" not in name and "text-embedding" not in name:
        # Match version strings for sorting
        # Prioritize 3.7, 2.0, 1.5, in descending order
        version_weight = 0.0
        if "3.7" in short_name:
            version_weight += 300.0
        elif "2.0" in short_name:
            version_weight += 200.0
        elif "1.5" in short_name:
            version_weight += 100.0
            
        # Give higher priority to flash/pro over experimental/older
        if "pro" in short_name:
            version_weight += 10.0
        elif "flash" in short_name:
            version_weight += 5.0
            
        # Push preview/thinking/experimental variants slightly down relative to stable versions
        if "thinking" in short_name or "preview" in short_name or "experimental" in short_name:
            version_weight -= 2.0
            
        chat_models.append((version_weight, short_name, m.get("displayName", short_name)))

# Sort descending (newest versions first)
chat_models.sort(key=lambda x: x[0], reverse=True)

# Generate list of options
options = []
# Option 1 is ALWAYS the local Apple Silicon GPU Qwen model
options.append(("local", "Local GPU Qwen 3.8-27B (APC + Speculative MTP)"))

for _, m_id, m_desc in chat_models:
    options.append((f"gemini/{m_id}", f"[Cloud] {m_desc} (gemini/{m_id})"))

# Prompt the user via stderr to keep stdout clean for capturing the output model selection
sys.stderr.write("=================================================================\n")
sys.stderr.write("             LLM-FERRY ACTIVE MODEL CATALOG (NEWEST FIRST)\n")
sys.stderr.write("=================================================================\n")
for idx, (m_id, label) in enumerate(options, 1):
    sys.stderr.write(f"  {idx}) {label}\n")
sys.stderr.write("=================================================================\n")
sys.stderr.write(f"Select a model to launch (1-{len(options)}) [Default: 2]: ")
sys.stderr.flush()

try:
    # Read response directly from /dev/tty
    with open("/dev/tty", "r") as tty:
        choice_str = tty.readline().strip()
    choice = int(choice_str) if choice_str else 2
except Exception:
    choice = 2

if choice < 1 or choice > len(options):
    choice = 2

selected_id = options[choice - 1][0]
print(selected_id)
PYEOF
)

  if [[ "$chosen_model" == "local" ]]; then
    LAUNCH_MODE="local"
  elif [[ "$chosen_model" == "__ERROR:"* ]]; then
    echo "Error parsing live models. Falling back to Local GPU model."
    LAUNCH_MODE="local"
  else
    LAUNCH_MODE="cloud"
    CLOUD_PROVIDER="gemini"
    CLOUD_MODEL="$chosen_model"
  fi
}

cmd_up() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry up' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi

  local LAUNCH_MODE="local" # Default if arguments parsed override it
  local CLOUD_PROVIDER=""
  local CLOUD_MODEL=""
  local target_port="$PORT"
  local skip_catalog=0

  # If no arguments passed, launch the interactive model catalog!
  if [[ $# -eq 0 ]]; then
    select_model_from_catalog
    skip_catalog=1
  fi
  
  if (( ! skip_catalog )); then
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -l|--local)
          LAUNCH_MODE="local"
          skip_catalog=1
          shift
          ;;
        -i|--interactive)
          select_model_from_catalog
          skip_catalog=1
          shift
          ;;
        -c|--cloud)
          LAUNCH_MODE="cloud"
          CLOUD_PROVIDER="gemini"
          CLOUD_MODEL="$DEFAULT_GEMINI"
          skip_catalog=1
          shift
          ;;
        -r|--route)
          LAUNCH_MODE="route"
          skip_catalog=1
          shift
          ;;
        -m|--model)
          LAUNCH_MODE="cloud"
          CLOUD_MODEL="$2"
          skip_catalog=1
          if [[ "$CLOUD_MODEL" == gemini/* ]]; then
            CLOUD_PROVIDER="gemini"
          else
            CLOUD_PROVIDER="generic"
          fi
          shift 2
          ;;
        -p|--port)
          target_port="$2"
          shift 2
          ;;
        *)
          echo "Unknown option: $1"
          usage
          ;;
      esac
    done
  fi

  # Stop any conflicting server on the target port first
  if lsof -nP -iTCP:"$target_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo ">>> Port $target_port is already in use. Stopping conflicting server..."
    lsof -ti tcp:"$target_port" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi

  # Port-accurate cloud/route log name (the global CLOUD_LOG is pinned to the default port).
  local cloud_log="$LOG_DIR/cloud-proxy-$target_port.log"

  if [[ "$LAUNCH_MODE" == "local" ]]; then
    # Local GPU serving is Apple MLX — macOS / Apple Silicon only. Guard every
    # path that lands here (explicit --local, the interactive catalog, and its
    # key-missing/offline fallbacks) so Linux never tries to exec mlx_vlm.server.
    if (( ! IS_MAC )); then
      echo "Error: local GPU serving uses Apple MLX (macOS / Apple Silicon only)."
      echo "       On Linux, serve a cloud / OpenAI-compatible endpoint instead:"
      echo "         ferry up --route        # multiple models from litellm.yaml"
      echo "         ferry up --cloud        # default Gemini model"
      echo "         ferry up --model <id>   # any LiteLLM model string"
      exit 1
    fi

    # Start local GPU server
    if ! command -v mlx_vlm.server >/dev/null 2>&1; then
      echo "Error: 'mlx_vlm.server' is missing. Run: ferry install"
      exit 1
    fi
    
    export APC_ENABLED=1
    echo ">>> Launching local GPU model server with APC and MTP..."
    echo "    Model: $LOCAL_MODEL"
    echo "    Port:  $target_port"
    
    nohup mlx_vlm.server \
      --model "$LOCAL_MODEL" \
      --draft-model "$LOCAL_DRAFT" \
      --host 0.0.0.0 \
      --port "$target_port" > "$LOCAL_LOG" 2>&1 & disown
      
    echo ">>> Running in background. Log: $LOCAL_LOG"
    
  elif [[ "$LAUNCH_MODE" == "cloud" ]]; then
    # Start cloud API proxy
    if ! command -v litellm >/dev/null 2>&1; then
      echo "Error: 'litellm' is missing. Run: ferry install"
      exit 1
    fi

    # Verify key is present
    if [[ -z "${GEMINI_API_KEY:-}" ]]; then
      echo "Error: GEMINI_API_KEY is not set in your environment or ~/.config/ferry/secrets.env."
      exit 1
    fi

    echo ">>> Proxying to Cloud Model: \033[1;32m$CLOUD_MODEL\033[0m"
    echo "    Port: $target_port"
    
    # Launch LiteLLM proxy
    nohup litellm \
      --model "$CLOUD_MODEL" \
      --port "$target_port" \
      --host 0.0.0.0 > "$cloud_log" 2>&1 & disown

    echo ">>> Cloud proxy running in background. Log: $cloud_log"
  elif [[ "$LAUNCH_MODE" == "route" ]]; then
    # Multi-model routing via a litellm config: an orchestrator model plus
    # worker model(s) with automatic key failover (two same-named deployments).
    if ! command -v litellm >/dev/null 2>&1; then
      echo "Error: 'litellm' is missing. Run: ferry install"
      exit 1
    fi

    local route_config="$DEFAULT_ROUTE_CONFIG"

    # First run: seed the config from the shipped template, then STOP so the
    # user can set their model ids / keys before we launch anything.
    if [[ ! -f "$route_config" ]]; then
      mkdir -p "$(dirname "$route_config")"
      if [[ -f "$ROUTE_TEMPLATE" ]]; then
        cp "$ROUTE_TEMPLATE" "$route_config"
        echo ">>> Seeded route config from template:"
        echo "    $route_config"
        echo "    Edit it (set your model ids), export the keys it references"
        echo "    (e.g. KIMI_API_KEY, GEMINI_API_KEY, GEMINI_API_KEY_2), then"
        echo "    re-run 'ferry up --route'."
        exit 0
      else
        echo "Error: No route config at $route_config and no template at $ROUTE_TEMPLATE."
        exit 1
      fi
    fi

    # Warn (don't hard-fail) on unset keys the default template expects.
    local missing=()
    [[ -z "${KIMI_API_KEY:-}" ]]     && missing+=("KIMI_API_KEY")
    [[ -z "${GEMINI_API_KEY:-}" ]]   && missing+=("GEMINI_API_KEY")
    [[ -z "${GEMINI_API_KEY_2:-}" ]] && missing+=("GEMINI_API_KEY_2 (failover)")
    if (( ${#missing[@]} > 0 )); then
      echo ">>> WARNING: these env vars are unset; models that need them will 401:"
      for m in "${missing[@]}"; do echo "      - $m"; done
      echo "    Export them in your shell or ~/.config/ferry/secrets.env."
    fi

    echo ">>> Serving MULTIPLE models via litellm route config:"
    echo "    Config: $route_config"
    echo "    Port:   $target_port"

    nohup litellm \
      --config "$route_config" \
      --port "$target_port" \
      --host 0.0.0.0 > "$cloud_log" 2>&1 & disown

    echo ">>> Route proxy running in background. Log: $cloud_log"
    echo "    Served models:  curl -s http://127.0.0.1:$target_port/v1/models"
  fi
}

cmd_down() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry down' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi

  echo ">>> Stopping all LLM-Ferry servers, cloud proxies, and share servers..."
  
  # Terminate local model servers
  pkill -f mlx_vlm.server || true
  
  # Terminate LiteLLM proxies
  pkill -f "litellm --model" || true

  # Route mode runs `litellm --config …`, which the --model matcher above misses.
  pkill -f "litellm --config" || true

  # Terminate sharing Python servers. They run as `python3 - <port> <dir> ferry-share-marker`
  # with the script fed on stdin via heredoc, so "DynamicHandler" lives in stdin and never
  # appears in argv — `pkill -f DynamicHandler` could never match them and leaked a server
  # every session. We tag them with a stable sentinel arg and match that instead.
  pkill -f "ferry-share-marker" || true
  pkill -f "host-share.sh" || true

  # Terminate the experimental HuggingFace pass-through proxy (tagged with its sentinel arg).
  pkill -f "ferry-hf-marker" || true

  # Terminate the general HTTP(S) download forward proxy (tagged with its sentinel arg).
  pkill -f "ferry-proxy-marker" || true

  echo ">>> Success: All servers stopped."
}

cmd_status() {
  if (( CLIENT_MODE )); then
    echo "================================================================="
    echo "                 LLM-FERRY CLIENT DIAGNOSTICS"
    echo "================================================================="
    echo "Config Profile:      $CLIENT_CONF"
    echo "Host Target Server:  http://$CLIENT_HOST:$CLIENT_PORT"
    echo "Host Telemetry Port: http://$CLIENT_HOST:$CLIENT_SHARE_PORT"
    echo "================================================================="
    
    echo ">>> Probing network connectivity to Host Mac..."
    if curl -fsS -m 3 "http://$CLIENT_HOST:$CLIENT_PORT/v1/models" >/dev/null 2>&1; then
      echo ">>> Connection Health: \033[1;32mONLINE\033[0m"
      
      # Query active model
      local models=$(curl -fsS -m 2 "http://$CLIENT_HOST:$CLIENT_PORT/v1/models" 2>/dev/null || echo "")
      if [[ -n "$models" ]]; then
        local active=$(echo "$models" | python3 -c "import json,sys; d=json.load(sys.stdin).get('data',[]); print(d[0]['id'] if d else 'None')")
        echo "    Currently active model on Host: \033[1;32m$active\033[0m"
      fi
    else
      echo ">>> Connection Health: \033[1;31mOFFLINE\033[0m (Check Wi-Fi/cable or network status)"
    fi
    echo "================================================================="
    return
  fi

  # Host status
  echo "================================================================="
  echo "                 LLM-FERRY SYSTEM ACTIVE LISTENERS"
  echo "================================================================="
  echo "Host mDNS Domain:    http://$MDNS_NAME"
  echo "Host active LAN IP:  http://$LAN_IP"
  echo "================================================================="

  # Check active ports
  for p in 8090 8095; do
    if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
      local pid=$(lsof -t -iTCP:"$p" -sTCP:LISTEN)
      local cmd=$(ps -p "$pid" -o comm= 2>/dev/null | xargs basename 2>/dev/null || echo "unknown")
      echo ">>> Port $p is \033[1;32mONLINE\033[0m (PID: $pid, Command: $cmd)"
      
      # If port is our model server, query the active model
      if [[ "$cmd" == "Python" || "$cmd" == "python3" || "$cmd" == "litellm" || "$p" == "8090" ]]; then
        local models=$(curl -fsS -m 2 "http://127.0.0.1:$p/v1/models" 2>/dev/null || echo "")
        if [[ -n "$models" ]]; then
          local active=$(echo "$models" | python3 -c "import json,sys; d=json.load(sys.stdin).get('data',[]); print(d[0]['id'] if d else 'None')")
          echo "    Active Model loaded: \033[1;32m$active\033[0m"
          echo "    Test with: curl -fsS -H \"Content-Type: application/json\" -d '{\"model\":\"$active\",\"messages\":[{\"role\":\"user\",\"content\":\"Say Hello!\"}],\"max_tokens\":10}' http://$MDNS_NAME:$p/v1/chat/completions"
        fi
      fi
    else
      echo ">>> Port $p is \033[1;31mOFFLINE\033[0m"
    fi
  done

  # Experimental HuggingFace pass-through proxy (only reported when running).
  if lsof -nP -iTCP:"$HF_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    local hf_pid=$(lsof -t -iTCP:"$HF_PORT" -sTCP:LISTEN)
    echo ">>> Port $HF_PORT is \033[1;32mONLINE\033[0m (HF pass-through proxy, PID: $hf_pid)"
  fi

  # General HTTP(S) download forward proxy (only reported when running).
  if lsof -nP -iTCP:"$PROXY_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo ">>> HTTP download proxy is \033[1;32mONLINE\033[0m (port $PROXY_PORT)"
  fi

  # Offered files manifest (from `ferry offer`).
  local offered_file="$HOME/.config/ferry/offered.json"
  if [[ -f "$offered_file" ]]; then
    local offered_count=$(python3 -c "import json; print(len(json.load(open('$offered_file'))))" 2>/dev/null || echo "?")
    echo ">>> Offered files: $offered_count  ($offered_file)"
  fi
  echo "================================================================="
}
