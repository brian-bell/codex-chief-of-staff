from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .state import Ledger, StateError, json_line


DEFAULT_DB = Path.home() / ".codex" / "data" / "codex-chief-of-staff" / "state.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chief-of-staff-state")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")

    work = commands.add_parser("work")
    work_commands = work.add_subparsers(dest="work_command", required=True)
    create = work_commands.add_parser("create")
    create.add_argument("--id", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--mode", required=True)
    create.add_argument("--authority", required=True)
    create.add_argument("--project-id")
    create.add_argument("--coordinator-task-id")
    show = work_commands.add_parser("show")
    show.add_argument("id")
    list_work = work_commands.add_parser("list")
    list_work.add_argument("--open", action="store_true")
    transition = work_commands.add_parser("transition")
    transition.add_argument("id")
    transition.add_argument("--to", required=True)
    transition.add_argument("--evidence", required=True, type=json.loads)
    promote = work_commands.add_parser("promote")
    promote.add_argument("id")
    promote.add_argument("--mode", required=True)
    promote.add_argument("--authority", required=True)
    promote.add_argument("--evidence", required=True, type=json.loads)
    set_head = work_commands.add_parser("set-head")
    set_head.add_argument("id")
    set_head.add_argument("--head-sha", required=True)
    set_head.add_argument("--evidence", required=True, type=json.loads)

    dispatch = commands.add_parser("dispatch")
    dispatch_commands = dispatch.add_subparsers(dest="dispatch_command", required=True)
    record = dispatch_commands.add_parser("record")
    record.add_argument("work_id")
    record.add_argument("--task-id", required=True)
    record.add_argument("--role", required=True)
    record.add_argument("--host-id")
    record.add_argument("--environment", required=True)
    record.add_argument("--brief-digest", required=True)

    verdict = commands.add_parser("verdict")
    verdict_commands = verdict.add_subparsers(dest="verdict_command", required=True)
    record_verdict = verdict_commands.add_parser("record")
    record_verdict.add_argument("work_id")
    record_verdict.add_argument("--id", required=True)
    record_verdict.add_argument("--head-sha", required=True)
    record_verdict.add_argument("--verdict", required=True)
    record_verdict.add_argument("--risk", required=True)
    record_verdict.add_argument("--reviewer-task-id")
    record_verdict.add_argument("--evidence", required=True, type=json.loads)
    current_verdict = verdict_commands.add_parser("current")
    current_verdict.add_argument("work_id")

    gate = commands.add_parser("gate")
    gate_commands = gate.add_subparsers(dest="gate_command", required=True)
    open_gate = gate_commands.add_parser("open")
    open_gate.add_argument("work_id")
    open_gate.add_argument("--id", required=True)
    open_gate.add_argument("--question", required=True)
    resolve_gate = gate_commands.add_parser("resolve")
    resolve_gate.add_argument("id")
    resolve_gate.add_argument("--answer", required=True)

    event = commands.add_parser("event")
    event_commands = event.add_subparsers(dest="event_command", required=True)
    append_event = event_commands.add_parser("append")
    append_event.add_argument("work_id")
    append_event.add_argument("--type", required=True)
    append_event.add_argument("--payload", required=True, type=json.loads)
    append_event.add_argument("--idempotency-key", required=True)
    append_event.add_argument("--source-task-id")
    list_events = event_commands.add_parser("list")
    list_events.add_argument("work_id")

    project = commands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    put_project = project_commands.add_parser("put")
    put_project.add_argument("--id", required=True)
    put_project.add_argument("--name", required=True)
    put_project.add_argument("--codex-project-id")
    put_project.add_argument("--repository")
    put_project.add_argument("--source-control")
    put_project.add_argument("--default-branch")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = Ledger(args.db)
    try:
        if args.command == "init":
            result = ledger.initialize()
        elif args.command == "project":
            result = ledger.put_project(
                project_id=args.id,
                name=args.name,
                codex_project_id=args.codex_project_id,
                repository=args.repository,
                source_control=args.source_control,
                default_branch=args.default_branch,
            )
        elif args.command == "dispatch":
            result = ledger.record_dispatch(
                work_id=args.work_id,
                task_id=args.task_id,
                role=args.role,
                host_id=args.host_id,
                environment=args.environment,
                brief_digest=args.brief_digest,
            )
        elif args.command == "verdict":
            if args.verdict_command == "record":
                result = ledger.record_verdict(
                    verdict_id=args.id,
                    work_id=args.work_id,
                    head_sha=args.head_sha,
                    verdict=args.verdict,
                    risk=args.risk,
                    reviewer_task_id=args.reviewer_task_id,
                    evidence=args.evidence,
                )
            else:
                result = ledger.current_verdict(args.work_id)
        elif args.command == "gate":
            if args.gate_command == "open":
                result = ledger.open_gate(
                    gate_id=args.id, work_id=args.work_id, question=args.question
                )
            else:
                result = ledger.resolve_gate(gate_id=args.id, answer=args.answer)
        elif args.command == "event":
            if args.event_command == "append":
                result = ledger.append_event(
                    work_id=args.work_id,
                    event_type=args.type,
                    payload=args.payload,
                    idempotency_key=args.idempotency_key,
                    source_task_id=args.source_task_id,
                )
            else:
                result = ledger.list_events(args.work_id)
        elif args.work_command == "create":
            result = ledger.create_work_order(
                work_id=args.id,
                title=args.title,
                mode=args.mode,
                authority=args.authority,
                project_id=args.project_id,
                coordinator_task_id=args.coordinator_task_id,
            )
        elif args.work_command == "show":
            result = ledger.show_work_order(args.id)
        elif args.work_command == "list":
            result = ledger.list_work_orders(open_only=args.open)
        elif args.work_command == "transition":
            result = ledger.transition_work_order(
                work_id=args.id, target=args.to, evidence=args.evidence
            )
        elif args.work_command == "promote":
            result = ledger.promote_work_order(
                work_id=args.id,
                mode=args.mode,
                authority=args.authority,
                evidence=args.evidence,
            )
        else:
            result = ledger.set_head(
                work_id=args.id, head_sha=args.head_sha, evidence=args.evidence
            )
    except (OSError, sqlite3.Error, StateError) as exc:
        print(json_line({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json_line(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
