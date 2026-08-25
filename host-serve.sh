#!/bin/zsh
# host-serve.sh — Decoupled high-performance LAN inference server for Apple Silicon Macs.
# Launches mlx_vlm.server with automatic prefix caching (APC) and optional speculative decoding (MTP).
# Bound to 0.0.0.0 so any machine on the local LAN can connect.

set -eu

# Default high-performance configurations (Benchmarked for M-series Max/Ultra chips)
DEFAULT_MODEL="mlx-community/Qwen3.8-27B-nvfp4"
DEFAULT_DRAFT="mlx-community/Qwen3.8-27B-MTP-8bit"
DEFAULT_PORT="8090"
DEFAULT_HOST="0.0.0.0"
# KV-cache memory governor (measured 2026-08-25 on 128GB M5 Max): stock flags let
# the APC prefix cache retain every request's full fp16 KV — a 121k-token opencode
# session peaked at 97GB phys_footprint (GPU wired ceiling ~90-100GB => "caps out
# around 30k tokens" under concurrency; watch with `footprint <pid>`, not ps).
# With these: same session peaked 56GB, idled 35GB, decoded ~60% faster.
# Empty value disables the corresponding flag.
DEFAULT_KV_BITS="4"
DEFAULT_MAX_KV="131072"
DEFAULT_MAX_SEQS="4"
DEFAULT_APC_BLOCKS="512"

# Help Banner
usage() {
  cat <<EOF
Usage: ./host-serve.sh [options]

Options:
  -m, --model <id>      Inference model (default: $DEFAULT_MODEL)
  -d, --draft <id>      Speculative decoding draft model (default: $DEFAULT_DRAFT, use "none" to disable)
  -p, --port <port>     Inference server port (default: $DEFAULT_PORT)
  -h, --host <host>     Network bind address (default: $DEFAULT_HOST / all interfaces)
  --kv-bits <n>         KV cache quantization bits (default: $DEFAULT_KV_BITS; "none" to disable)
  --max-kv <n>          Max prompt+max_tokens budget (default: $DEFAULT_MAX_KV; 0 to disable)
  --max-seqs <n>        Max concurrent sequences (default: $DEFAULT_MAX_SEQS; 0 to disable)
  --apc-blocks <n>      APC retained prefix pool blocks x16 tokens (default: $DEFAULT_APC_BLOCKS)
  --no-apc              Disable Automatic Prefix Caching (APC)
  --bg                  Run in background (via nohup)
  --help                Show this message

Environment Variables:
  APC_ENABLED=1         Enables MLX Automatic Prefix Caching (set by default)
  APC_NUM_BLOCKS=512    APC retained prefix cache pool size (blocks x16 tokens)
  IOGPU_LIMIT           Optional sysctl wired memory limit check (e.g., 115000 for 128GB Macs)
EOF
  exit 0
}

# Parse options
MODEL="$DEFAULT_MODEL"
DRAFT="$DEFAULT_DRAFT"
PORT="$DEFAULT_PORT"
BIND_HOST="$DEFAULT_HOST"
KV_BITS="$DEFAULT_KV_BITS"
MAX_KV="$DEFAULT_MAX_KV"
MAX_SEQS="$DEFAULT_MAX_SEQS"
APC_BLOCKS="$DEFAULT_APC_BLOCKS"
USE_APC=1
BACKGROUND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--model)     MODEL="$2"; shift 2 ;;
    -d|--draft)     DRAFT="$2"; shift 2 ;;
    -p|--port)      PORT="$2"; shift 2 ;;
    -h|--host)      BIND_HOST="$2"; shift 2 ;;
    --kv-bits)      KV_BITS="$2"; [[ "$KV_BITS" == "none" || "$KV_BITS" == "0" ]] && KV_BITS=""; shift 2 ;;
    --max-kv)       MAX_KV="$2"; [[ "$MAX_KV" == "none" || "$MAX_KV" == "0" ]] && MAX_KV=""; shift 2 ;;
    --max-seqs)     MAX_SEQS="$2"; [[ "$MAX_SEQS" == "none" || "$MAX_SEQS" == "0" ]] && MAX_SEQS=""; shift 2 ;;
    --apc-blocks)   APC_BLOCKS="$2"; [[ "$APC_BLOCKS" == "none" || "$APC_BLOCKS" == "0" ]] && APC_BLOCKS=""; shift 2 ;;
    --no-apc)       USE_APC=0; shift ;;
    --bg)           BACKGROUND=1; shift ;;
    --help)         usage ;;
    *)              echo "Unknown option: $1"; usage ;;
  esac
done

# Check dependencies
if ! command -v mlx_vlm.server >/dev/null 2>&1; then
  echo "Error: 'mlx_vlm.server' is not installed or not in PATH."
  echo "To install via uv:  uv tool install mlx-vlm --with jinja2"
  echo "Or via pip:        pip install mlx-vlm"
  exit 1
fi

# Set APC Prefix Cache environment variable
if (( USE_APC )); then
  export APC_ENABLED=1
  [[ -n "$APC_BLOCKS" ]] && export APC_NUM_BLOCKS="$APC_BLOCKS"
  echo ">>> Automatic Prefix Caching (APC) is ENABLED."
else
  export APC_ENABLED=0
  echo ">>> Automatic Prefix Caching (APC) is DISABLED."
fi

# Build arguments array
ARGS=(
  --model "$MODEL"
  --host "$BIND_HOST"
  --port "$PORT"
)

if [[ "$DRAFT" != "none" && -n "$DRAFT" ]]; then
  ARGS+=(--draft-model "$DRAFT")
  echo ">>> Speculative Decoding enabled using draft model: $DRAFT"
else
  echo ">>> Speculative Decoding is disabled."
fi

# KV cache memory governor
[[ -n "$KV_BITS" ]]  && ARGS+=(--kv-bits "$KV_BITS")
[[ -n "$MAX_KV" ]]   && ARGS+=(--max-kv-size "$MAX_KV")
[[ -n "$MAX_SEQS" ]] && ARGS+=(--max-num-seqs "$MAX_SEQS")

echo ">>> Starting inference server..."
echo "    Model: $MODEL"
echo "    Bind:  http://$BIND_HOST:$PORT"
echo "    KV gov: kv-bits=${KV_BITS:-off} max-kv=${MAX_KV:-off} seqs=${MAX_SEQS:-off} apc-blocks=${APC_BLOCKS:-off}"

if (( BACKGROUND )); then
  LOG_FILE="mlx-server-$PORT.log"
  nohup mlx_vlm.server "${ARGS[@]}" > "$LOG_FILE" 2>&1 & disown
  echo ">>> Server running in background. Logs redirected to $LOG_FILE"
  echo "    PID: $!"
else
  exec mlx_vlm.server "${ARGS[@]}"
fi
