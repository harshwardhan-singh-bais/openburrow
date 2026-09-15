"""Governance commands: audit, approvals, policy.

``burrow audit`` is the command this whole layer exists to make possible. It
answers a question none of the underlying protocols can: *who authorized this,
and who acted on whose behalf?* — reconstructed from the immutable ledger, not
from a live join that would break the moment a lane was pruned.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from openburrow.cli.context import CliContext, short_id, truncate
from openburrow.cli.output import (
    console,
    emit,
    failure,
    info,
    print_kv,
    section,
    severity_style,
    success,
    table,
    warn,
)
from openburrow.governance import PolicyGate

app = typer.Typer(help="Audit, approvals, and policy enforcement.", no_args_is_help=True)
approvals_app = typer.Typer(help="Respond to pending approvals.", no_args_is_help=True)
policy_app = typer.Typer(help="Inspect and test the policy gate.", no_args_is_help=True)
app.add_typer(approvals_app, name="approvals")
app.add_typer(policy_app, name="policy")


# ---------------------------------------------------------------------------
# burrow audit
# ---------------------------------------------------------------------------
@app.command("audit")
def audit(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")] = "",
    delegation: Annotated[
        str, typer.Option("--delegation", help="Trace one delegation chain in full.")
    ] = "",
    violations: Annotated[
        bool, typer.Option("--violations", help="Show only violations and critical events.")
    ] = False,
    strict: Annotated[
        bool, typer.Option("--strict", help="Show only cross-boundary records.")
    ] = False,
    export: Annotated[
        Path | None,
        typer.Option("--export", help="Write a compliance export to this path."),
    ] = None,
    fmt: Annotated[str, typer.Option("--format", help="jsonl|csv|html.")] = "jsonl",
) -> None:
    """Produce the accountability report for a session.

    Reads only from the immutable audit log, never from live tables — the report
    must be reproducible after the session's lanes and tasks have been pruned.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())

    if delegation:
        data = asyncio.run(client.call("governance.delegation_chain", delegation=delegation))
        emit(
            context,
            data,
            human_renderer=lambda d: console.print(d.get("report", "no chain found")),
        )
        return

    session_id = session or _latest_session(client)
    data = asyncio.run(
        client.call(
            "governance.audit",
            session=session_id,
            violations_only=violations,
            strict_only=strict,
        )
    )

    if export is not None:
        _write_export(export, data, fmt)
        success(f"wrote {fmt} export to {export}")

    def render(result: dict) -> None:
        report = result.get("report") or {}
        section("Accountability report")
        print_kv(
            {
                "session": short_id(str(report.get("session_id", "")), 14),
                "records": report.get("total_records", 0),
                "violations": report.get("violation_count", 0),
            }
        )

        by_actor = report.get("by_actor") or {}
        if by_actor:
            section("On behalf of")
            for actor, count in sorted(by_actor.items(), key=lambda kv: -kv[1]):
                console.print(f"  [cyan]{actor}[/cyan]  [dim]{count} record(s)[/dim]")

        by_boundary = report.get("by_boundary") or {}
        if by_boundary:
            section("Trust boundaries crossed")
            for boundary, count in sorted(by_boundary.items(), key=lambda kv: -kv[1]):
                console.print(f"  {boundary:<16} [dim]{count}[/dim]")

        records = result.get("records") or []
        if records:
            section("Records")
            table(
                ["seq", "event", "severity", "actor", "on behalf of", "allowed"],
                [
                    [
                        record.get("seq"),
                        record.get("event"),
                        f"[{severity_style(str(record.get('severity')))}]{record.get('severity')}[/]",
                        short_id(str(record.get("actor_lane") or ""), 10),
                        record.get("on_behalf_of") or "—",
                        "yes" if record.get("allowed") else "[red]no[/red]",
                    ]
                    for record in records[-40:]
                ],
            )

        if not records and not by_actor:
            info("no audit records for this session yet")

    emit(context, data, human_renderer=render)


def _write_export(path: Path, data: dict, fmt: str) -> None:
    """Write a compliance export.

    ``jsonl`` is the default because it streams and diffs; ``csv`` is there
    because auditors ask for it; the HTML form exists so a report can be handed
    over without also handing over a JSON parser.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    records = data.get("records") or []
    report = data.get("report") or {}

    if fmt == "csv":
        import csv

        with path.open("w", newline="", encoding="utf-8") as handle:
            fields = [
                "seq",
                "event",
                "severity",
                "trust_boundary",
                "actor_lane",
                "actor_harness",
                "on_behalf_of",
                "allowed",
                "reason",
                "summary",
                "at",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow({k: record.get(k) for k in fields})
        return

    if fmt == "html":
        rows = "\n".join(
            "<tr>"
            + "".join(
                f"<td>{_escape(str(record.get(key, '')))}</td>"
                for key in ("seq", "event", "severity", "on_behalf_of", "allowed", "summary")
            )
            + "</tr>"
            for record in records
        )
        path.write_text(
            "<!doctype html><meta charset='utf-8'>"
            "<title>OpenBurrow accountability report</title>"
            "<style>body{font:14px/1.5 system-ui;margin:2rem;color:#111}"
            "table{border-collapse:collapse;width:100%}"
            "th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left}"
            "th{background:#f6f6f6}</style>"
            f"<h1>Accountability report</h1><p>Session {report.get('session_id', '')} · "
            f"{report.get('total_records', 0)} records · "
            f"{report.get('violation_count', 0)} violations</p>"
            "<table><thead><tr><th>seq</th><th>event</th><th>severity</th>"
            "<th>on behalf of</th><th>allowed</th><th>summary</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>",
            encoding="utf-8",
        )
        return

    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str) + "\n")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# burrow approvals
# ---------------------------------------------------------------------------
@approvals_app.command("list")
def approvals_list(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    all_sessions: Annotated[bool, typer.Option("--all", help="Across every open session.")] = False,
) -> None:
    """List pending approvals."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = "" if all_sessions else (session or _latest_session(client))
    pending = asyncio.run(client.call("approvals.list", session=session_id))

    def render(data: list[dict]) -> None:
        if not data:
            success("no approvals waiting")
            return
        for approval in data:
            risk = str(approval.get("risk_tier", "medium"))
            style = {"critical": "bold red", "high": "red", "medium": "yellow"}.get(risk, "dim")
            console.print(
                f"\n[{style}]{risk.upper():<8}[/{style}] "
                f"[cyan]{short_id(str(approval.get('id', '')))}[/cyan]  "
                f"[dim]{approval.get('lane_id', '')}[/dim]"
            )
            console.print(
                f"  action: [bold]{truncate(str(approval.get('action', '')), 100)}[/bold]"
            )
            if approval.get("reason"):
                console.print(f"  reason: [dim]{approval['reason']}[/dim]")
            if approval.get("affected_paths"):
                console.print(f"  paths:  [dim]{', '.join(approval['affected_paths'][:5])}[/dim]")

    emit(context, pending, human_renderer=render)


@approvals_app.command("respond")
def approvals_respond(
    ctx: typer.Context,
    approval: Annotated[str, typer.Argument(help="Approval id.")],
    approve: Annotated[bool, typer.Option("--approve", help="Allow the action.")] = False,
    deny: Annotated[bool, typer.Option("--deny", help="Reject the action.")] = False,
    edit: Annotated[
        str, typer.Option("--edit", help="Approve a modified version of the action.")
    ] = "",
    note: Annotated[str, typer.Option("--note", help="Reason, recorded in the audit log.")] = "",
) -> None:
    """Approve, deny, or approve-with-edits.

    Any authorized teammate may respond, not only the task owner. A blocked lane
    waiting on someone who went to lunch is a worse failure than an extra person
    being able to unblock it — and the audit record captures who answered either
    way.
    """
    context: CliContext = ctx.obj
    if not (approve or deny or edit):
        failure("pass --approve, --deny, or --edit")
        raise typer.Exit(code=1)

    client = asyncio.run(context.require_daemon())
    data = asyncio.run(
        client.call(
            "approvals.respond",
            approval=approval,
            approve=approve and not deny,
            deny=deny,
            edited_action=edit,
            note=note,
            by=context.config().settings.governance_human_id,
        )
    )

    def render(result: dict) -> None:
        if result.get("status") == "denied":
            warn("denied")
        elif result.get("edited"):
            success("approved with edits")
        else:
            success("approved")

    emit(context, data, human_renderer=render)


# ---------------------------------------------------------------------------
# burrow policy
# ---------------------------------------------------------------------------
@policy_app.command("show")
def policy_show(ctx: typer.Context) -> None:
    """Show the effective policy, and any setting that would do nothing."""
    context: CliContext = ctx.obj
    config = context.config()
    policy = config.policy
    notes = PolicyGate(policy).diagnose()

    def render(data: dict) -> None:
        section("Policy")
        print_kv(
            {
                "enforce": data.get("enforce"),
                "default action": data.get("default_action"),
                "allowed commands": data.get("allowed_commands") or "—",
                "denied commands": data.get("denied_commands") or "—",
                "denied paths": data.get("denied_paths") or "—",
            }
        )
        tiers = data.get("risk_tiers") or {}
        if tiers:
            section("Risk tiers")
            for tier, patterns in tiers.items():
                style = {"critical": "bold red", "high": "red", "medium": "yellow"}.get(tier, "dim")
                console.print(f"  [{style}]{tier:<10}[/{style}] {', '.join(patterns[:4])}")
        overrides = data.get("role_overrides") or {}
        if overrides:
            section("Role overrides")
            for role, override in overrides.items():
                console.print(f"  [cyan]{role}[/cyan] [dim]{override}[/dim]")
        if notes:
            section("Configuration notes")
            for note in notes:
                warn(note)

    emit(
        context,
        {**policy.model_dump(mode="json"), "diagnostics": list(notes)},
        human_renderer=render,
    )


@policy_app.command("test")
def policy_test(
    ctx: typer.Context,
    command: Annotated[str, typer.Argument(help="Command to test, e.g. 'git push --force'.")],
    path: Annotated[
        str, typer.Option("--path", help="Repo-relative path the command touches.")
    ] = "",
    role: Annotated[
        str, typer.Option("--role", help="Evaluate as this lane role, applying role_overrides.")
    ] = "",
) -> None:
    """Dry-run the policy gate against a command without executing it.

    This is what makes a policy change reviewable: you can see what a rule would
    block before it blocks something in a real session.

    The rules are not implemented here. They live in
    :class:`openburrow.governance.policy.PolicyGate`, which is the same object the
    daemon consults before it starts a lane. Inlining them in this command is how
    they came to disagree with their own documentation: four defects, none of
    which any test of this command could have caught, because the thing under
    test was a report and the thing that mattered was a gate.
    """
    context: CliContext = ctx.obj
    config = context.config()

    gate = PolicyGate(config.policy)
    verdict = gate.check(command, path=path, role=role or None)
    payload = {
        **verdict.as_dict(),
        "enforce": gate.enforce,
    }

    def render(data: dict) -> None:
        if data["action"] == "allow":
            success(f"allowed  [{data['risk_tier']} risk]  ({data['matched_rule']})")
        else:
            failure(f"denied  [{data['risk_tier']} risk]  ({data['matched_rule']})")
            if not data["enforce"]:
                warn("policy.enforce is false, so this denial is advisory only")
        if data["would_require_approval"]:
            warn("this would pause for human approval before running")
        if data.get("denied_paths"):
            console.print(f"  [dim]denied paths: {', '.join(data['denied_paths'])}[/dim]")
        for note in data.get("diagnostics") or ():
            warn(note)

    emit(context, payload, human_renderer=render)


def _latest_session(client) -> str:
    sessions = asyncio.run(client.call("session.list", open_only=True))
    if not sessions:
        failure("no open sessions")
        raise typer.Exit(code=1)
    return str(sessions[0]["id"])


__all__ = ["app", "approvals_app", "policy_app"]
