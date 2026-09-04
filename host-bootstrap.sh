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
# Pin litellm to the version the stack is verified against. 1.99.0 restamps the
# response `model` field to the client-requested lane name (the lane abstraction
# clients rely on) and uses fastapi's current get_flat_params API, so fastapi is
# deliberately NOT pinned (1.99.0 runs with 0.141+). Two non-default deps the
# proxy imports at runtime even in a no-DB setup:
#   prisma            — the auth-error handler imports it; without it an authed
#                       request 500s instead of 401ing. Required since v1.22.0.
#   prometheus_client — litellm's native /metrics (callbacks: ["prometheus"])
#                       crashes startup without it.
# The [proxy] extra is what pulls fastapi/uvicorn in the first place.
echo ">>> Installing/Updating 'litellm' via uv..."
uv tool install 'litellm[proxy]==1.99.0' --with 'prisma' --with 'prometheus_client' --force

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

echo ">>> Fetching Model 1: Qwen 3.8-27B nvfp4 (the local-orch lane)"
download_model "mlx-community/Qwen3.8-27B-nvfp4"

echo ">>> Fetching Model 2: Qwen 3.8-27B MTP speculative drafter, 8-bit (local-orch)"
download_model "mlx-community/Qwen3.8-27B-MTP-8bit"

echo ">>> Fetching Model 3: NVIDIA Nemotron 3 Nano 30B A3B NVFP4 (the local-sub lane)"
download_model "mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"

# mlx-vlm's continuous-batching engine passes BOTH input_ids and inputs_embeds,
# but the nemotron_h backbone requires exactly one — so WITHOUT this patch every
# request to the local-sub lane 500s ("Provide exactly one of inputs or
# inputs_embeds"). Must run after the mlx-vlm install above, which rewrites
# site-packages. Idempotent; no-ops once upstream fixes the call site.
# Keep in sync with _ferry_patch_nemotron_batching in lib/ferry-install.zsh.
echo ">>> Patching mlx-vlm nemotron_h for continuous batching (local-sub lane)..."
python3 - <<'PYEOF' || echo "    (patch step skipped; local-sub may 500 on every request)"
import glob, os, sys
cands = glob.glob(os.path.expanduser(
    "~/.local/share/uv/tools/mlx-vlm/lib/python*/site-packages/mlx_vlm/models/nemotron_h/language.py"))
try:
    import mlx_vlm  # noqa
    cands.append(os.path.join(os.path.dirname(mlx_vlm.__file__), "models", "nemotron_h", "language.py"))
except Exception:
    pass
path = next((c for c in cands if os.path.exists(c)), None)
if not path:
    print("    nemotron_h/language.py not found - nothing to patch."); sys.exit(0)
src = open(path).read()
ANCHOR = "        out = self.backbone(inputs, cache=cache, inputs_embeds=inputs_embeds)"
GUARD = "        if inputs_embeds is not None:\n            inputs = None\n"
if GUARD in src:
    print(f"    Already patched: {path}"); sys.exit(0)
if ANCHOR not in src:
    print(f"    Upstream shape changed (anchor absent) - leaving {path} untouched."); sys.exit(0)
open(path, "w").write(src.replace(ANCHOR,
    "        # Continuous batching always passes BOTH input_ids and inputs_embeds;\n"
    "        # the backbone requires exactly one, so defer to inputs_embeds.\n" + GUARD + ANCHOR, 1))
print(f"    Patched: {path}")
PYEOF

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
echo "    Note: the front door's master key is LITELLM_MASTER_KEY in ~/.config/ferry/secrets.env;"
echo "    host-reset.sh generates it on first run — read it from there to hand it to clients."
echo "================================================================="
