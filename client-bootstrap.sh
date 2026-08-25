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

# The host serves LANES, not raw model ids: a lane name is a stable role that the
# host can re-point at a different model without any client ever being edited.
LANE_ORCH="orch"              # cloud: the big driving model + its fallback chain
LANE_FLASH="flash"            # cloud: cheap high-volume worker pool
LANE_LOCAL_ORCH="local-orch"  # host GPU: the "smart" local model
LANE_LOCAL_SUB="local-sub"    # host GPU: the cheap local fan-out model

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
SELECTED_MODEL="$LANE_ORCH" # default
echo ""
echo ">>> Which LANE should your integrations default to on the host?"
echo "    (The host serves all of these at once on one endpoint. You are picking a"
echo "     default, not a restriction — any tool can name another lane per request.)"
echo ""
echo " Cloud (uses the host's API keys; nothing runs on the host GPU):"
echo "  1) orch        big driving model + strict fallback chain [Recommended Default]"
echo "  2) flash       cheap high-volume worker pool"
echo ""
echo " Local (runs on the host's Apple Silicon GPU via MLX):"
echo "  3) local-orch  the host GPU's smart model"
echo "  4) local-sub   the host GPU's cheap fan-out model"
printf "Select option (1-4) [Default: 1]: "

read MODEL_CHOICE < /dev/tty
MODEL_CHOICE="${MODEL_CHOICE:-1}"

case "$MODEL_CHOICE" in
  1) SELECTED_MODEL="$LANE_ORCH" ;;
  2) SELECTED_MODEL="$LANE_FLASH" ;;
  3) SELECTED_MODEL="$LANE_LOCAL_ORCH" ;;
  4) SELECTED_MODEL="$LANE_LOCAL_SUB" ;;
  *) SELECTED_MODEL="$LANE_ORCH" ;;
esac

# 5. Perform Automatic opencode configuration
if (( SETUP_OPENCODE )); then
  echo ""
  echo ">>> Auto-configuring 'opencode' to route through the host (detects served models)..."
  # Honour the lane picked in the menu above. `ferry opencode` auto-detects and
  # defaults to the CLOUD pair, so a local pick has to be passed through
  # explicitly — otherwise choosing "local-orch" silently wires orch + flash.
  OC_LANE_FLAG=""
  case "$SELECTED_MODEL" in
    "$LANE_LOCAL_ORCH"|"$LANE_LOCAL_SUB") OC_LANE_FLAG="--local" ;;
  esac
  if "$HOME/.local/bin/ferry" opencode --host "$HOST_NAME" --port "$HOST_PORT" $OC_LANE_FLAG; then
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
# opencode-cloud: the CLOUD pair — `orch` drives (build/plan), `flash` runs the
# fan-out (general/explore/scout). Nothing touches the host GPU.
opencode-cloud() {
  OPENCODE_CONFIG_CONTENT='{"provider":{"ferry":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://$HOST_NAME:$HOST_PORT/v1","apiKey":"local"},"models":{"orch":{},"flash":{}}}},"model":"ferry/orch","small_model":"ferry/flash","agent":{"build":{"model":"ferry/orch"},"plan":{"model":"ferry/orch"},"general":{"model":"ferry/flash"},"explore":{"model":"ferry/flash"},"scout":{"model":"ferry/flash"}}}' command opencode "\$@"
}

# opencode-local: the GPU pair — both lanes on the host's Apple Silicon, nothing
# leaving the machine. `local-orch` drives (build/plan) and `local-sub` runs the
# fan-out (general/explore/scout): the subagent lane is a hybrid-attention MoE
# whose tiny KV cache lets many parallel agents fit in the host's RAM, so the
# expensive lane is not spent on cheap work.
opencode-local() {
  OPENCODE_CONFIG_CONTENT='{"provider":{"ferry":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://$HOST_NAME:$HOST_PORT/v1","apiKey":"local"},"models":{"local-orch":{},"local-sub":{}}}},"model":"ferry/local-orch","small_model":"ferry/local-sub","agent":{"build":{"model":"ferry/local-orch"},"plan":{"model":"ferry/local-orch"},"general":{"model":"ferry/local-sub"},"explore":{"model":"ferry/local-sub"},"scout":{"model":"ferry/local-sub"}}}' command opencode "\$@"
}

$END_MARKER
EOF
echo "    Successfully added opencode profile functions to ~/.zshrc."

# Install the local-lane opencode guardrails: /fan-out command + spawning-subagents
# skill (global dirs — opencode picks them up automatically). Keep the heredoc
# bodies in sync with opencode/command/fan-out.md and
# opencode/skills/spawning-subagents/SKILL.md in the llm-ferry repo.
# Context: Nemotron (the opencode-local model) produces malformed task-tool
# calls (hallucinated task_id, missing description) that are rejected before
# the tool runs, causing silent retry doom loops. The recipe injected as the
# USER message (what /fan-out does) is the empirically working fix.
if command -v opencode >/dev/null 2>&1; then
  echo ">>> Installing opencode local-lane guardrails (/fan-out + spawning-subagents skill)..."
  mkdir -p "$HOME/.config/opencode/command" "$HOME/.config/opencode/skills/spawning-subagents"

  cat > "$HOME/.config/opencode/command/fan-out.md" <<'FANOUT'
---
description: Fan a build task out to up to 3 parallel subagents with the safe task-tool recipe (for local lanes, whose raw task calls get rejected).
---

Task: $ARGUMENTS

Delegation rules, follow EXACTLY:

- First split this task into up to THREE self-contained component briefs. If the task is small, fewer is fine; if it cannot be split, do it yourself and skip delegation.
- Call the task tool once per brief. You may launch them in parallel.
- Each task call MUST have exactly these three fields and nothing else:
  - description: a short 3-5 word label
  - subagent_type: the string "general"
  - prompt: the complete brief
- Do NOT pass task_id or any other field. Do NOT nest delegation: a brief must never mention subagents, delegating, or orchestrating — it describes concrete work and what to return.
- If a tool call errors, read the error, fix the named field, and retry that call ONCE. Never resend an identical failing call.

After the subagents return, integrate their results into the final artifact yourself, write it to disk, and verify it exists and is complete.
FANOUT

  cat > "$HOME/.config/opencode/skills/spawning-subagents/SKILL.md" <<'SKILLMD'
---
name: spawning-subagents
description: Use when calling the task tool from a LOCAL lane (opencode-local, or any ferry local-* model) to delegate work to subagents. Prevents malformed task calls (hallucinated task_id, missing description, nested delegation) that silently stall local agent sessions; covers the exact three-field call recipe and retry-once rule.
---

# Spawning subagents on the local lanes

Small local models driving a fan-out frequently produce malformed `task` tool
calls - hallucinated `task_id` fields, missing `description`, or nested
delegation - which the harness rejects before the tool ever runs. The failure
looks like a silent stall: no tool output, the model silently retrying the same
broken call every turn.

This applies to whichever local model is DRIVING. On ferry that is the
`local-orch` lane; `local-sub` is the lane the subagents themselves run on and
does not issue `task` calls.

Follow these rules EVERY time you call the `task` tool:

1. The call MUST have exactly these three fields - nothing else:
   - `description`: a short 3-5 word label for the subtask
   - `subagent_type`: the string "general"
   - `prompt`: the complete, self-contained brief
2. NEVER pass `task_id`, `command`, `model`, or any other field. `task_id` is
   reserved for resuming an existing session and must start with "ses" - if
   you invent one, the call fails.
3. NEVER write a brief that tells the subagent to delegate further. One level
   of fan-out only.
4. If a tool call returns an error, read the error text, fix the named field,
   and retry that call ONCE with corrected arguments. Never resend an
   identical failing call.
5. Do the integration work yourself: subagents return code/modules; the main
   agent writes files and verifies them on disk.

## Operational notes (measured 2026-08-25)

Measured with NVIDIA Nemotron 3 Nano 30B A3B driving the session. It has since
moved to the `local-sub` (subagent) lane, so it is no longer the default driver
- but the failure mode is a property of small local models issuing `task`
calls, not of that one model, so the recipe still applies to whatever drives.

- The recipe works when it sits in the USER message (end of context). Putting
  it in system instructions or relying on the model to load this skill made
  failures WORSE - use the `/fan-out` command, which injects the recipe as the
  user message, rather than hoping the model finds it.
- Bare tool calls (read/write/bash) are reliable; only the `task` schema is flaky.
- Doom-loop signature for headless runs: repeated server requests with
  IDENTICAL generated-token counts and finish_reason=tool_calls every turn
  (e.g. 22 identical 38-token calls). Kill on 3+ identical consecutive.
  Server-side request logs are ground truth; opencode's `--format json`
  stream lags and can miss in-flight loops entirely.
SKILLMD

  echo "    Installed: ~/.config/opencode/command/fan-out.md"
  echo "              ~/.config/opencode/skills/spawning-subagents/SKILL.md"
fi

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
  echo "    opencode-cloud   -> cloud pair: orch drives, flash fans out"
  echo "    opencode-local   -> GPU pair:   local-orch drives, local-sub fans out"
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
