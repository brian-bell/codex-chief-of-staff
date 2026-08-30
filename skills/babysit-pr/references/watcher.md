# GitHub PR watcher

`scripts/watch-pr` makes one bounded observation through the authenticated `gh` CLI. Its fixed GraphQL query reads the PR head, current-head check rollup, reviews, unresolved review threads, pagination state, and merge state. It has no command path for comments, branch updates, merge, or automatic merge.

Pass the SHA associated with existing review or verification evidence through `--expected-head`. If GitHub reports another head, the result is `stale-head` and `verdict_reusable` is false.

The watcher emits one compact, key-sorted JSON object. Every result has:

- `repository`, `pr_number`, `observed_head_sha`, and `observed_at`;
- one `classification`;
- normalized current-attempt `checks`, review records, and `merge_state` evidence;
- `author_waiting`, `product_gate_reasons`, and bounded provider or schema errors;
- a semantic `fingerprint` that excludes observation time;
- `attempts` and `retry_exhausted`.

Classification uses this order:

1. A changed expected head is `stale-head`.
2. Incomplete, contradictory, unauthorized, truncated, or unknown GitHub evidence is `product-gate`.
3. A closed or merged PR is `product-gate`.
4. GitHub conflicts are `conflict`.
5. Requested changes or unresolved current review threads are `blocking-review-feedback`.
6. Failed checks with deterministic conclusions are `deterministic-check-failure`.
7. Timed-out, cancelled, stale, or startup-failed checks are `plausible-transient-failure`.
8. Queued or running checks are `checks-pending`.
9. Draft status, a pending required review, or another non-clean GitHub merge state is `product-gate`.
10. Only an open, mergeable PR with a clean merge state and no earlier condition is `merge-ready`.

The command observes a deterministic check failure once. It retries a plausible transient check failure or an unambiguously transient rate-limit or transport error at most twice after the first attempt. Any permission, schema, or other nontransient error suppresses retries. Each live attempt has a fixed 30-second transport timeout. Use `--max-attempts 1` for a single live read.

Timestamps are parsed as timezone-aware instants and normalized to UTC. Check reruns are selected by normalized time. Readiness reduces the latest current-head APPROVED or CHANGES_REQUESTED decision per author; later COMMENTED reviews do not erase it, and stale reviews remain evidence without affecting a separate current-head decision. Review threads retain their GitHub node IDs, so distinct threads remain distinct in evidence and fingerprints. Ambiguous ties, approvals that exist only for stale heads, missing pagination metadata, head mismatches, rollup contradictions, oversized fields, and responses above the fixed limits cannot reach merge-ready.

Use `--fixture PATH --observed-at ISO-8601` to replay a saved GitHub response without network access. Successful observations write one bounded JSON object to stdout. Argument, fixture, acquisition, permission, and schema failures write one compact typed JSON error to stderr, return nonzero, and omit raw provider prose.
