
# Dynamic interactive catalog selector querying Gemini API live list
select_model_from_catalog() {
  echo "================================================================="
  echo "               FETCHING LIVE GEMINI MODELS LIST..."
  echo "================================================================="

  # Ensure API key is present
  if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    echo "Error: GEMINI_API_KEY is not set in your environment or ~/.config/ferry/secrets.env."
    echo "Please set GEMINI_API_KEY to access cloud models dynamically."
    echo "Fallback: launching the local-orch GPU lane instead."
    LAUNCH_MODE="local-orch"
    return
  fi

  # Call Gemini REST endpoint to grab the active model list
  local raw_models
  if ! raw_models=$(curl -fsS -m 5 "https://generativelanguage.googleapis.com/v1beta/models?key=${GEMINI_API_KEY}" 2>/dev/null); then
    echo "WARNING: Could not connect to Gemini's server to retrieve live list."
    echo "Fallback: launching the local-orch GPU lane instead."
    LAUNCH_MODE="local-orch"
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
# Option 1 is ALWAYS the full stack: every lane on one endpoint. Options 2-3 are the
# single GPU lanes for when you want ONE model on :8090 and nothing else resident.
options.append(("stack", "FULL STACK - orch + flash (cloud) + local-orch + local-sub (GPU), one endpoint"))
options.append(("local-orch", "Local GPU Qwen 3.8-27B nvfp4 only (local-orch lane, APC + speculative MTP)"))
options.append(("local-sub", "Local GPU NVIDIA Nemotron 3 Nano 30B A3B NVFP4 only (local-sub lane)"))

for _, m_id, m_desc in chat_models:
    options.append((f"gemini/{m_id}", f"[Cloud] {m_desc} (gemini/{m_id})"))

# Prompt the user via stderr to keep stdout clean for capturing the output model selection
sys.stderr.write("=================================================================\n")
sys.stderr.write("             LLM-FERRY ACTIVE MODEL CATALOG (NEWEST FIRST)\n")
sys.stderr.write("=================================================================\n")
for idx, (m_id, label) in enumerate(options, 1):
    sys.stderr.write(f"  {idx}) {label}\n")
sys.stderr.write("=================================================================\n")
sys.stderr.write(f"Select a lane to launch (1-{len(options)}) [Default: 1 = full stack]: ")
sys.stderr.flush()

try:
    # Read response directly from /dev/tty
    with open("/dev/tty", "r") as tty:
        choice_str = tty.readline().strip()
    choice = int(choice_str) if choice_str else 1
except Exception:
    choice = 1

if choice < 1 or choice > len(options):
    choice = 1

selected_id = options[choice - 1][0]
print(selected_id)
PYEOF
)

  if [[ "$chosen_model" == "stack" || "$chosen_model" == "local" \
     || "$chosen_model" == "local-orch" || "$chosen_model" == "local-sub" ]]; then
    LAUNCH_MODE="$chosen_model"
  elif [[ "$chosen_model" == "__ERROR:"* ]]; then
    echo "Error parsing live models. Falling back to the full stack."
    LAUNCH_MODE="stack"
  else
    LAUNCH_MODE="cloud"
    CLOUD_PROVIDER="gemini"
    CLOUD_MODEL="$chosen_model"
  fi
}

# ---- Stack helpers ---------------------------------------------------------
# `ferry up` (no args) runs the STACK: litellm on $PORT is the ONE door clients
# use, and it fans out to the cloud lanes plus two MLX servers on internal ports.
# These helpers exist so the stack and the single-lane flags launch MLX the same
# way — one launch line, one governor, no drift between them.

# _ferry_free_port <port> — stop whatever is holding <port> so a lane can bind it.
_ferry_free_port() {
  local p="$1"
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo ">>> Port $p is already in use. Stopping conflicting server..."
    lsof -ti tcp:"$p" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
}

# _ferry_launch_mlx <label> <model> <draft|""> <port> <log> <kv_bits> <max_kv> <max_seqs> <apc_blocks>
# Launch ONE mlx_vlm.server lane in the background under the KV/memory governor.
# Every governor flag is conditional, so passing "" for one drops it from the
# launch line rather than sending an empty value.
_ferry_launch_mlx() {
  local label="$1" model="$2" draft="$3" port="$4" log="$5"
  local kv_bits="$6" max_kv="$7" max_seqs="$8" apc_blocks="$9"

  # APC is env-driven, not a flag. Exporting immediately before each nohup means
  # each lane inherits ITS OWN pool size (the child snapshots the env at fork).
  export APC_ENABLED=1
  [[ -n "$apc_blocks" ]] && export APC_NUM_BLOCKS="$apc_blocks"

  local mlargs=(
    --model "$model"
    --host 0.0.0.0
    --port "$port"
  )
  [[ -n "$draft" ]]    && mlargs+=(--draft-model "$draft")
  [[ -n "$kv_bits" ]]  && mlargs+=(--kv-bits "$kv_bits")
  [[ -n "$max_kv" ]]   && mlargs+=(--max-kv-size "$max_kv")
  [[ -n "$max_seqs" ]] && mlargs+=(--max-num-seqs "$max_seqs")

  echo ">>> [$label] $model"
  echo "        port   :$port   draft=${draft:-none}"
  echo "        KV gov kv-bits=${kv_bits:-off} max-kv=${max_kv:-off} seqs=${max_seqs:-off} apc-blocks=${apc_blocks:-off}"
  echo "        log    $log"
  nohup mlx_vlm.server "${mlargs[@]}" > "$log" 2>&1 & disown
}

# _ferry_wait_http <url> <label> [timeout-seconds]
# Poll until <url> answers, so `ferry up` reports REAL readiness instead of
# "launched". mlx_vlm preloads inside its FastAPI lifespan, so the port does not
# accept connections until the weights are resident — any 200 here means the lane
# is genuinely warm, never "listening but still loading". A cold 15-18GB lane
# legitimately takes tens of seconds. Returns 1 on timeout; callers tolerate that
# (the lane keeps loading in the background).
_ferry_wait_http() {
  local url="$1" label="$2" timeout="${3:-600}" waited=0
  while (( waited < timeout )); do
    if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
      echo ">>> [$label] \033[1;32mREADY\033[0m (${waited}s)"
      return 0
    fi
    sleep 3
    waited=$(( waited + 3 ))
    (( waited % 15 == 0 )) && echo "    [$label] loading... ${waited}s"
  done
  echo ">>> [$label] \033[1;33mNOT READY\033[0m after ${timeout}s - still loading, or check its log."
  return 1
}

# _ferry_require_route_config — set FERRY_ROUTE_CONFIG, seeding it from the
# shipped template on first run (then exiting so keys can be filled in first).
# NOT a command-substitution helper on purpose: `exit` has to end `ferry up`,
# which it cannot do from inside a $(...) subshell.
_ferry_require_route_config() {
  FERRY_ROUTE_CONFIG="$DEFAULT_ROUTE_CONFIG"
  if [[ ! -f "$FERRY_ROUTE_CONFIG" ]]; then
    mkdir -p "$(dirname "$FERRY_ROUTE_CONFIG")"
    if [[ -f "$ROUTE_TEMPLATE" ]]; then
      cp "$ROUTE_TEMPLATE" "$FERRY_ROUTE_CONFIG"
      echo ">>> Seeded route config from template:"
      echo "    $FERRY_ROUTE_CONFIG"
      echo "    Edit it (set your model ids), export the keys it references"
      echo "    (e.g. GLM_API_KEY, GEMINI_API_KEY, GEMINI_API_KEY_2), then re-run."
      exit 0
    fi
    echo "Error: No route config at $FERRY_ROUTE_CONFIG and no template at $ROUTE_TEMPLATE."
    exit 1
  fi
}

# _ferry_warn_missing_keys — warn (never hard-fail) about unset keys the shipped
# route template references. A lane whose key is missing 401s; the others are fine.
_ferry_warn_missing_keys() {
  local missing=()
  [[ -z "${GLM_API_KEY:-}" ]]      && missing+=("GLM_API_KEY (orch primary)")
  [[ -z "${GEMINI_API_KEY:-}" ]]   && missing+=("GEMINI_API_KEY (flash pool)")
  [[ -z "${GEMINI_API_KEY_2:-}" ]] && missing+=("GEMINI_API_KEY_2 (flash pool)")
  if (( ${#missing[@]} > 0 )); then
    echo ">>> WARNING: these env vars are unset; lanes that need them will 401:"
    for m in "${missing[@]}"; do echo "      - $m"; done
    echo "    Export them in your shell or ~/.config/ferry/secrets.env."
  fi
}

cmd_up() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry up' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi

  local LAUNCH_MODE="stack" # Default if arguments parsed override it
  local CLOUD_PROVIDER=""
  local CLOUD_MODEL=""
  local target_port="$PORT"
  local skip_catalog=0

  # No arguments = the FULL STACK (all four lanes on one endpoint). The
  # interactive catalog, which used to be the no-arg default, now lives behind -i.
  if [[ $# -eq 0 ]]; then
    LAUNCH_MODE="stack"
    skip_catalog=1
  fi
  
  if (( ! skip_catalog )); then
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -a|--all|--stack)
          LAUNCH_MODE="stack"
          skip_catalog=1
          shift
          ;;
        -l|--local|--local-orch)
          # The local ORCHESTRATOR lane alone on the target port.
          LAUNCH_MODE="local-orch"
          skip_catalog=1
          shift
          ;;
        -s|--sub|--local-sub)
          # The local SUBAGENT lane alone on the target port.
          LAUNCH_MODE="local-sub"
          skip_catalog=1
          shift
          ;;
        -o|--orch)
          # `--orch` predates the lane split, when the local orchestrator WAS
          # Nemotron. The orchestrator lane is now Qwen; Nemotron is the subagent
          # lane. Point --orch at whatever "local orchestrator" currently means
          # and say so, so a muscle-memory invocation is not silently redefined.
          echo ">>> Note: the local orchestrator lane is now $LOCAL_MODEL_ORCH."
          echo "    Nemotron moved to the subagent lane - use 'ferry up --local-sub' for it."
          LAUNCH_MODE="local-orch"
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

  # ── Shared guard for every lane that needs the GPU ───────────────────────
  # Local GPU serving is Apple MLX — macOS / Apple Silicon only. In STACK mode a
  # non-Mac degrades to the cloud lanes rather than failing outright, because the
  # cloud half of the stack is perfectly servable on Linux.
  if [[ "$LAUNCH_MODE" == "stack" || "$LAUNCH_MODE" == local-* ]]; then
    if (( ! IS_MAC )); then
      if [[ "$LAUNCH_MODE" == "stack" ]]; then
        echo ">>> Local GPU lanes need Apple MLX (macOS / Apple Silicon only)."
        echo "    Serving the CLOUD lanes only (orch + flash) — same endpoint, two lanes."
        LAUNCH_MODE="route"
      else
        echo "Error: local GPU serving uses Apple MLX (macOS / Apple Silicon only)."
        echo "       On Linux, serve a cloud / OpenAI-compatible endpoint instead:"
        echo "         ferry up --route        # multiple models from litellm.yaml"
        echo "         ferry up --cloud        # default Gemini model"
        echo "         ferry up --model <id>   # any LiteLLM model string"
        exit 1
      fi
    elif ! command -v mlx_vlm.server >/dev/null 2>&1; then
      echo "Error: 'mlx_vlm.server' is missing. Run: ferry install"
      exit 1
    fi
  fi

  if [[ "$LAUNCH_MODE" == "stack" ]]; then
    # ── THE STACK: one door, four lanes ────────────────────────────────────
    #   litellm on $target_port  ->  orch        (cloud: GLM 5.3 + fallback chain)
    #                            ->  flash       (cloud: Gemini 3.7 Flash key pool)
    #                            ->  local-orch  (MLX on :$LOCAL_ORCH_PORT)
    #                            ->  local-sub   (MLX on :$LOCAL_SUB_PORT)
    # The two MLX ports are INTERNAL plumbing — clients only ever talk to
    # $target_port, and the lane names there are the contract they bind to.
    if ! command -v litellm >/dev/null 2>&1; then
      echo "Error: 'litellm' is missing. Run: ferry install"
      exit 1
    fi
    _ferry_require_route_config
    _ferry_warn_missing_keys

    echo "================================================================="
    echo "   FERRY STACK — four lanes, one endpoint"
    echo "================================================================="
    echo "   orch         cloud   GLM 5.3 + strict fallback chain"
    echo "   flash        cloud   Gemini 3.7 Flash key pool"
    echo "   local-orch   GPU     $LOCAL_MODEL_ORCH"
    echo "   local-sub    GPU     $LOCAL_MODEL_SUB"
    echo "================================================================="

    _ferry_free_port "$LOCAL_ORCH_PORT"
    _ferry_free_port "$LOCAL_SUB_PORT"

    # Both MLX lanes start first and load CONCURRENTLY: the two loads are
    # dominated by streaming ~33GB out of the HF cache, so overlapping them is
    # markedly faster than serialising, and neither blocks the other's warm-up.
    _ferry_launch_mlx "local-orch" "$LOCAL_MODEL_ORCH" "$LOCAL_DRAFT_ORCH" \
      "$LOCAL_ORCH_PORT" "$LOCAL_ORCH_LOG" \
      "$LOCAL_ORCH_KV_BITS" "$LOCAL_ORCH_MAX_KV" "$LOCAL_ORCH_MAX_SEQS" "$LOCAL_ORCH_APC_BLOCKS"
    _ferry_launch_mlx "local-sub" "$LOCAL_MODEL_SUB" "$LOCAL_DRAFT_SUB" \
      "$LOCAL_SUB_PORT" "$LOCAL_SUB_LOG" \
      "$LOCAL_SUB_KV_BITS" "$LOCAL_SUB_MAX_KV" "$LOCAL_SUB_MAX_SEQS" "$LOCAL_SUB_APC_BLOCKS"

    # litellm does NOT probe its backends at boot, so the front door can come up
    # in parallel with the GPU lanes. A call that arrives before a lane is warm
    # fails on THAT lane only — the cloud lanes are servable immediately.
    echo ">>> [front] litellm --config $FERRY_ROUTE_CONFIG"
    echo "        port   :$target_port"
    echo "        log    $cloud_log"
    nohup litellm \
      --config "$FERRY_ROUTE_CONFIG" \
      --port "$target_port" \
      --host 0.0.0.0 > "$cloud_log" 2>&1 & disown

    echo ">>> Waiting for lanes (MLX loads ~33GB of weights — that is the slow part)..."
    _ferry_wait_http "http://127.0.0.1:$target_port/v1/models"       "front"      120 || true
    _ferry_wait_http "http://127.0.0.1:$LOCAL_ORCH_PORT/v1/models"   "local-orch" 900 || true
    _ferry_wait_http "http://127.0.0.1:$LOCAL_SUB_PORT/v1/models"    "local-sub"  900 || true

    echo "================================================================="
    echo ">>> Stack up. Lanes served on http://$MDNS_NAME:$target_port/v1 :"
    curl -fsS -m 5 "http://127.0.0.1:$target_port/v1/models" 2>/dev/null \
      | python3 -c "import json,sys; [print('       ' + m['id']) for m in json.load(sys.stdin).get('data', [])]" 2>/dev/null \
      || echo "       (front door not answering yet — check $cloud_log)"
    echo "-----------------------------------------------------------------"
    echo "    Onboard clients:  ferry share"
    echo "    Live dashboard:   ferry dash"
    echo "    Stop everything:  ferry down"
    echo "================================================================="

  elif [[ "$LAUNCH_MODE" == "local-orch" || "$LAUNCH_MODE" == "local" ]]; then
    # ONE lane on the target port: the local orchestrator model, no litellm in
    # front. Clients address it by its HuggingFace id, not by a lane name.
    echo ">>> Launching the local-orch lane alone (no route proxy)."
    _ferry_launch_mlx "local-orch" "$LOCAL_MODEL_ORCH" "$LOCAL_DRAFT_ORCH" \
      "$target_port" "$LOCAL_LOG" \
      "$LOCAL_ORCH_KV_BITS" "$LOCAL_ORCH_MAX_KV" "$LOCAL_ORCH_MAX_SEQS" "$LOCAL_ORCH_APC_BLOCKS"
    _ferry_wait_http "http://127.0.0.1:$target_port/v1/models" "local-orch" 900 || true

  elif [[ "$LAUNCH_MODE" == "local-sub" ]]; then
    # ONE lane on the target port: the local subagent model (nemotron_h hybrid
    # MoE). Raise LOCAL_SUB_MAX_SEQS to admit more concurrent agents.
    echo ">>> Launching the local-sub lane alone (no route proxy)."
    echo "    (subagent fan-out: raise LOCAL_SUB_MAX_SEQS to admit more concurrent agents)"
    _ferry_launch_mlx "local-sub" "$LOCAL_MODEL_SUB" "$LOCAL_DRAFT_SUB" \
      "$target_port" "$LOCAL_LOG" \
      "$LOCAL_SUB_KV_BITS" "$LOCAL_SUB_MAX_KV" "$LOCAL_SUB_MAX_SEQS" "$LOCAL_SUB_APC_BLOCKS"
    _ferry_wait_http "http://127.0.0.1:$target_port/v1/models" "local-sub" 900 || true

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
    # The CLOUD half of the stack: the litellm route config without the local GPU
    # lanes. Same config file, same lane names — `local-orch`/`local-sub` are still
    # listed by /v1/models but have no backend running, so calls to them fail.
    if ! command -v litellm >/dev/null 2>&1; then
      echo "Error: 'litellm' is missing. Run: ferry install"
      exit 1
    fi

    _ferry_require_route_config
    _ferry_warn_missing_keys

    echo ">>> Serving the CLOUD lanes via litellm route config:"
    echo "    Config: $FERRY_ROUTE_CONFIG"
    echo "    Port:   $target_port"

    nohup litellm \
      --config "$FERRY_ROUTE_CONFIG" \
      --port "$target_port" \
      --host 0.0.0.0 > "$cloud_log" 2>&1 & disown

    echo ">>> Route proxy running in background. Log: $cloud_log"
    echo "    Served lanes:  curl -s http://127.0.0.1:$target_port/v1/models"
  fi
}

cmd_down() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry down' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi

  echo ">>> Stopping all LLM-Ferry servers, cloud proxies, and share servers..."

  # Terminate local model servers. One pkill covers BOTH stack lanes (they are the
  # same binary on different ports) as well as any single-lane server.
  pkill -f mlx_vlm.server || true

  # Belt-and-braces: free the stack's internal lane ports even if something OTHER
  # than mlx_vlm.server ended up holding one (a wedged uvicorn child, a stale
  # process from a killed run). Without this a later `ferry up` finds the port
  # taken and the lane silently never binds.
  for _p in "$LOCAL_ORCH_PORT" "$LOCAL_SUB_PORT"; do
    if lsof -nP -iTCP:"$_p" -sTCP:LISTEN >/dev/null 2>&1; then
      lsof -ti tcp:"$_p" | xargs kill -9 2>/dev/null || true
    fi
  done
  
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

  # Check active ports. $PORT is the client-facing door; the two lane ports are
  # INTERNAL backends (only populated in stack mode) and are labelled as such so
  # an OFFLINE lane port is not mistaken for the endpoint being down.
  local _label
  for p in "$PORT" "$LOCAL_ORCH_PORT" "$LOCAL_SUB_PORT" "$SHARE_PORT"; do
    case "$p" in
      "$PORT")            _label="endpoint" ;;
      "$LOCAL_ORCH_PORT") _label="local-orch lane (internal)" ;;
      "$LOCAL_SUB_PORT")  _label="local-sub lane (internal)" ;;
      "$SHARE_PORT")      _label="client share" ;;
      *)                  _label="" ;;
    esac
    if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
      local pid=$(lsof -t -iTCP:"$p" -sTCP:LISTEN | head -1)
      local cmd=$(ps -p "$pid" -o comm= 2>/dev/null | xargs basename 2>/dev/null || echo "unknown")
      echo ">>> Port $p ($_label) is \033[1;32mONLINE\033[0m (PID: $pid, Command: $cmd)"

      # phys_footprint is the number that matters for an MLX lane — RSS is blind
      # to wired GPU memory, so `ps` cheerfully under-reports a 50GB model server.
      if [[ "$p" == "$LOCAL_ORCH_PORT" || "$p" == "$LOCAL_SUB_PORT" ]] && command -v footprint >/dev/null 2>&1; then
        local fp=$(footprint "$pid" 2>/dev/null | grep -iE "phys_footprint" | head -1 | tr -s ' ')
        [[ -n "$fp" ]] && echo "    Memory: $fp"
      fi

      if [[ "$p" == "$LOCAL_ORCH_PORT" || "$p" == "$LOCAL_SUB_PORT" ]]; then
        # An MLX lane's /v1/models lists the whole HuggingFace CACHE, not what is
        # loaded — printing it would advertise eight models this lane cannot serve
        # without a reload. The launch line is the truth, so read --model from argv.
        local loaded=$(ps -p "$pid" -o args= 2>/dev/null | sed -E 's/.*--model[= ]+([^ ]+).*/\1/')
        [[ -n "$loaded" ]] && echo "    Model loaded: \033[1;32m$loaded\033[0m"
        echo "    (internal backend — address this lane as a lane name on :$PORT, not here)"
      elif [[ "$p" != "$SHARE_PORT" ]]; then
        # The front door: list every lane it serves, not just the first one.
        local models=$(curl -fsS -m 3 "http://127.0.0.1:$p/v1/models" 2>/dev/null || echo "")
        if [[ -n "$models" ]]; then
          local served=$(echo "$models" | python3 -c "import json,sys; print(' '.join(m['id'] for m in json.load(sys.stdin).get('data',[])))" 2>/dev/null || echo "")
          if [[ -n "$served" ]]; then
            echo "    Lanes served: \033[1;32m$served\033[0m"
            local first=${served%% *}
            echo "    Test with: curl -fsS -H \"Content-Type: application/json\" -d '{\"model\":\"$first\",\"messages\":[{\"role\":\"user\",\"content\":\"Say Hello!\"}],\"max_tokens\":10}' http://$MDNS_NAME:$p/v1/chat/completions"
          fi
        fi
      fi
    else
      echo ">>> Port $p ($_label) is \033[1;31mOFFLINE\033[0m"
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
