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

# Help Banner
usage() {
  cat <<EOF
Usage: ./host-serve.sh [options]

Options:
  -m, --model <id>      Inference model (default: $DEFAULT_MODEL)
  -d, --draft <id>      Speculative decoding draft model (default: $DEFAULT_DRAFT, use "none" to disable)
  -p, --port <port>     Inference server port (default: $DEFAULT_PORT)
  -h, --host <host>     Network bind address (default: $DEFAULT_HOST / all interfaces)
  --no-apc              Disable Automatic Prefix Caching (APC)
  --bg                  Run in background (via nohup)
  --help                Show this message

Environment Variables:
  APC_ENABLED=1         Enables MLX Automatic Prefix Caching (set by default)
  IOGPU_LIMIT           Optional sysctl wired memory limit check (e.g., 115000 for 128GB Macs)
EOF
  exit 0
}

# Parse options
MODEL="$DEFAULT_MODEL"
DRAFT="$DEFAULT_DRAFT"
PORT="$DEFAULT_PORT"
BIND_HOST="$DEFAULT_HOST"
USE_APC=1
BACKGROUND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--model)     MODEL="$2"; shift 2 ;;
    -d|--draft)     DRAFT="$2"; shift 2 ;;
    -p|--port)      PORT="$2"; shift 2 ;;
    -h|--host)      BIND_HOST="$2"; shift 2 ;;
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

echo ">>> Starting inference server..."
echo "    Model: $MODEL"
echo "    Bind:  http://$BIND_HOST:$PORT"

if (( BACKGROUND )); then
  LOG_FILE="mlx-server-$PORT.log"
  nohup mlx_vlm.server "${ARGS[@]}" > "$LOG_FILE" 2>&1 & disown
  echo ">>> Server running in background. Logs redirected to $LOG_FILE"
  echo "    PID: $!"
else
  exec mlx_vlm.server "${ARGS[@]}"
fi
