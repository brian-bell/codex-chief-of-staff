# Dispatch contract

Send every peer task a complete brief. Keep simple work brief, but do not omit a field. Do not repeat standing Build or Review policy in the brief; include only facts, constraints, and checks specific to this work order.

```text
WORK ID
Stable CS- identifier shared by tasks and artifacts.

OUTCOME
One sentence stating the user-visible or system result.

MODE AND AUTHORITY
Mode, allowed writes, and forbidden writes.

PROJECT AND REPOSITORY
Codex project, repository, source control, and execution environment.

BASE
Exact branch, ref, or working-tree state. Never invent it.

SCOPE
Paths, services, and behavior in scope. State what must remain unchanged.

CONTEXT
Issue, evidence, decisions, relevant files, and constraints.

ACCEPTANCE
One checkable criterion per line.

VERIFY
Commands or real-interface workflow, including known environment limits.

STOP CONDITIONS
Questions that require the user and facts the worker must not guess.

CALLBACK
Exact coordinator task ID and work ID. Instruct the worker to call
send_message_to_thread for needs-attention, blocked, failed, cancelled, and
done. Empty results and no-op outcomes still require a callback.

REPORT
Status, branch, head SHA, files changed, checks and results, artifacts,
deviations, blockers, and recommended next stage.
```

Use this callback message shape:

```text
CS-N CALLBACK
STATUS: done|needs-attention|blocked|failed|cancelled
SUMMARY: concise outcome, including no change or nothing found
ARTIFACTS: branch, head SHA, PR, report path, or none
BLOCKERS: exact question or none
RECOMMENDED NEXT STAGE: stage or none
```

The worker sends the callback with `send_message_to_thread` to the exact coordinator task ID. Include the coordinator host ID when the brief provides one. The worker sends `needs-attention` as soon as it needs a user decision. It sends a terminal callback before ending its final turn. If delivery fails transiently, retry once. If it still fails, keep the full report in the worker task and state that callback delivery failed.

Record a digest of the exact brief with the task mapping. Do not store secrets in the brief or callback. After task creation, record the returned task ID and environment before the work order enters `dispatched`.
