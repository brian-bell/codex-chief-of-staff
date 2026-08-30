---
name: adversarial-review
description: Independently review a material change at an exact base and head SHA, return structured findings, and produce a SHA-bound verdict without modifying the worker branch. Use when a Chief of Staff work order requires independent review.
---

# Adversarial review

Review the exact base and head named in the brief. Refresh both refs before inspection and report if the requested head is unavailable or changed. Do not modify the worker branch unless the user separately authorizes a fix task.

Inspect the diff and enough surrounding code to test its assumptions. Focus on correctness, security, data integrity, concurrency, compatibility, migrations, billing, defaults, and missing behavioral proof according to the change's risk. Separate fixable defects from product-intent questions.

Judge the change against its acceptance criteria, existing invariants,
documented contracts, regressions, and credible threats through supported
interfaces. Do not require handling for merely representable malformed inputs,
speculative extensibility, or broader interpretations of terms such as robust
and production-ready. Treat unnecessary abstractions, state, configuration,
duplicated policy, test-only production helpers, and disproportionate test
machinery as findings only when they create a concrete maintenance or correctness
risk. Name what can be removed or simplified.

Run relevant checks when practical. Report each finding with severity, location, consequence, and evidence. Avoid style findings unless they create maintenance or correctness risk.

Return a verdict matching [verdict-schema.json](references/verdict-schema.json). Use `blocked` when evidence cannot support pass or fail. Any required code change means the next worker head needs a new verdict.
