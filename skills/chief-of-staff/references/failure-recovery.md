# Failure recovery

## Coordinator restart

List open work orders. Resolve every linked task's current status. Refresh Git and forge state. Continue from the newest event that still matches live evidence. Do not replay the original request blindly.

## Worker failure

Record the last observed evidence and classify cancellation, environment failure, unclear failure, or task defect. Retry once when the failure is plausibly transient. Reduce scope after context or resource exhaustion. If the next attempt hits the same block, record the exact resumption command or prompt and surface the blocker.

## Stale result

Compare the reported branch and SHA with the live repository. Keep stale receipts in history, but do not use them for promotion or Land. A new head needs a new review unless deterministic patch identity proves equivalence and repository policy permits that proof.

## Duplicate worker

Do not replace an existing task mapping silently. Stop a duplicate writer or give it a non-overlapping read-only role. Never let two tasks write the same branch or worktree.

## Changed direction

Record the user instruction as an event. Stop or narrow affected workers before sending replacement scope. Preserve useful artifacts and mark abandoned work `superseded` when appropriate.
