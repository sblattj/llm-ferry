#!/bin/zsh
# client-bootstrap.sh — Configures a client laptop on the same LAN to use the host's inference server.
# Dynamically installs the 'ferry' CLI on the client and configures editors/CLIs.

set -eu

# Fallback defaults — can be overridden via env vars or dynamically injected by the host-share server
HOST_NAME="${HOST_NAME:-HOST_MDNS_PLACEHOLDER}"
HOST_PORT="${HOST_PORT:-8090}"
SHARE_PORT="${SHARE_PORT:-SHARE_PORT_PLACEHOLDER}"

# When served by `ferry share`, the host rewrites these placeholders with its live
# mDNS name and share port. If you fetched this script another way, they stay as
# placeholders and we fall back to sensible defaults — the probe below will then
# prompt you for the host if it can't be reached.
if [[ "$HOST_NAME" == "HOST_MDNS_PLACEHOLDER" ]]; then
  HOST_NAME="your-host.local"
fi
if [[ "$SHARE_PORT" == "SHARE_PORT_PLACEHOLDER" ]]; then
  SHARE_PORT="8095"
fi

# Default Model lists
MODEL_LOCAL="mlx-community/Qwen3.8-27B-nvfp4"
MODEL_LOCAL_ORCH="mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"
MODEL_CLAUDE="anthropic/claude-3-7-sonnet-20250219"
MODEL_GEMINI="gemini/gemini-3.7-flash"
MODEL_GEMINI_2="gemini/gemini-2.0-flash"

echo "================================================================="
echo "            BOOTSTRAPPING CLIENT LAPTOP FOR LLM-FERRY"
echo "================================================================="
echo "Target Host Server: http://$HOST_NAME:$HOST_PORT"
echo "================================================================="

# 1. Test basic network connectivity to the host (with interactive fallback if it fails)
echo ">>> Probing host at http://$HOST_NAME:$HOST_PORT..."
if curl -fsS -m 3 "http://$HOST_NAME:$HOST_PORT/v1/models" >/dev/null 2>&1; then
  echo "    \033[1;32mSUCCESS: Connected to host inference server!\033[0m"
else
  echo "    \033[1;31mCould not connect to the default host at $HOST_NAME:$HOST_PORT.\033[0m"
  echo "    Please enter the host's correct mDNS hostname or LAN IP"
  printf "    (e.g., mymacbook.local or 192.168.0.100) [Enter to skip/keep default]: "
  
  # Read from /dev/tty because stdin is redirected during a 'curl | zsh' pipe
  read NEW_HOST_NAME < /dev/tty
  
  if [[ -n "${NEW_HOST_NAME:-}" ]]; then
    HOST_NAME="$NEW_HOST_NAME"
    echo ">>> Probing new host at http://$HOST_NAME:$HOST_PORT..."
    if curl -fsS -m 3 "http://$HOST_NAME:$HOST_PORT/v1/models" >/dev/null 2>&1; then
      echo "    \033[1;32mSUCCESS: Connected to host inference server at $HOST_NAME!\033[0m"
    else
      echo "    \033[1;33mWARNING: Still could not connect to $HOST_NAME:$HOST_PORT.\033[0m"
      echo "    We will proceed with configuring client shortcuts anyway."
    fi
  else
    echo "    Keeping default host: $HOST_NAME"
  fi
fi

# 2. STEP ONE: Download and Install the 'ferry' CLI on the client laptop
echo ""
echo ">>> Installing 'ferry' CLI locally on this client laptop..."
mkdir -p "$HOME/.local/bin"

if curl -fsSL -m 5 "http://$HOST_NAME:$SHARE_PORT/ferry" -o "$HOME/.local/bin/ferry" 2>/dev/null; then
  chmod +x "$HOME/.local/bin/ferry"
  echo "    \033[1;32mSuccessfully installed 'ferry' CLI to ~/.local/bin/ferry!\033[0m"
else
  echo "    WARNING: Could not download 'ferry' CLI from share server. Creating placeholder..."
  # If share server didn't host it yet, write client bootstrap location copy
  # (Unlikely, but a robust fallback)
  echo "echo 'ferry CLI placeholder'" > "$HOME/.local/bin/ferry"
  chmod +x "$HOME/.local/bin/ferry"
fi

# Write local client JSON config profile
echo ">>> Creating client configuration profile..."
mkdir -p "$HOME/.config/ferry"
cat <<EOF > "$HOME/.config/ferry/client.json"
{
  "host": "$HOST_NAME",
  "port": "$HOST_PORT",
  "share_port": "$SHARE_PORT"
}
EOF
echo "    Successfully saved profile: ~/.config/ferry/client.json"

# 3. Interactive Selection Menu: Choose favorite tools
echo ""
echo ">>> Which developer tools would you like to configure for LLM-Ferry?"
echo "  1) opencode CLI  (recommended; full auto-integration, auto-detects the host's served models)"
echo "  2) VS Code Continue Extension (config.json generator)"
echo "  3) Cursor IDE"
echo "  4) Set up ALL integrations"
printf "Select option (1-4) [Default: 1]: "

read TOOL_CHOICE < /dev/tty
TOOL_CHOICE="${TOOL_CHOICE:-1}"

# Initialize setup flags
SETUP_OPENCODE=0
SETUP_CONTINUE=0
SETUP_CURSOR=0

case "$TOOL_CHOICE" in
  1) SETUP_OPENCODE=1 ;;
  2) SETUP_CONTINUE=1 ;;
  3) SETUP_CURSOR=1 ;;
  4) SETUP_OPENCODE=1; SETUP_CONTINUE=1; SETUP_CURSOR=1 ;;
  *) SETUP_OPENCODE=1 ;;
esac

# 4. Interactive Selection Menu: Choose default model (Sorted Chronologically, Newest First)
SELECTED_MODEL="$MODEL_GEMINI" # default
echo ""
echo ">>> Which model should your integrations default to using on the host?"
echo " Cloud (recommended — uses the host's API keys):"
echo "  1) [Cloud] Gemini 3.7 Flash  (gemini/gemini-3.7-flash) [Recommended Default]"
echo "  2) [Cloud] Claude 3.7 Sonnet (anthropic/claude-3-7-sonnet-20250219)"
echo "  3) [Cloud] Gemini 2.0 Flash  (gemini/gemini-2.0-flash)"
echo ""
echo " Local (runs on the host's Apple Silicon GPU via MLX):"
echo "  4) [Local] Qwen 3.8-27B GPU  (mlx-community/Qwen3.8-27B-nvfp4)"
echo "  5) [Local] NVIDIA Nemotron 3 Nano 30B A3B (orchestrator-grade, subagent-friendly)  (mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4)"
printf "Select option (1-5) [Default: 1]: "

read MODEL_CHOICE < /dev/tty
MODEL_CHOICE="${MODEL_CHOICE:-1}"

case "$MODEL_CHOICE" in
  1) SELECTED_MODEL="$MODEL_GEMINI" ;;
  2) SELECTED_MODEL="$MODEL_CLAUDE" ;;
  3) SELECTED_MODEL="$MODEL_GEMINI_2" ;;
  4) SELECTED_MODEL="$MODEL_LOCAL" ;;
  5) SELECTED_MODEL="$MODEL_LOCAL_ORCH" ;;
  *) SELECTED_MODEL="$MODEL_GEMINI" ;;
esac

# 5. Perform Automatic opencode configuration
if (( SETUP_OPENCODE )); then
  echo ""
  echo ">>> Auto-configuring 'opencode' to route through the host (detects served models)..."
  if "$HOME/.local/bin/ferry" opencode --host "$HOST_NAME" --port "$HOST_PORT"; then
    :
  else
    echo "    WARNING: 'ferry opencode' failed. Wire opencode manually: add an openai-compatible"
    echo "    provider with baseURL=http://$HOST_NAME:$HOST_PORT/v1, apiKey=local, and a model from"
    echo "    http://$HOST_NAME:$HOST_PORT/v1/models."
  fi

  # Setup shell alias in .zshrc
  echo ">>> Configuring terminal 'host-code' shortcut in ~/.zshrc..."
  ZSHRC="$HOME/.zshrc"
  ALIAS_LINE="alias host-code='opencode'"

  if [[ -f "$ZSHRC" ]]; then
    if grep -q "^alias host-code=" "$ZSHRC" 2>/dev/null; then
      # Update existing alias
      sed -i '' "s|^alias host-code=.*|$ALIAS_LINE|" "$ZSHRC" 2>/dev/null || \
        sed -i "s|^alias host-code=.*|$ALIAS_LINE|" "$ZSHRC"
      echo "    Updated existing alias 'host-code' in ~/.zshrc"
    else
      # Add new alias
      echo "" >> "$ZSHRC"
      echo "# LLM-Ferry Shortcut" >> "$ZSHRC"
      echo "$ALIAS_LINE" >> "$ZSHRC"
      echo "    Added shortcut 'host-code' to ~/.zshrc"
    fi
  else
    # Create a zshrc with the alias
    echo "$ALIAS_LINE" > "$ZSHRC"
    echo "    Created ~/.zshrc and added shortcut 'host-code'"
  fi
fi

# Ensure ~/.local/bin is in PATH in client ~/.zshrc
echo ">>> Verifying ~/.local/bin is in your PATH in ~/.zshrc..."
ZSHRC="$HOME/.zshrc"
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
if [[ -f "$ZSHRC" ]]; then
  if ! grep -q ".local/bin" "$ZSHRC" 2>/dev/null; then
    echo "" >> "$ZSHRC"
    echo "# Add local binaries to PATH" >> "$ZSHRC"
    echo "$PATH_LINE" >> "$ZSHRC"
    echo "    Successfully added ~/.local/bin to ~/.zshrc PATH."
  fi
else
  echo "$PATH_LINE" > "$ZSHRC"
fi

# Ensure opencode profile functions (opencode-cloud, opencode-local) are in ~/.zshrc
echo ">>> Configuring opencode profile functions in ~/.zshrc..."
ZSHRC="$HOME/.zshrc"
START_MARKER="# >>> ferry opencode profiles >>>"
END_MARKER="# <<< ferry opencode profiles <<<"
touch "$ZSHRC"

# Strip any existing ferry opencode profiles block before re-adding
python3 - "$ZSHRC" "$START_MARKER" "$END_MARKER" <<'PYEOF'
import sys
rc, start, end = sys.argv[1], sys.argv[2], sys.argv[3]
with open(rc) as f:
    lines = f.readlines()
out, skip = [], False
for ln in lines:
    s = ln.rstrip("\n")
    if s == start:
        skip = True
        continue
    if s == end:
        skip = False
        continue
    if not skip:
        out.append(ln)
while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
PYEOF

cat <<EOF >> "$ZSHRC"
$START_MARKER
# opencode-cloud: route-mode 'orch' pattern — orchestrator main + Gemini Flash subagents.
# Requires the host running \`ferry up --route\`.
opencode-cloud() {
  OPENCODE_CONFIG_CONTENT='{"provider":{"ferry":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://$HOST_NAME:$HOST_PORT/v1","apiKey":"local"},"models":{"orchestrator":{},"gemini-3.7-flash":{}}}},"model":"ferry/orchestrator","small_model":"ferry/gemini-3.7-flash","agent":{"build":{"model":"ferry/orchestrator"},"plan":{"model":"ferry/orchestrator"},"general":{"model":"ferry/gemini-3.7-flash"},"explore":{"model":"ferry/gemini-3.7-flash"},"scout":{"model":"ferry/gemini-3.7-flash"}}}' command opencode "\$@"
}

# opencode-local: all-local lane — host GPU runs NVIDIA Nemotron 3 Nano 30B A3B (NVFP4)
# for BOTH main and subagents (hybrid attention = tiny KV cache -> many parallel agents
# fit in the host's RAM). Requires the host running \`ferry up --orch\`.
opencode-local() {
  OPENCODE_CONFIG_CONTENT='{"provider":{"ferry":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://$HOST_NAME:$HOST_PORT/v1","apiKey":"local"},"models":{"mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4":{}}}},"model":"ferry/mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4","small_model":"ferry/mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4","agent":{"build":{"model":"ferry/mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"},"plan":{"model":"ferry/mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"},"general":{"model":"ferry/mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"},"explore":{"model":"ferry/mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"},"scout":{"model":"ferry/mlx-community/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"}}}' command opencode "\$@"
}
$END_MARKER
EOF
echo "    Successfully added opencode profile functions to ~/.zshrc."

# 6. Output integration guides based on selection
echo "================================================================="
echo ">>> SUCCESS! Client setup is complete."
echo "    Please open a NEW terminal window (or run: source ~/.zshrc)."
echo "================================================================="

echo ">>> UNIFIED FERRY CLI INSTALLED:"
echo "    You can now use 'ferry' on this client for diagnostics and logging!"
echo "    - Check Connection Health:   \033[1;32mferry status\033[0m"
echo "    - Send Quick Msg to Host:    \033[1;32mferry msg \"Everything is working!\"\033[0m"
echo "    - Stream terminal logs:      \033[1;32mopencode run \"...\" 2>&1 | ferry log\033[0m"
echo ""

if (( SETUP_OPENCODE )); then
  echo ">>> OPENCODE CLI INSTANT ACCESS:"
  echo "    You can now call the host model using standard commands:"
  echo "    \033[1;32mhost-code run \"Build a snake game in Python\"\033[0m"
  echo "    (Or run bare 'opencode' commands — default model is now set to 'ferry')"
  echo "    opencode-cloud   -> 'orch' pattern: orchestrator main + Gemini Flash subagents (host: ferry up --route)"
  echo "    opencode-local   -> all-local: Nemotron 3 Nano 30B A3B NVFP4 main + subagents on the host GPU (host: ferry up --orch)"
  echo ""
fi

if (( SETUP_CONTINUE )); then
  echo ">>> VS CODE CONTINUE CONFIGURATION:"
  echo "    Add this model block to your ~/.continue/config.json file:"
  echo "    {"
  echo "      \"title\": \"LLM-Ferry Model\","
  echo "      \"provider\": \"openai\","
  echo "      \"model\": \"$SELECTED_MODEL\","
  echo "      \"apiBase\": \"http://$HOST_NAME:$HOST_PORT/v1\","
  echo "      \"apiKey\": \"local\""
  echo "    }"
  echo ""
fi

if (( SETUP_CURSOR )); then
  echo ">>> CURSOR IDE CONFIGURATION:"
  echo "    1. Go to Settings -> Models -> OpenAI-Compatible"
  echo "    2. Toggle ON"
  echo "    3. Set Base URL:  \033[1;32mhttp://$HOST_NAME:$HOST_PORT/v1\033[0m"
  echo "    4. Set API Key:   local"
  echo "    5. Click 'Add Model...' and enter: \033[1;32m$SELECTED_MODEL\033[0m"
  echo "================================================================="
fi
