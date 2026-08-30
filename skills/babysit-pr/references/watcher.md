# GitHub PR watcher

`scripts/watch-pr` makes one bounded observation through the authenticated `gh` CLI. Its fixed GraphQL query reads the PR head, check rollup, reviews, review threads, pagination state, and merge state. It has no command path for comments, branch updates, merge, or automatic merge.

Pass the 40-character SHA associated with existing Chief of Staff verdict evidence through `--expected-head`. If GitHub reports another head, the result is `stale-head`, `action_owner` is `reviewer`, and `verdict_reusable` is false. Without `--expected-head`, `verdict_reusable` is also false because the watcher has no existing verdict to compare. GitHub's current `reviewDecision` controls GitHub approval state. The ledger separately binds the Chief of Staff verdict to the exact head SHA.

## Complete observations

A complete observation writes one compact, key-sorted JSON object to stdout and exits zero. It includes:

- `acquisition_state` set to `complete`;
- the repository, PR number, observed head, and observation time;
- one `classification` and its `action_owner`;
- normalized checks, reviews, review threads, and merge state;
- product-gate reasons when they apply;
- a semantic fingerprint that excludes observation time and retry telemetry;
- attempt and retry-exhaustion telemetry.

Classification uses this order:

1. A changed expected head is `stale-head`.
2. A closed or merged PR is `product-gate`.
3. A draft PR is `product-gate` with `action_owner` set to `author`.
4. GitHub conflicts are `conflict`.
5. Requested changes or unresolved current review threads are `blocking-review-feedback`.
6. `ACTION_REQUIRED`, `FAILURE`, `NEUTRAL`, or `SKIPPED` checks are `deterministic-check-failure`.
7. Timed-out, cancelled, stale, startup-failed, or error checks are `plausible-transient-failure`.
8. Queued or running checks are `checks-pending`.
9. A required review or another understood policy block is `product-gate`.
10. Only an open, non-draft, mergeable PR with a clean merge state and no earlier condition is `merge-ready`.
11. Any other complete state is `product-gate` with a specific reason.

`action_owner` is one of `none`, `author`, `reviewer`, `ci`, `product`, or `provider`. It identifies who or what must change before the PR can advance.

## Observation errors

Incomplete evidence receives no PR classification. Partial GraphQL data, missing fields, unknown enum values, contradictory rollups, truncated required connections, malformed scalar types, ambiguous attempts, and oversized fields return a typed error with `acquisition_state` set to `partial`.

Provider failures that return no usable observation use `unavailable`. Errors write one bounded JSON object to stderr, exit nonzero, omit raw provider prose, and never emit a traceback.

Required connections are limited to 100 nodes. The watcher fails closed when GitHub reports another page.

## Retry rules

The watcher retries only temporary acquisition failures such as rate limits, timeouts, and connection failures. Permission and schema failures do not retry. CI results never retry within one invocation. Babysit decides when to make the next observation.

Each live attempt has a 30-second transport timeout. `--max-attempts` accepts 1 through 3 and limits acquisition attempts.

Timestamps are timezone-aware and normalized to UTC. Repeated checks use kind and name as their identity, then select the latest valid provider timestamp. Reordering provider arrays does not change normalized evidence or the semantic fingerprint. A repeated attempt without complete timestamps or distinct attempts at the same timestamp produce a partial observation.

Use `--fixture PATH --observed-at ISO-8601` to replay a saved GitHub response without network access.
