
# ----------------- COMMANDS -----------------

cmd_install() {
  echo "================================================================="
  echo "               PROVISIONING LLM-FERRY SYSTEM HOST"
  echo "================================================================="
  
  # Ensure uv is installed
  if ! command -v uv >/dev/null 2>&1; then
    echo ">>> Installing 'uv'..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  else
    echo ">>> 'uv' is already installed."
  fi

  # Install mlx-vlm (macOS / Apple Silicon only — this is the local GPU serving path).
  if (( IS_MAC )); then
    echo ">>> Installing 'mlx-vlm' via uv..."
    uv tool install mlx-vlm --with jinja2 --force
  fi

  # Install litellm for cloud proxying
  # Pin litellm + fastapi as a matched, tested set. litellm 1.97.0's proxy imports
  # fastapi.dependencies.utils.get_flat_dependant, which FastAPI removed after
  # 0.136.3. Left unpinned, uv resolves fastapi 0.141+ and the proxy dies at
  # startup with a misleading "ModuleNotFoundError: No module named 'proxy_server'".
  # The [proxy] extra is what pulls fastapi/uvicorn in the first place.
  # prometheus_client is what litellm's native /metrics needs: without it, setting
  # `callbacks: ["prometheus"]` in litellm.yaml crashes startup with
  # "ModuleNotFoundError: No module named 'prometheus_client'". Bundling it here lets
  # the observ Grafana stack chart per-model usage, failures, and fallback events.
  echo ">>> Installing 'litellm' via uv..."
  uv tool install 'litellm[proxy]==1.97.0' --with 'fastapi==0.136.3' --with 'prometheus_client' --force

  # Download default local models (macOS only — Linux has no local MLX serving).
  if (( IS_MAC )); then
    echo ">>> Downloading default local models..."
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
    download_model "$LOCAL_MODEL"
    download_model "$LOCAL_DRAFT"
  else
    echo ">>> Linux detected: skipping mlx-vlm and local model downloads."
    echo "    Local GPU serving is macOS / Apple Silicon only. On Linux, serve via"
    echo "    'ferry up --route' / '--cloud' / '--model <id>' against a cloud endpoint."
    if ! command -v zsh >/dev/null 2>&1; then
      echo ">>> NOTE: 'zsh' is not installed (ferry is a zsh script). Install it with:"
      echo "      sudo apt install zsh"
    fi
    echo ">>> Recommended: 'avahi-daemon' so '.local' mDNS names resolve across the LAN:"
    echo "      sudo apt install avahi-daemon"
    echo "    'iproute2' provides the 'ip' command used for LAN IP detection:"
    echo "      sudo apt install iproute2"
  fi

  # Link globally to ~/.local/bin/ferry (+ the ferry-dash companion)
  echo ">>> Creating global symlinks in ~/.local/bin (ferry, ferry-dash)..."
  mkdir -p "$HOME/.local/bin"
  ln -sfn "${0:A}" "$HOME/.local/bin/ferry"
  [[ -f "$APP_DIR/ferry-dash" ]] && ln -sfn "$APP_DIR/ferry-dash" "$HOME/.local/bin/ferry-dash"

  echo "================================================================="
  if (( IS_MAC )); then
    echo ">>> SUCCESS! 'ferry' CLI has been linked globally (macOS / Apple Silicon)."
    echo "    Local GPU serving, cloud proxy, route, dash, and LAN share are all available."
  else
    echo ">>> SUCCESS! 'ferry' CLI has been linked globally (Linux)."
    echo "    Available: cloud proxy, route, dash, client wiring, LAN share/transfer."
    echo "    Local GPU serving (--local) is macOS-only; use --route / --cloud / --model."
  fi
  echo "    Ensure ~/.local/bin is in your PATH. Run: ferry --help"
  echo "================================================================="
}
