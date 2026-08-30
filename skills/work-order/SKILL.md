---
name: work-order
description: Execute one Chief of Staff work order inside its assigned Codex task while preserving scope, authority, user changes, and the standard verification and report contract. Use when a dispatch brief contains a CS- work ID.
---

# Work order

Own the dispatched work order. Do not take over coordination for other work orders.

Read [report-contract.md](references/report-contract.md) before closeout.

Confirm the brief names an outcome, mode, authority, project, exact base, scope, acceptance checks, verification, stop conditions, work ID, and coordinator callback task ID. If a required fact is missing and cannot be discovered safely, return `needs-attention` with the exact question.

For Git work, inspect the branch and worktree before editing. Preserve user changes. One task owns writes to this branch or worktree. Do not push, create or update a PR, comment, deploy, or merge unless the brief grants that exact authority.

For bugs, reproduce the failure through the closest practical interface before changing code. Make the smallest coherent change that satisfies acceptance. Run the smallest relevant checks first, then broader checks proportional to risk. Compilation and type checks support verification but do not replace behavioral proof.

Treat acceptance criteria, existing invariants, observed failures, documented
contracts, and credible threats through supported interfaces as the limits of the
implementation. Broad terms such as robust, comprehensive, deterministic, and
production-ready do not add requirements. Before adding an abstraction, state,
configuration, validation, or edge-case handling, identify the requirement or
failure that justifies it. Prefer authoritative upstream behavior and the
production path over local reconstructions or test-only helpers.

Derive the smallest useful test set from acceptance behavior, regressions,
documented contracts, and credible boundary failures. Include a check through the
real production entry point when practical. Large fixtures, extensive mocking,
or many tie-breaking cases are reasons to reconsider the design. Before closeout,
remove speculative behavior, duplicated policy, unused extension points, and
tests outside the supported behavior.

Report evidence, not confidence. Include the exact head SHA when Git is present and label environmental limits. If scope, product intent, or authority conflicts with the work, stop and return the gate instead of guessing.

Notify the coordinator with `send_message_to_thread` when status becomes `needs-attention`, `blocked`, `failed`, `cancelled`, or `done`. Start the message with `<WORK ID> CALLBACK`, then include status, concise outcome, artifacts, blockers, and recommended next stage. Send no-op and nothing-found outcomes too. Send `needs-attention` immediately. Send a terminal callback before ending the final turn. If delivery fails transiently, retry once, then preserve the full report here and state the delivery failure.
