# Initialize Chief of Staff

This file is an installer for Codex. Follow it instead of summarizing it.

The finished setup has an installed and enabled `codex-chief-of-staff` plugin, an initialized ledger, and one pinned projectless task titled `Chief of Staff`. Preserve existing work and configuration throughout setup.

## 1. Check the machine

Confirm that `git`, `codex`, and Python 3.11 or newer are available. Stop with the missing requirement if any check fails.

Use these default paths:

```text
Plugin checkout: ~/plugins/codex-chief-of-staff
Marketplace file: ~/.agents/plugins/marketplace.json
Ledger: ~/.codex/data/codex-chief-of-staff/state.db
```

## 2. Install or refresh the checkout

If the plugin checkout does not exist, clone `https://github.com/brian-bell/codex-chief-of-staff.git` there. Record that the checkout changed.

If it already exists and is a Git checkout, confirm that its origin is this repository and inspect its branch and worktree. Pull `origin/main` with `--ff-only` when the checkout is clean. Do not overwrite local changes. If the checkout is dirty or cannot fast-forward, stop and report the exact conflict.

If the directory exists but is not a Git checkout, inspect `.codex-plugin/plugin.json`. Stop if the manifest is missing or its plugin name is not `codex-chief-of-staff`. For a matching legacy copy, move the directory to an unused sibling path named `codex-chief-of-staff.backup-<UTC timestamp>`, then clone the repository into the default path. Keep the backup and report its path. Do not delete it during initialization.

## 3. Register the personal marketplace

Read the marketplace file before changing it. Preserve every existing marketplace field and plugin entry. If it is missing, create this minimal marketplace with the plugin entry below in its `plugins` array:

```json
{
  "name": "personal",
  "interface": {
    "displayName": "Personal"
  },
  "plugins": []
}
```

Ensure its `plugins` array contains this entry exactly once:

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

The `personal` marketplace is discovered automatically. Do not add it with `codex plugin marketplace add`.

## 4. Install the plugin snapshot

Run `codex plugin list` and inspect the installed path and version.

- If the plugin is not installed, run `codex plugin add codex-chief-of-staff@personal`.
- If the plugin is installed from this checkout, run `codex plugin remove codex-chief-of-staff@personal`, then `codex plugin add codex-chief-of-staff@personal`. An explicit initializer run always rebuilds the installed snapshot from the checkout.
- If the same plugin name points somewhere else, stop and ask before replacing it.

Confirm that `codex plugin list` reports the plugin as installed and enabled.

## 5. Initialize the ledger

From the plugin checkout, run:

```zsh
./skills/chief-of-staff/scripts/chief-of-staff-state init
```

Then list open work:

```zsh
./skills/chief-of-staff/scripts/chief-of-staff-state work list --open
```

Do not reset or replace an existing ledger.

## 6. Open the desk

Check the Codex task list for a projectless coordinator task already linked as `coordinator_task_id` by open work orders. Reuse that exact task when it exists. Do not strand active callbacks by creating a replacement.

If no open work names a coordinator, inspect existing projectless tasks for a dedicated Chief. A candidate must clearly invoke `$chief-of-staff` as its coordinator role; a development task that merely mentions the plugin does not qualify. Reuse and pin the only matching task. If several tasks qualify, ask the user which one to keep instead of guessing.

Create a projectless task titled `Chief of Staff` only when no existing task qualifies. Create it after the plugin installation completes. Its first prompt must invoke `$chief-of-staff` explicitly and include the full charter from `templates/AGENTS.chief-of-staff.md`. Pin the task. Use the Codex task tools when available; otherwise give the user these final manual steps.

Finish by opening the Chief task and reporting whether the checkout, plugin snapshot, ledger, and coordinator task were created, refreshed, or reused. The user works through that task from then on.
