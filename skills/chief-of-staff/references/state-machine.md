# State machine

The ledger accepts these normal transitions:

```text
draft -> queued -> dispatched -> running
running -> verifying -> review-required -> ready-to-publish
ready-to-publish -> published -> babysitting -> merge-ready
merge-ready -> awaiting-merge-authority -> landing -> done
```

Some stages may be skipped when risk and authority permit it. The CLI still enforces its transition table. Terminal alternatives are `blocked`, `cancelled`, `failed`, and `superseded`.

Every transition needs a non-empty JSON evidence object. The following transitions have extra proof requirements:

- `dispatched` needs a recorded task link.
- `ready-to-publish` and later states cannot have an open gate.
- `published` needs a PR URL and current head SHA.
- `landing` needs merge authority and a passing or pass-with-notes verdict for the current head.
- `done` needs the actual terminal artifact in evidence, not a statement that work is complete.

Run `verdict current` immediately before Land. Refresh the live PR head first. A verdict remains in the audit history after a head change, but it is no longer applicable.
