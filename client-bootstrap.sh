#!/bin/zsh
# client-bootstrap.sh — Configures a client laptop on the same LAN to use the host's inference server.
# Installs the 'ferry' CLI on the client and wires opencode to the host. Fully
# non-interactive when the host is reachable (the only prompt is the host-name
# fallback when the initial probe fails).
#
#   curl -fsSL http://<host>:<share-port>/client-bootstrap.sh | zsh
#   curl -fsSL http://<host>:<share-port>/client-bootstrap.sh | zsh -s -- --profiles-only
#   curl -fsSL http://<host>:<share-port>/client-bootstrap.sh | zsh -s -- --no-opencode
#
# HOW MUCH OF OPENCODE THIS TOUCHES is a flag, because the answer is not the same
# on a laptop that already has an opencode setup of its own. Three modes:
#
#   full (default)   the whole integration: the takeover of opencode's own
#                    ~/.config/opencode/opencode.json, both ferry lane profiles,
#                    the ~/.zshrc wrappers INCLUDING the bare-`opencode`
#                    override, and the local-lane guardrail files under
#                    ~/.config/opencode/.
#   --profiles-only  ferry keeps to its own directory. Writes only
#                    ~/.config/ferry/opencode-{cloud,local}.json and the two
#                    NAMED wrappers (opencode-cloud / opencode-local). Bare
#                    `opencode` is left alone, and nothing under
#                    ~/.config/opencode is read or written.
#   --no-opencode    installs the ferry CLI and ~/.config/ferry/client.json and
#                    stops. No opencode file of any kind, ferry's own included.
#
# The chosen mode is recorded in client.json as "opencode_mode", so a later
# `client-reset.sh` catches this machine up without silently re-widening it.
#
# CLAUDE CODE is a separate integration with its own single switch. By default
# the claude-ferry / claude-ferry-local wrappers are installed when a `claude`
# CLI exists on this machine (claude absent: a one-line note, same gate the
# guardrails apply to `opencode`); --no-claude skips the step entirely. The
# choice is recorded in client.json as "claude_mode" (full / none), the same
# way, for client-reset.sh to re-apply.

set -eu

# --- Flags ------------------------------------------------------------------
# Piped invocations pass these after `zsh -s --`.
OC_MODE="full"
GUARDRAILS=""   # empty = follow the mode; 1/0 = explicit --with/--no-guardrails
NO_CLAUDE=0

usage() {
  sed -n '2,35p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'
  echo ""
  echo "Flags: --profiles-only | --no-opencode | --full-opencode"
  echo "       --with-guardrails | --no-guardrails   (the /fan-out command and the"
  echo "         spawning-subagents skill, which live in ~/.config/opencode/;"
  echo "         on by default in full mode, off in the other two)"
  echo "       --no-claude   (skip the Claude Code wrappers; by default they are"
  echo "         installed when a 'claude' CLI is on PATH)"
  echo "       -h | --help"
}

for arg in "$@"; do
  case "$arg" in
    --no-opencode)     OC_MODE="none" ;;
    --profiles-only)   OC_MODE="profiles" ;;
    --full-opencode)   OC_MODE="full" ;;
    --with-guardrails) GUARDRAILS=1 ;;
    --no-guardrails)   GUARDRAILS=0 ;;
    --no-claude)       NO_CLAUDE=1 ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "Unknown flag: $arg"; echo "Want: --profiles-only, --no-opencode, --full-opencode, --with-guardrails, --no-guardrails, --no-claude, --help"; exit 1 ;;
  esac
done

# Guardrails default off outside full mode: both files land in ~/.config/opencode,
# which is exactly what the narrower modes exist to leave alone.
if [[ -z "$GUARDRAILS" ]]; then
  [[ "$OC_MODE" == "full" ]] && GUARDRAILS=1 || GUARDRAILS=0
fi

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
case "$OC_MODE" in
  full)     echo "opencode: FULL integration (default config taken over + wrappers)" ;;
  profiles) echo "opencode: PROFILES ONLY — ~/.config/opencode is not touched" ;;
  none)     echo "opencode: NOT CONFIGURED — ferry CLI only" ;;
esac
echo "================================================================="

# 1. Resolve the host. Try, in order, WITHOUT ever prompting first:
#    (a) the share server's injected mDNS name (normal case — no prompt at all)
#    (b) the host saved by a previous run in ~/.config/ferry/client.json
#    (c) a raw LAN-IP prompt as the last resort (mDNS `.local` resolution is
#        flaky on some corp networks, so accept an IP too).
# The prompt only appears when every automatic candidate fails.
echo ">>> Probing host at http://$HOST_NAME:$HOST_PORT..."
if curl -fsS -m 3 "http://$HOST_NAME:$HOST_PORT/v1/models" >/dev/null 2>&1; then
  echo "    \033[1;32mSUCCESS: Connected to host inference server!\033[0m"
else
  # (b) last-known host from a previous bootstrap
  SAVED_HOST=""
  if [[ -f "$HOME/.config/ferry/client.json" ]]; then
    SAVED_HOST=$(python3 -c "import json;print(json.load(open('$HOME/.config/ferry/client.json')).get('host',''))" 2>/dev/null || true)
  fi
  if [[ -n "$SAVED_HOST" && "$SAVED_HOST" != "$HOST_NAME" ]]; then
    echo ">>> First probe failed; retrying with last-known host $SAVED_HOST..."
    HOST_NAME="$SAVED_HOST"
    if curl -fsS -m 3 "http://$HOST_NAME:$HOST_PORT/v1/models" >/dev/null 2>&1; then
      echo "    \033[1;32mSUCCESS: Connected to host inference server at $HOST_NAME!\033[0m"
    else
      HOST_NAME=""
    fi
  else
    HOST_NAME=""
  fi

  # (c) last resort: ask — mDNS name or LAN IP both accepted
  if [[ -z "$HOST_NAME" ]]; then
    echo "    \033[1;31mCould not auto-detect the host.\033[0m"
    echo "    Please enter the host's mDNS hostname or LAN IP"
    printf "    (e.g., mymacbook.local or 192.168.0.100) [Enter to abort]: "

    # Read from /dev/tty because stdin is redirected during a 'curl | zsh' pipe.
    # `|| true` so an EOF/missing tty degrades instead of aborting under set -e.
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
      echo "    No host entered; aborting."
      exit 1
    fi
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

# Claude Code scope is decided before client.json is written so it lands in the
# profile client-reset.sh reads — same gate as the guardrails below use for
# `opencode`: the wrappers only make sense when the claude CLI exists to run.
if [[ $NO_CLAUDE -eq 1 ]]; then
  CLAUDE_MODE="none"
elif command -v claude >/dev/null 2>&1; then
  CLAUDE_MODE="full"
else
  CLAUDE_MODE="none"
fi

# Write local client JSON config profile
echo ">>> Creating client configuration profile..."
mkdir -p "$HOME/.config/ferry"
# opencode_mode is written for client-reset.sh, which re-applies the takeover
# later and must not re-widen a machine that was deliberately bootstrapped
# narrow. Absent (a profile from before this key existed) reads as "full".
# claude_mode is the same idea for the Claude Code wrappers: full when they
# were installed, none when --no-claude was passed or no `claude` CLI exists.
# Absent on a pre-claude profile reads as "none" — a reset never widens.
cat <<EOF > "$HOME/.config/ferry/client.json"
{
  "host": "$HOST_NAME",
  "port": "$HOST_PORT",
  "share_port": "$SHARE_PORT",
  "opencode_mode": "$OC_MODE",
  "claude_mode": "$CLAUDE_MODE"
}
EOF
echo "    Successfully saved profile: ~/.config/ferry/client.json"

# 3. Automatic opencode configuration (the one supported integration; everything
# else can point an OpenAI-compatible client at http://HOST:8090/v1 by hand).
# No lane question: the host predetermines the models behind each lane, the
# `opencode-cloud` / `opencode-local` / `opencode-super` wrappers pick a pair
# per invocation, and bare `opencode` follows whichever wrapper ran last (cloud
# until then). So the persistent default written here is always the cloud pair.
#
# MECHANISM NOTE: opencode takes config via OPENCODE_CONFIG (a FILE PATH), not
# an env var holding JSON — an invented OPENCODE_CONFIG_CONTENT is silently
# ignored and every wrapper silently runs whatever the default config is. So
# each pair gets a real config file written here, and the wrappers below point
# OPENCODE_CONFIG at it.
#
# ONE WRITER: every one of these files is written by `ferry opencode`, never by
# this script. That command is a surgical takeover — it replaces exactly
# permission / model / small_model / agent, appends the goal plugin, leaves
# every other key alone, and snapshots the previous file to <name>.<UTC>.jsonc
# before it writes. This script used to json.dump the two profiles from
# scratch instead, which silently ate any agent, permission, mcp or command
# block the user had added to them on every re-run.
echo ""
OC_FAILED=0
if [[ "$OC_MODE" == "none" ]]; then
  echo ">>> Skipping opencode configuration (--no-opencode)."
  echo "    Nothing under ~/.config/opencode or ~/.config/ferry/opencode-*.json was"
  echo "    written. To route an OpenAI-compatible client at the host by hand:"
  echo "      baseURL http://$HOST_NAME:$HOST_PORT/v1, apiKey 'local', and a LANE"
  echo "      NAME from http://$HOST_NAME:$HOST_PORT/v1/models."
else
  echo ">>> Auto-configuring 'opencode' to route through the host..."
  mkdir -p "$HOME/.config/ferry"
  # target|extra-flags. The three ferry profiles the wrappers select between
  # are always written — they are ferry's own files. opencode's OWN default
  # config is in the list only in full mode: --profiles-only exists precisely
  # so that a laptop with its own opencode setup keeps it.
  oc_targets=(
    "$HOME/.config/ferry/opencode-cloud.json|"
    "$HOME/.config/ferry/opencode-local.json|--local"
    "$HOME/.config/ferry/opencode-super.json|--super"
  )
  if [[ "$OC_MODE" == "full" ]]; then
    oc_targets=("$HOME/.config/opencode/opencode.json|" "${oc_targets[@]}")
  fi
  for oc_target in "${oc_targets[@]}"; do
    oc_path="${oc_target%%|*}"
    oc_flag="${oc_target#*|}"
    echo "    -> $oc_path"
    # Unset OPENCODE_CONFIG for the call: `ferry opencode` honours it as the
    # default target, and a bootstrap that inherited one from the caller's shell
    # would write the same file three times.
    if ! env -u OPENCODE_CONFIG "$HOME/.local/bin/ferry" opencode \
          --host "$HOST_NAME" --port "$HOST_PORT" --config "$oc_path" $oc_flag; then
      OC_FAILED=1
    fi
  done
  if [[ "$OC_MODE" == "profiles" ]]; then
    echo "    --profiles-only: ~/.config/opencode/opencode.json was NOT read or written."
  fi
fi
if [[ $OC_FAILED -eq 1 ]]; then
  echo "    WARNING: 'ferry opencode' failed for at least one target. Wire opencode"
  echo "    manually: an openai-compatible provider with baseURL=http://$HOST_NAME:$HOST_PORT/v1,"
  echo "    apiKey=local, and a LANE NAME from http://$HOST_NAME:$HOST_PORT/v1/models."
fi

# Setup shell alias in .zshrc. In full mode `host-code` rides on the bare
# `opencode` wrapper; in profiles mode there IS no bare wrapper, so it points at
# the named cloud one instead. In --no-opencode mode it would be a shortcut to
# the user's own unrelated opencode, so it is not installed at all.
ZSHRC="$HOME/.zshrc"
if [[ "$OC_MODE" == "none" ]]; then
  echo ">>> Skipping the 'host-code' shortcut (--no-opencode)."
  ALIAS_LINE=""
elif [[ "$OC_MODE" == "profiles" ]]; then
  echo ">>> Configuring terminal 'host-code' shortcut (-> opencode-cloud) in ~/.zshrc..."
  ALIAS_LINE="alias host-code='opencode-cloud'"
else
  echo ">>> Configuring terminal 'host-code' shortcut in ~/.zshrc..."
  ALIAS_LINE="alias host-code='opencode'"
fi

if [[ -z "$ALIAS_LINE" ]]; then
  :
elif [[ -f "$ZSHRC" ]]; then
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

# Ensure opencode profile functions (opencode-cloud, opencode-local,
# opencode-super) are in ~/.zshrc.
# --no-opencode installs none of them and, deliberately, does not remove a block
# an earlier run left behind either: silently deleting shell functions the user
# may still be using is its own surprise. It says so instead.
ZSHRC="$HOME/.zshrc"
if [[ "$OC_MODE" == "none" ]]; then
  echo ">>> Skipping the opencode shell wrappers (--no-opencode)."
  if [[ -f "$ZSHRC" ]] && grep -q '# >>> ferry opencode profiles >>>' "$ZSHRC" 2>/dev/null; then
    echo "    NOTE: an earlier bootstrap's wrapper block is still in ~/.zshrc, so bare"
    echo "    'opencode' is still being redirected to a ferry profile. Delete the block"
    echo "    marked '# >>> ferry opencode profiles >>>', or run client-cleanup.sh."
  fi
else
echo ">>> Configuring opencode profile functions in ~/.zshrc..."
touch "$ZSHRC"

# Strip any existing ferry opencode profiles block before re-adding, and remove
# LEGACY `alias opencode-cloud` / `alias opencode-local` / `alias opencode` lines
# from older hand-wired setups: an alias defined above a function definition
# makes zsh expand it inside `name() {` -> "defining function based on alias"
# -> "parse error near ()" on every future `source ~/.zshrc`.
python3 - "$ZSHRC" "# >>> ferry opencode profiles >>>" "# <<< ferry opencode profiles <<<" "$OC_MODE" <<'PYEOF'
import sys
rc, start, end, mode = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
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
# Legacy aliases collide with the function names below — but only with the
# functions we are about to DEFINE. In profiles mode no bare `opencode` function
# is written, so a user's own `alias opencode=...` is theirs to keep.
def is_legacy_alias(l):
    t = l.lstrip()
    if (t.startswith("alias opencode-cloud=") or t.startswith("alias opencode-local=")
            or t.startswith("alias opencode-super=")):
        return True
    return mode == "full" and t.startswith("alias opencode=")
out = [l for l in out if not is_legacy_alias(l)]
while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
PYEOF

# The three profile FILES the wrappers below select between were already written
# by `ferry opencode` in step 3 — this block only installs the wrappers.
#
# QUOTED heredoc: the body is written VERBATIM (no $, backtick, or quote
# expansion), which is what keeps the $HOME/$@ inside the functions intact.
if [[ "$OC_MODE" == "profiles" ]]; then
cat <<'EOF' >> "$ZSHRC"
# >>> ferry opencode profiles >>>
# --profiles-only: the three NAMED wrappers and nothing else. There is
# deliberately no bare `opencode` function here, so plain `opencode` keeps
# using whatever config this machine already had — ferry is opt-in, per
# invocation.
unalias opencode-cloud opencode-local opencode-super 2>/dev/null

# opencode-cloud: the CLOUD pair — orch drives (build/plan), flash runs the
# fan-out and the housekeeping models. Nothing touches the host GPU.
opencode-cloud() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-cloud.json" command opencode "$@"
}

# opencode-local: the GPU pair — local-orch drives, local-sub runs the fan-out.
# Nothing leaves the host.
opencode-local() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-local.json" command opencode "$@"
}

# opencode-super: heavy drives; super-flash runs the fan-out AND the
# housekeeping (title/summary/compaction). The cheapest cloud profile.
opencode-super() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-super.json" command opencode "$@"
}

# <<< ferry opencode profiles <<<
EOF
echo "    Added opencode-cloud / opencode-local / opencode-super to ~/.zshrc."
echo "    Bare 'opencode' was NOT wrapped (--profiles-only)."
else
cat <<'EOF' >> "$ZSHRC"
# >>> ferry opencode profiles >>>
# Defensive: an alias with a function's name anywhere earlier in the file (or in
# the live shell) breaks the definitions below with "defining function based on
# alias". Kill them first.
unalias opencode opencode-cloud opencode-local opencode-super 2>/dev/null

# Bare `opencode` routes through whichever lane you used LAST (cloud until you
# first run opencode-local or opencode-super). An explicit OPENCODE_CONFIG
# always wins, so other tools/wrappers passing their own config are unaffected.
opencode() {
  if [[ -n "${OPENCODE_CONFIG:-}" ]]; then
    command opencode "$@"
    return
  fi
  local lane="$(cat "$HOME/.config/ferry/last-lane" 2>/dev/null)"
  local cfg="$HOME/.config/ferry/opencode-cloud.json"
  if [[ "$lane" == "super" ]]; then
    cfg="$HOME/.config/ferry/opencode-super.json"
  elif [[ "$lane" == "local" ]]; then
    cfg="$HOME/.config/ferry/opencode-local.json"
  fi
  OPENCODE_CONFIG="$cfg" command opencode "$@"
}

# opencode-cloud: the CLOUD pair — orch drives (build/plan), flash runs the
# fan-out and the housekeeping models (general/explore/title/summary/compaction).
# Nothing touches the host GPU. Sets the bare-`opencode` default.
opencode-cloud() {
  mkdir -p "$HOME/.config/ferry" && printf 'cloud\n' > "$HOME/.config/ferry/last-lane"
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-cloud.json" command opencode "$@"
}

# opencode-local: the GPU pair — local-orch drives (build/plan), local-sub runs
# the fan-out (a hybrid-attention MoE whose tiny KV cache lets many parallel
# agents fit in the host's RAM). Nothing leaves the host. Sets the default.
opencode-local() {
  mkdir -p "$HOME/.config/ferry" && printf 'local\n' > "$HOME/.config/ferry/last-lane"
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-local.json" command opencode "$@"
}

# opencode-super: heavy drives; super-flash runs the fan-out AND the
# housekeeping (title/summary/compaction). The cheapest cloud profile. Sets the
# bare-`opencode` default.
opencode-super() {
  mkdir -p "$HOME/.config/ferry" && printf 'super\n' > "$HOME/.config/ferry/last-lane"
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-super.json" command opencode "$@"
}

# <<< ferry opencode profiles <<<
EOF
echo "    Successfully added opencode profile functions to ~/.zshrc."
fi
fi

# Install the local-lane opencode guardrails: /fan-out command + spawning-subagents
# skill (global dirs — opencode picks them up automatically). Keep the heredoc
# bodies in sync with opencode/command/fan-out.md and
# opencode/skills/spawning-subagents/SKILL.md in the llm-ferry repo.
# Context: Nemotron (the opencode-local model) produces malformed task-tool
# calls (hallucinated task_id, missing description) that are rejected before
# the tool runs, causing silent retry doom loops. The recipe injected as the
# USER message (what /fan-out does) is the empirically working fix.
#
# Both files land under ~/.config/opencode/, so they follow the mode: on in full,
# off in --profiles-only / --no-opencode unless --with-guardrails asks for them.
if [[ $GUARDRAILS -eq 0 ]]; then
  echo ">>> Skipping the local-lane guardrails (they live in ~/.config/opencode/)."
  echo "    They are two NEW files — /fan-out and the spawning-subagents skill — and"
  echo "    modify nothing existing. Add them with: --with-guardrails"
elif ! command -v opencode >/dev/null 2>&1; then
  echo ">>> opencode is not on PATH — skipping the local-lane guardrails."
else
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

# 4. Claude Code integration. Claude Code speaks the Anthropic protocol, not
# OpenAI's, so the opencode profiles do not serve it: `ferry claude` writes
# ~/.config/ferry/claude.json and installs the claude-ferry /
# claude-ferry-local wrapper functions into its own marked block in ~/.zshrc
# (its installer, its markers — this script adds no bare `claude()` wrapper).
# Independent of the opencode mode; scope was recorded in client.json above.
echo ""
CLAUDE_FAILED=0
if [[ "$CLAUDE_MODE" == "full" ]]; then
  echo ">>> Wiring Claude Code to the host..."
  # env -u OPENCODE_CONFIG: same hygiene as the `ferry opencode` calls above —
  # the shell's own pointer must not leak into ferry's installer.
  if ! env -u OPENCODE_CONFIG "$HOME/.local/bin/ferry" claude \
        --host "$HOST_NAME" --port "$HOST_PORT"; then
    CLAUDE_FAILED=1
  fi
elif [[ $NO_CLAUDE -eq 1 ]]; then
  echo ">>> Skipping Claude Code wiring (--no-claude)."
else
  echo ">>> NOTE: no 'claude' CLI on PATH — skipping Claude Code wiring."
fi
if [[ $CLAUDE_FAILED -eq 1 ]]; then
  echo "    WARNING: 'ferry claude' failed. By hand: point Claude Code's"
  echo "    ANTHROPIC_BASE_URL at http://$HOST_NAME:$HOST_PORT (any bearer token),"
  echo "    or re-run this script / client-reset.sh."
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

case "$OC_MODE" in
  full)
    echo ">>> OPENCODE CLI INSTANT ACCESS:"
    echo "    You can now call the host model using standard commands:"
    echo "    \033[1;32mhost-code run \"Build a snake game in Python\"\033[0m"
    echo "    opencode-cloud   -> cloud pair:  orch drives, flash fans out"
    echo "    opencode-local   -> GPU pair:    local-orch drives, local-sub fans out"
    echo "    opencode-super   -> super pair:  heavy drives, super-flash fans out"
    echo "                     AND keeps house (title/summary/compaction)"
    echo "    bare 'opencode'  -> whichever pair you used LAST (cloud until you first"
    echo "                       run opencode-local or opencode-super)"
    ;;
  profiles)
    echo ">>> OPENCODE, OPT-IN PER INVOCATION:"
    echo "    opencode-cloud   -> cloud pair:  orch drives, flash fans out"
    echo "    opencode-local   -> GPU pair:    local-orch drives, local-sub fans out"
    echo "    opencode-super   -> super pair:  heavy drives, super-flash fans out"
    echo "                     AND keeps house (title/summary/compaction)"
    echo "    bare 'opencode'  -> UNCHANGED. Your own config, exactly as it was."
    echo "    Without the wrappers, the same thing by hand:"
    echo "      OPENCODE_CONFIG=~/.config/ferry/opencode-cloud.json opencode ..."
    echo "    Widen later with:  --full-opencode    Narrow further with: --no-opencode"
    ;;
  none)
    echo ">>> OPENCODE WAS NOT CONFIGURED (--no-opencode)."
    echo "    Point any OpenAI-compatible client at the host yourself:"
    echo "      baseURL http://$HOST_NAME:$HOST_PORT/v1   apiKey local"
    echo "      model   a LANE NAME from http://$HOST_NAME:$HOST_PORT/v1/models"
    echo "    Or re-run this script with --profiles-only for ferry-owned opencode"
    echo "    profiles that leave ~/.config/opencode alone."
    ;;
esac

case "$CLAUDE_MODE" in
  full)
    echo ">>> CLAUDE CODE ON THE FERRY BACKEND:"
    echo "    claude-ferry / claude-ferry-local installed (Claude Code on the ferry backend)"
    echo "    claude-ferry       -> cloud lanes: heavy drives, flash fans out"
    echo "    claude-ferry-local -> GPU lanes:   local-orch drives, local-sub fans out"
    echo "    bare 'claude' is UNCHANGED. Skip the wrappers with --no-claude."
    if [[ $CLAUDE_FAILED -eq 1 ]]; then
      echo "    (WARNING: the wiring step FAILED above — the wrappers may not work yet.)"
    fi
    ;;
  none)
    if [[ $NO_CLAUDE -eq 1 ]]; then
      echo ">>> CLAUDE CODE WAS NOT CONFIGURED (--no-claude)."
    else
      echo ">>> CLAUDE CODE WAS NOT CONFIGURED (no 'claude' CLI on PATH)."
    fi
    echo "    By hand: ANTHROPIC_BASE_URL=http://$HOST_NAME:$HOST_PORT (any bearer"
    echo "    token), model = a LANE NAME (heavy/flash cloud, local-orch/local-sub GPU)."
    ;;
esac
echo ""
