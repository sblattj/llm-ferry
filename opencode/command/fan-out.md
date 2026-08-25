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
