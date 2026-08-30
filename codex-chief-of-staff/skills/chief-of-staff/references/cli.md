# Ledger CLI

The bundled `scripts/chief-of-staff-state` command writes to `~/.codex/data/codex-chief-of-staff/state.db` unless `--db PATH` is supplied.

Common commands:

```text
chief-of-staff-state init
chief-of-staff-state project put --id ID --name NAME [project fields]
chief-of-staff-state work create --id CS-N --title TITLE --mode MODE --authority AUTHORITY
chief-of-staff-state work show CS-N
chief-of-staff-state work list --open
chief-of-staff-state work transition CS-N --to STATUS --evidence JSON
chief-of-staff-state work promote CS-N --mode MODE --authority AUTHORITY --evidence JSON
chief-of-staff-state work set-head CS-N --head-sha SHA --evidence JSON
chief-of-staff-state dispatch record CS-N --task-id ID --role ROLE --environment ENV --brief-digest DIGEST
chief-of-staff-state gate open CS-N --id ID --question QUESTION
chief-of-staff-state gate resolve ID --answer ANSWER
chief-of-staff-state verdict record CS-N --id ID --head-sha SHA --verdict VERDICT --risk RISK --evidence JSON
chief-of-staff-state verdict current CS-N
chief-of-staff-state event append CS-N --type TYPE --payload JSON --idempotency-key KEY
chief-of-staff-state event list CS-N
```

Create delegated work with `--coordinator-task-id`. Dispatch recording fails without that callback target. A successful `dispatch record` response includes the coordinator task ID, work ID, and statuses that require a worker callback.

The command returns compact JSON on stdout. Contract or state errors return nonzero and write a JSON error object to stderr.
