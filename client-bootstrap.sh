#!/bin/zsh
# client-bootstrap.sh — Configures a client laptop on the same LAN to use the host's inference server.
# Installs the 'ferry' CLI on the client and wires opencode to the host. Fully
# non-interactive when the host is reachable (the only prompt is the host-name
# fallback when the initial probe fails).

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
# `orch`/`flash` are the cloud pair, `local-orch`/`local-sub` the host-GPU pair.

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
  
  # Read from /dev/tty because stdin is redirected during a 'curl | zsh' pipe.
  # `|| true` so an EOF/missing tty degrades to "keep default" instead of
  # aborting the whole script (set -e kills on a failed read).
  read NEW_HOST_NAME < /dev/tty || true
  
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

# 3. Automatic opencode configuration (the one supported integration; everything
# else can point an OpenAI-compatible client at http://HOST:8090/v1 by hand).
# No lane question: the host predetermines the models behind each lane, the
# `opencode-cloud` / `opencode-local` wrappers pick a pair per invocation, and
# bare `opencode` follows whichever wrapper ran last (cloud until then). So the
# persistent default written here is always the cloud pair.
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
touch "$ZSHRC"

# Strip any existing ferry opencode profiles block before re-adding, and remove
# LEGACY `alias opencode-cloud` / `alias opencode-local` / `alias opencode` lines
# from older hand-wired setups: an alias defined above a function definition
# makes zsh expand it inside `name() {` -> "defining function based on alias"
# -> "parse error near ()" on every future `source ~/.zshrc`.
python3 - "$ZSHRC" "# >>> ferry opencode profiles >>>" "# <<< ferry opencode profiles <<<" <<'PYEOF'
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
# legacy aliases collide with the function names below
def is_legacy_alias(l):
    t = l.lstrip()
    return (t.startswith("alias opencode-cloud=")
            or t.startswith("alias opencode-local=")
            or t.startswith("alias opencode="))
out = [l for l in out if not is_legacy_alias(l)]
while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
PYEOF

# QUOTED heredoc: the body is written VERBATIM (no $, backtick, or quote
# expansion). Host/port are spliced in afterwards via unique placeholders.
cat <<'EOF' >> "$ZSHRC"
# >>> ferry opencode profiles >>>
# Defensive: an alias with a function's name anywhere earlier in the file (or in
# the live shell) breaks the definitions below with "defining function based on
# alias". Kill them first.
unalias opencode opencode-cloud opencode-local 2>/dev/null

_FERRY_CFG_DIR="$HOME/.config/ferry"
_FERRY_LANE_FILE="$_FERRY_CFG_DIR/last-lane"
_FERRY_CFG_CLOUD='{"provider":{"ferry":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://__FERRY_HOST__:__FERRY_PORT__/v1","apiKey":"local"},"models":{"orch":{},"flash":{}}}},"model":"ferry/orch","small_model":"ferry/flash","agent":{"build":{"model":"ferry/orch"},"plan":{"model":"ferry/orch"},"general":{"model":"ferry/flash"},"explore":{"model":"ferry/flash"},"scout":{"model":"ferry/flash"}}}'
_FERRY_CFG_LOCAL='{"provider":{"ferry":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://__FERRY_HOST__:__FERRY_PORT__/v1","apiKey":"local"},"models":{"local-orch":{},"local-sub":{}}}},"model":"ferry/local-orch","small_model":"ferry/local-sub","agent":{"build":{"model":"ferry/local-orch"},"plan":{"model":"ferry/local-orch"},"general":{"model":"ferry/local-sub"},"explore":{"model":"ferry/local-sub"},"scout":{"model":"ferry/local-sub"}}}'

# Bare `opencode` routes through whichever lane you used LAST (cloud until you
# first run opencode-local). An explicit OPENCODE_CONFIG_CONTENT always wins, so
# other tools/wrappers passing their own config are unaffected.
opencode() {
  if [[ -n "${OPENCODE_CONFIG_CONTENT:-}" ]]; then
    command opencode "$@"
    return
  fi
  local cfg="$_FERRY_CFG_CLOUD"
  [[ "$(cat "$_FERRY_LANE_FILE" 2>/dev/null)" == "local" ]] && cfg="$_FERRY_CFG_LOCAL"
  OPENCODE_CONFIG_CONTENT="$cfg" command opencode "$@"
}

# opencode-cloud: the CLOUD pair — orch drives (build/plan), flash runs the
# fan-out (general/explore/scout). Nothing touches the host GPU. Sets the
# bare-`opencode` default.
opencode-cloud() {
  mkdir -p "$_FERRY_CFG_DIR" && printf 'cloud\n' > "$_FERRY_LANE_FILE"
  OPENCODE_CONFIG_CONTENT="$_FERRY_CFG_CLOUD" command opencode "$@"
}

# opencode-local: the GPU pair — local-orch drives (build/plan), local-sub runs
# the fan-out (a hybrid-attention MoE whose tiny KV cache lets many parallel
# agents fit in the host's RAM). Nothing leaves the host. Sets the default.
opencode-local() {
  mkdir -p "$_FERRY_CFG_DIR" && printf 'local\n' > "$_FERRY_LANE_FILE"
  OPENCODE_CONFIG_CONTENT="$_FERRY_CFG_LOCAL" command opencode "$@"
}

# <<< ferry opencode profiles <<<
EOF
python3 - "$ZSHRC" "$HOST_NAME" "$HOST_PORT" <<'PYEOF'
import sys
rc, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(rc).read()
s = s.replace("__FERRY_HOST__", host).replace("__FERRY_PORT__", port)
open(rc, "w").write(s)
PYEOF
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

# 5. Wrap up
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

echo ">>> OPENCODE CLI INSTANT ACCESS:"
echo "    You can now call the host model using standard commands:"
echo "    \033[1;32mhost-code run \"Build a snake game in Python\"\033[0m"
echo "    opencode-cloud   -> cloud pair: orch drives, flash fans out"
echo "    opencode-local   -> GPU pair:   local-orch drives, local-sub fans out"
echo "    bare 'opencode'  -> whichever pair you used LAST (cloud until you first"
echo "                       run opencode-local)"
echo ""
