
# ----------------- COMMANDS -----------------

# _ferry_patch_nemotron_batching — make the `local-sub` lane actually answer.
#
# mlx-vlm's continuous-batching engine (generate/ar.py) passes BOTH `input_ids`
# and `inputs_embeds` on every request, but the `nemotron_h` backbone requires
# exactly one and raises `ValueError: Provide exactly one of inputs or
# inputs_embeds`. Unpatched, EVERY request to the Nemotron lane fails — the lane
# starts, reports healthy, and 500s on first use.
#
# The fix is two lines in LanguageModel.__call__: prefer inputs_embeds when it is
# present. This runs on every `ferry install` because `uv tool install --force`
# rewrites site-packages and discards the previous patch.
#
# Idempotent and fail-soft by design: it no-ops when already patched, no-ops when
# the upstream shape changes (i.e. when the bug is fixed), and never fails the
# install — a missing patch costs one lane, a broken install costs all of them.
_ferry_patch_nemotron_batching() {
  echo ">>> Patching mlx-vlm nemotron_h for continuous batching (local-sub lane)..."
  python3 - <<'PYEOF' || echo "    (patch step skipped; local-sub may 500 on every request)"
import glob, os, sys

CANDIDATES = []
# The uv-managed tool venv is the path `ferry install` creates.
CANDIDATES += glob.glob(os.path.expanduser(
    "~/.local/share/uv/tools/mlx-vlm/lib/python*/site-packages/mlx_vlm/models/nemotron_h/language.py"))
# Fall back to any importable mlx_vlm (pip/conda installs).
try:
    import mlx_vlm  # noqa
    CANDIDATES.append(os.path.join(os.path.dirname(mlx_vlm.__file__),
                                   "models", "nemotron_h", "language.py"))
except Exception:
    pass

path = next((c for c in CANDIDATES if os.path.exists(c)), None)
if not path:
    print("    nemotron_h/language.py not found - nothing to patch.")
    sys.exit(0)

src = open(path).read()
ANCHOR = "        out = self.backbone(inputs, cache=cache, inputs_embeds=inputs_embeds)"
GUARD = "        if inputs_embeds is not None:\n            inputs = None\n"

if GUARD in src:
    print(f"    Already patched: {path}")
    sys.exit(0)
if ANCHOR not in src:
    # Upstream changed this call site - most likely the bug is fixed. Do not
    # guess at a new insertion point; leave the file alone.
    print(f"    Upstream shape changed (anchor absent) - leaving {path} untouched.")
    sys.exit(0)

patched = src.replace(ANCHOR,
    "        # Continuous batching (generate/ar.py) always passes BOTH input_ids and\n"
    "        # inputs_embeds (it runs on embeddings); the backbone requires exactly\n"
    "        # one, so defer to inputs_embeds whenever it is present.\n"
    + GUARD + ANCHOR, 1)
open(path, "w").write(patched)
print(f"    Patched: {path}")
PYEOF
}

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
    echo ">>> Fetching Model 1: Qwen 3.8-27B nvfp4 (the local-orch lane)"
    download_model "$LOCAL_MODEL"
    echo ">>> Fetching Model 2: Qwen 3.8-27B MTP speculative drafter, 8-bit (local-orch)"
    download_model "$LOCAL_DRAFT"
    echo ">>> Fetching Model 3: NVIDIA Nemotron 3 Nano 30B A3B NVFP4 (the local-sub lane)"
    download_model "$LOCAL_MODEL_SUB"

    # The local-sub lane is unusable without this patch — see the function's
    # comment. It MUST run after `uv tool install mlx-vlm --force` above, which
    # replaces site-packages and therefore discards any previous patch.
    _ferry_patch_nemotron_batching
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

  # The local-lane opencode guardrails. Clients get these from
  # client-bootstrap.sh; before this the host got them from nowhere at all.
  _ferry_install_opencode_guardrails

  # Same story one layer out: the opencode-cloud / opencode-local shell
  # wrappers were also client-bootstrap-only, so the host had the profile files
  # and no way to select between them.
  _ferry_install_host_wrappers

  # The claude-ferry / claude-ferry-local shell wrappers, same deal: clients
  # got them via client-bootstrap.sh, so the host needed its own copy pointed
  # at the local front door.
  if (( $+functions[_ferry_install_claude_wrappers] )); then
    _ferry_install_claude_wrappers 127.0.0.1 "$PORT"
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
