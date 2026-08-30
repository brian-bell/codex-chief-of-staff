---
name: babysit-pr
description: Follow an authorized pull request through CI and review feedback until it is merge-ready or has a named blocker, while keeping Land as a separate authority stage. Use for an existing Chief of Staff PR work order.
---

# Babysit PR

Confirm the repository, PR, owning branch, current head SHA, allowed writes, required checks, and review bar. Refresh live forge state before each decision.

Run the bundled watcher before classifying GitHub state:

```zsh
./skills/babysit-pr/scripts/watch-pr --repo OWNER/REPOSITORY --pr NUMBER --expected-head SHA
```

Read [watcher.md](references/watcher.md) for the output contract and retry rules. Use the result's evidence and classification. Do not infer merge readiness from comments, logs, or a prior observation.

Address CI or review feedback only when the brief grants PR-maintenance authority and the change remains in scope. Verify and push each fix through the owning branch, then record the new head. A new head invalidates prior review evidence.

Do not retry an identical deterministic failure as a flake. Do not reply publicly unless comments are authorized. Stop at merge-ready. Never merge or arm automatic merge from this skill.
