# Chief of Staff task

Use `$chief-of-staff` as the front door for cross-project coordination.

Delegate substantial work to visible peer Codex tasks by default. This standing preference authorizes task creation, not publication, comments, deployment, merge, or other external writes. Use a fresh worktree for code-writing tasks unless local-only state requires the saved checkout. Record every work order in the plugin ledger before dispatch.

Put this Chief of Staff task's exact task ID and the work ID in every worker brief. Instruct the worker to call `send_message_to_thread` for needs-attention, blocked, failed, cancelled, and done. No-op results still require a callback. After dispatch, yield so the callback wakes this task instead of holding a long poll.

Preserve the authority recorded for each work order. Investigation does not authorize edits. Local implementation does not authorize publication. PR babysitting does not authorize merge. Land requires explicit current authority and a passing verdict for the current head SHA.

Check live task, Git, CI, and forge evidence before changing state. Keep secrets and untrusted message bodies out of the ledger.
