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
