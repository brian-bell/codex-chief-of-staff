# Chief of Staff for Codex

Chief of Staff gives one Codex task a durable way to coordinate visible peer tasks without replacing Codex task history, Git, CI, or the forge as sources of truth.

Version 0.1 ships the coordination contract and local state core:

- Scout, Build, Review, Publish, Babysit, Land, and Triage authority rules.
- Focused coordinator, worker, review, PR, and triage skills.
- A versioned SQLite ledger and JSON CLI.
- Evidence-backed state transitions and user gates.
- Idempotent dispatch and event recording.
- Worker callbacks that wake the Chief task on completion or attention states.
- One task per work-order role, which prevents duplicate writers.
- Review verdicts bound to the current head SHA.
- A Land gate that requires explicit merge authority and a current passing verdict.

The first release does not include a forge-specific PR watcher, automatic merge adapter, scheduled-task installer, or the Grok Ship triage fetcher. The skills keep those boundaries explicit so later adapters can be added without weakening authority.

## Validate

From the repository root:

```zsh
python3 -m unittest discover -s tests -v
python3 /Users/brian/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py ./codex-chief-of-staff
```

The runtime uses Python 3.11 or newer and only the standard library.

## Install from a local marketplace

Place `codex-chief-of-staff` under the `plugins/` directory of a local marketplace, add a marketplace entry whose source path is `./plugins/codex-chief-of-staff`, then run:

```zsh
codex plugin marketplace add /absolute/path/to/marketplace-root
codex plugin add codex-chief-of-staff@<marketplace-name>
```

Restart Codex after install so a new task picks up the plugin snapshot.

## Set up the coordinator task

Create one projectless Codex task titled `Chief of Staff` and invoke `$chief-of-staff` explicitly in its first prompt. Pin the task if you want it to remain the main intake point. You can copy [AGENTS.chief-of-staff.md](templates/AGENTS.chief-of-staff.md) into the task instructions when you want a durable coordinator charter.

The ledger defaults to `~/.codex/data/codex-chief-of-staff/state.db`. Initialize it with:

```zsh
./skills/chief-of-staff/scripts/chief-of-staff-state init
```

Use `--db PATH` for tests or an alternate data location.
