# Chief of Staff for Codex

Chief of Staff gives one Codex task a durable way to coordinate visible peer tasks without replacing Codex task history, Git, CI, or the forge as sources of truth.

Version 0.1 includes:

- Scout, Build, Review, Publish, Babysit, Land, and Triage authority rules.
- Coordinator, worker, independent-review, PR-maintenance, and repository-triage skills.
- A versioned SQLite ledger and JSON CLI.
- Evidence-backed state transitions and user gates.
- Idempotent dispatch and event recording.
- Worker callbacks that wake the Chief task on completion or attention states.
- One task per work-order role.
- Review verdicts bound to the current head SHA.
- A Land gate that requires explicit merge authority and a current passing verdict.
- A standard-library-only, read-only GitHub PR watcher with deterministic classification and bounded retries.

The first release does not include PR creation or update automation, an automatic merge adapter, automatic scheduling, GitLab or stacked-PR support, or a Grok Ship triage fetcher. Publish and Land remain authority stages, not bundled forge automation.

## Requirements

Chief of Staff requires Python 3.11 or newer and uses only the Python standard library. Installation also requires Codex with local plugin marketplace support.

## Quick start

Tell any Codex task:

```text
Follow https://github.com/brian-bell/codex-chief-of-staff/blob/main/INIT.md
```

Codex installs or refreshes the plugin, initializes the ledger, and opens a pinned Chief of Staff task. Bring future work to that task.

## Manual setup

Clone the repository into the default personal marketplace's plugin directory:

```zsh
git clone https://github.com/brian-bell/codex-chief-of-staff.git ~/plugins/codex-chief-of-staff
```

Add this entry to `~/.agents/plugins/marketplace.json` if it is not already present:

```json
{
  "name": "codex-chief-of-staff",
  "source": {
    "source": "local",
    "path": "./plugins/codex-chief-of-staff"
  },
  "policy": {
    "installation": "AVAILABLE",
    "authentication": "ON_INSTALL"
  },
  "category": "Productivity"
}
```

Install the plugin from the default `personal` marketplace:

```zsh
codex plugin add codex-chief-of-staff@personal
```

The personal marketplace is discovered automatically. Do not run `codex plugin marketplace add` for `~/.agents/plugins/marketplace.json`.

For a custom local marketplace, clone the repository to `<marketplace-root>/plugins/codex-chief-of-staff`, add the same plugin entry to `<marketplace-root>/.agents/plugins/marketplace.json`, then run:

```zsh
codex plugin marketplace add /absolute/path/to/marketplace-root
codex plugin add codex-chief-of-staff@<marketplace-name>
```

Start a new Codex task after installation so it loads the plugin snapshot.

## Set up the coordinator task

Create a projectless Codex task titled `Chief of Staff`. Invoke `$chief-of-staff` explicitly in its first prompt and pin the task if you want it to remain the main intake point. Copy [AGENTS.chief-of-staff.md](templates/AGENTS.chief-of-staff.md) into the task instructions for a durable coordinator charter.

The ledger defaults to `~/.codex/data/codex-chief-of-staff/state.db`. Initialize it with:

```zsh
./skills/chief-of-staff/scripts/chief-of-staff-state init
```

Use `--db PATH` for an alternate data location. The command writes compact JSON to stdout and returns JSON errors on stderr.

List open work after a restart:

```zsh
./skills/chief-of-staff/scripts/chief-of-staff-state work list --open
```

The coordinator refreshes every linked Codex task, repository, pull request, and check before it resumes from ledger state.

## Observe a GitHub pull request

The babysit skill includes a live, read-only watcher. It uses the authenticated `gh` CLI already available to the operator and makes one fixed GraphQL query scoped to the exact repository and PR:

```zsh
./skills/babysit-pr/scripts/watch-pr \
  --repo OWNER/REPO \
  --pr NUMBER \
  --expected-head HEAD_SHA \
  --verdict-head REVIEWED_SHA
```

The watcher returns one compact JSON result after a complete observation. It reports stale heads, conflicts, current-head review blockers, product gates, pending checks, deterministic failures, typed rate-limit failures, or merge-ready state. Live acquisition and schema failures return safe nonzero JSON errors instead. The command has a bounded subprocess timeout and retries only typed transport or rate-limit failures. It does not push, comment, change PR state, merge, or enable auto-merge. Missing permissions, stale approvals, and partial connections block `merge-ready`. See [the watcher contract](skills/babysit-pr/references/watcher.md) for precedence, evidence fields, and retry behavior.

## Development

Run the behavioral suite from the repository root:

```zsh
python3 -m unittest discover -s tests -v
```

Validate the plugin and each skill when the Codex system skills are installed:

```zsh
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
for skill_dir in skills/*; do
  python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py "$skill_dir"
done
```

See [AGENTS.md](AGENTS.md) for the repository layout, implementation contracts, and development constraints.
