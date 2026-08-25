#!/bin/zsh
# host-bootstrap.sh — Bootstraps a new Mac to become a high-performance LAN inference host.
# Sets up uv, installs mlx-vlm, downloads default models, and prepares the workspace.

set -eu

echo "================================================================="
echo "            BOOTSTRAPPING LLM-FERRY HOST MAC"
echo "================================================================="

# 1. Ensure Python 3 is available
if ! command -v python3 >/dev/null 2>&1; then
  echo ">>> Python 3 is required. Please install it (e.g., via Xcode command line tools or python.org)."
  exit 1
fi

# 2. Install UV (Ultra-fast python and tool installer) if missing
if ! command -v uv >/dev/null 2>&1; then
  echo ">>> 'uv' is not installed. Installing 'uv' via official installer..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Reload path to pick up uv
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    echo ">>> Error: Could not locate 'uv' in PATH after installation."
    echo "    Please restart your terminal and run this bootstrap script again."
    exit 1
  fi
else
  echo ">>> 'uv' is already installed."
fi

# 3. Install mlx-vlm tool
echo ">>> Installing/Updating 'mlx-vlm' tool via uv..."
uv tool install mlx-vlm --with jinja2 --force

# 4. Install litellm for cloud proxying
# Pin litellm + fastapi as a matched, tested set. litellm 1.97.0's proxy imports
# fastapi.dependencies.utils.get_flat_dependant, which FastAPI removed after
# 0.136.3. Left unpinned, uv resolves fastapi 0.141+ and the proxy dies at
# startup with a misleading "ModuleNotFoundError: No module named 'proxy_server'".
# The [proxy] extra is what pulls fastapi/uvicorn in the first place.
echo ">>> Installing/Updating 'litellm' via uv..."
uv tool install 'litellm[proxy]==1.97.0' --with 'fastapi==0.136.3' --force

# 5. Download high-performance default models (using hf tool if available, else huggingface-cli)
# Note: the LAN "ferry" transfer commands (offer/pull/get/send/receive) need no extra installs —
# they use python3 stdlib, curl, tar, and nc, all present on macOS. Only the EXPERIMENTAL
# `ferry pull --transport hf` (fetch through `ferry serve-hf`) uses the `hf`/`huggingface-cli`
# tool, which uv installs alongside mlx-vlm above.
echo ">>> Downloading default high-performance models (~16.6GB total)..."

download_model() {
  local model_id=$1
  if command -v hf >/dev/null 2>&1; then
    echo "    [hf] Downloading $model_id..."
    hf download "$model_id"
  else
    echo "    [huggingface-cli] Downloading $model_id..."
    uv run huggingface-cli download "$model_id"
  fi
}

echo ">>> Fetching Model 1: Qwen 3.8-27B (nvfp4 quantized)"
download_model "mlx-community/Qwen3.8-27B-nvfp4"

echo ">>> Fetching Model 2: Qwen 3.8-27B MTP Speculative Drafter (8-bit)"
download_model "mlx-community/Qwen3.8-27B-MTP-8bit"

echo ">>> Fetching Model 3: NVIDIA Nemotron 3 Nano 30B A3B (local orchestrator, NVFP4)"
download_model "mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"

# 6. Check and recommend VRAM adjustments
TOTAL_MEM_GB=$(sysctl -n hw.memsize 2>/dev/null | awk '{print int($1/1024/1024/1024)}')
echo ">>> System unified memory detected: ${TOTAL_MEM_GB}GB"
if [[ -n "$TOTAL_MEM_GB" && "$TOTAL_MEM_GB" -ge 64 ]]; then
  echo "    [RECOMMENDED] For Macs with >=64GB RAM, allocate more wired GPU limit:"
  echo "    sudo sysctl iogpu.wired_limit_mb=115000"
fi

# 7. Make all host scripts executable and link globally to ~/.local/bin/ferry
echo ">>> Making script files executable..."
chmod +x host-serve.sh status.sh host-share.sh ferry 2>/dev/null || true

echo ">>> Creating global symlink in ~/.local/bin/ferry..."
mkdir -p "$HOME/.local/bin"
ln -sfn "${0:A:h}/ferry" "$HOME/.local/bin/ferry"

echo "================================================================="
echo ">>> SUCCESS! Host is bootstrapped and the 'ferry' CLI is globally linked."
echo "    You can now run: ferry status  (or ferry --help) from any folder!"
echo "================================================================="
