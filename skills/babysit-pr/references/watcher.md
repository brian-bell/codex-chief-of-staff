# Pull-request watcher contract

`scripts/watch-pr` observes one GitHub pull request through a fixed GraphQL query. The query names the exact owner, repository, and pull-request number. The command runner rejects every other `gh` command and rejects any altered query. It never invokes a GitHub mutation, `gh pr` write command, Git push, comment action, merge action, or auto-merge action.

The command writes one compact, key-sorted JSON value to stdout. Invalid arguments, unreadable fixtures, and runtime failures return nonzero and write one compact JSON error to stderr. Use `--fixture PATH --observed-at TIME` to replay a saved response without network access.

## Classification precedence

The classifier applies the first matching rule:

1. `stale-head`: the live head differs from either supplied authoritative constraint. The classifier checks `--expected-head` and `--verdict-head` independently.
2. `conflict`: GitHub reports `CONFLICTING` or `DIRTY`.
3. `blocking-review-feedback`: a reviewer's latest submission requests changes on the observed head, or a current unresolved review thread exists.
4. `product-gate`: the PR is closed or draft; an allowlisted author-waiting, hold, or product-decision label is present; enough current-head approvals are missing; mergeability is unknown; or evidence is incomplete.
5. `checks-pending`: a check is still running, or a required check has not appeared.
6. `deterministic-check-failure`: a CheckRun finishes with any documented non-success conclusion, a StatusContext reports `ERROR` or `FAILURE`, or an empty rollup reports a failure. `CANCELLED`, `NEUTRAL`, `SKIPPED`, and `STALE` are deterministic failures rather than successes.
7. `plausible-transient-failure`: a saved GitHub response contains a typed `RATE_LIMITED` error. Live timeouts and rate-limit errors use the nonzero acquisition-error contract after bounded retries.
8. `merge-ready`: all requested evidence is complete, the head is current, the PR is open and not draft, GitHub reports no conflict, blocking feedback is absent, required reviews are approved, and required checks passed.

Check names, titles, summaries, annotations, review bodies, and arbitrary labels never control retries. The live query does not request titles, summaries, annotations, or review bodies. The output contains machine fields, bounded identifiers, allowlisted policy labels, and check detail hostnames.

The classifier normalizes CheckRun and StatusContext separately, including when both types use the same displayed name. A CheckRun that is not `COMPLETED` is pending. A completed run succeeds only with `SUCCESS`. A StatusContext succeeds only with `SUCCESS`; `PENDING` remains pending. Missing or unknown machine states are incomplete evidence. Check and review timestamps must parse as timezone-aware ISO-8601 instants. Ordering compares their normalized UTC instants, so offsets and fractional seconds are chronological rather than lexical. Invalid or timezone-free timestamps make their connection incomplete. For repeated runs of the same type and logical identity, parsed timestamps and database IDs select the latest attempt. Ties choose pending, then failure, then incomplete, then success, followed by a canonical key covering every emitted semantic check field. Source order never breaks a tie.

The status-check rollup remains authoritative when child contexts exist. `FAILURE` and `ERROR` produce a deterministic failure even if a child is successful or pending. `PENDING` and `EXPECTED` remain pending while the result retains any child failure detail. A `SUCCESS` rollup still yields to a pending or failed child. Unknown, malformed, or incomplete rollup or child state is a product gate. No contradictory rollup and child combination can produce `merge-ready`.

Reviews retain their commit SHA. The classifier selects each reviewer's latest submission by timestamp with a conservative tie break. At equal timestamps, current-head change requests and evidence that cannot prove a current approval outrank a current approval; review ID ordering cannot make the PR merge-ready. If timestamp, conservative rank, and ID also tie, a canonical key covering every emitted review field selects the same evidence regardless of source order. Required-review policy counts only distinct approvals for the observed head. Old-head approvals, dismissed approvals, unknown states, and approvals without a commit SHA cannot satisfy the rule.

## Result evidence

Each result contains:

- Repository, PR number, UTC observation time, expected head, verdict head, and observed head.
- Classification, reason, current-verdict applicability, and an observation fingerprint.
- Base branch, PR state, draft flag, mergeability, merge-state status, review decision, author-waiting signals, and product-gate signals.
- Sorted checks, reviews, unresolved-thread IDs, applicable branch-protection rules, and review, thread, and conversation-comment counts.
- Permission status for review threads and branch protection.
- GraphQL error types and paths, missing fields, incomplete or truncated connections, rate-limit state, and retry attempts, delays, and exhaustion.

The semantic fingerprint covers classification inputs and normalized current state. It excludes observation time, retry attempts and delays, and rate-limit cost, remaining points, and reset time. Equivalent source order and duplicate-run order produce the same fingerprint. Head, classification, selected checks, current reviews, merge state, partial evidence, and product gates change it.

## Retry and permission behavior

The default live policy permits two retries with one-second and two-second delays. Every `gh` subprocess has a 20-second timeout. Tests replace the sleeper, timeout runner, and adapter. Deterministic check failures and `checks-pending` never enter the transport retry loop. The watcher never polls indefinitely.

Fixture replay keeps the library contract: GraphQL permission errors, omitted fields, malformed payloads, and incomplete connections return a conservative result. The top-level `errors` value, when present, must be a list of objects with a nonempty string `type`. Optional paths, locations, messages, and extensions must match their documented JSON types. A malformed error list marks fixture evidence incomplete and cannot activate retry from an untrusted type. A valid error list is transient only when every error has the exact type `RATE_LIMITED`. Permission types take nontransient permission precedence, and any other mixed type is a conservative nontransient failure. Source order cannot change that decision.

Normalized error evidence is fixed in size. Results retain at most 16 errors in canonical order, eight path components per error, and 80 characters per type or path component. Nonnegative integer path components of up to 80 decimal digits remain exact. Larger integers become 77-character SHA-256 tokens computed from every bit of their canonical binary value. The normalizer feeds fixed-size chunks to the hash and never constructs or emits the full decimal value or a full-size byte buffer. The result reports the full error count and whether evidence was truncated. Messages, extensions, and locations never enter output. Classification and retry inspect every validated error before output truncation, so a nontransient error outside the retained subset still prevents retry. Live acquisition is stricter. Transport failures, malformed or non-object JSON, malformed GraphQL errors, permission failures, and required-schema failures write one compact safe JSON error to stderr and exit nonzero. A structured GraphQL error in stdout is still inspected when `gh` exits nonzero: only an all-rate-limited list enters bounded retry, while a permission denial fails immediately. No stderr body, GraphQL message, token, or response content is copied into that error.

Checks, reviews, review threads, labels, and branch protection each require at most 100 nodes, an exact nonnegative total count, and page information in both directions. Every node must match the typed fields in the fixed GraphQL query, including nested author and commit identities, exact booleans, integer counts, and string lists. The PR head must be a 40-character hexadecimal object ID, `isDraft` must be an exact boolean, and the other required PR state fields must be nonempty strings. Any next or previous page, missing or mistyped field, malformed node, or count that differs from the returned-node count marks fixture evidence partial and is a live schema error. A successful query that consumes the last rate-limit point remains a successful observation; `remaining: 0` alone does not trigger a retry.
