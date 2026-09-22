"""The ``burrow`` entry point.

This module does four things and nothing else:

1. Declares the global flags and turns them into one
   :class:`~openburrow.cli.context.CliContext`.
2. Mounts the command modules so ``burrow --help`` is the whole map.
3. Maps :class:`~openburrow.core.errors.OpenBurrowError` subclasses onto
   distinct process exit codes, so a shell script or CI job can branch on
   *why* something failed rather than grepping stderr.
4. Makes sure no traceback ever reaches a user who did not ask for one.

The exit-code table is the part worth reading. A governance refusal and a
configuration typo both exit non-zero, but they are not the same event: the
first is the system working correctly, the second is the system being set up
wrong. Collapsing them into ``1`` would make a CI job that asserts "no
governance violations occurred" impossible to write.

Usage::

    burrow --help
    burrow init
    burrow doctor --fix
    burrow session start --name spike --template pair
    burrow observability watch
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from openburrow.cli import commands
from openburrow.cli.context import CliContext, build_context
from openburrow.cli.output import info, render_error
from openburrow.core.errors import OpenBurrowError
from openburrow.core.version import __version__

# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_DAEMON = 4
EXIT_APPROVAL = 5
EXIT_GOVERNANCE = 6
EXIT_PROTOCOL = 7
EXIT_HARNESS = 8
EXIT_NOT_FOUND = 9
EXIT_INTERRUPTED = 130

#: Error ``code`` → process exit code.
#:
#: Keyed on the stable machine code rather than the exception class, because the
#: code is what crosses the wire. A daemon-side refusal arrives at the CLI as a
#: remote error carrying the same code, and it should land on the same exit code
#: as the in-process equivalent.
EXIT_CODES: dict[str, int] = {
    # Setup problems — the user has something to fix.
    "openburrow.repo_not_initialized": EXIT_CONFIG,
    "openburrow.config_error": EXIT_CONFIG,
    "openburrow.config_schema_error": EXIT_CONFIG,
    # Lifecycle problems — the daemon is up when it should be down, or vice versa.
    "openburrow.daemon_not_running": EXIT_DAEMON,
    "openburrow.daemon_already_running": EXIT_DAEMON,
    # A human is needed and has not answered yet. Not an error the user made.
    "openburrow.approval_required": EXIT_APPROVAL,
    "openburrow.approval_timeout": EXIT_APPROVAL,
    # Governance said no. This is the system working.
    "openburrow.authority_scope_violation": EXIT_GOVERNANCE,
    "openburrow.silent_authority_creep": EXIT_GOVERNANCE,
    "openburrow.redelegation_denied": EXIT_GOVERNANCE,
    "openburrow.impersonation_detected": EXIT_GOVERNANCE,
    "openburrow.policy_violation": EXIT_GOVERNANCE,
    "openburrow.injection_error": EXIT_GOVERNANCE,
    "openburrow.governance_error": EXIT_GOVERNANCE,
    # Protocol-level failures — a peer, a card, or a performative sequence.
    "openburrow.a2a_illegal_transition": EXIT_PROTOCOL,
    "openburrow.a2a_protocol_error": EXIT_PROTOCOL,
    "openburrow.a2a_conformance_error": EXIT_PROTOCOL,
    "openburrow.acp_negotiation_error": EXIT_PROTOCOL,
    "openburrow.protocol_error": EXIT_PROTOCOL,
    # Harness-side failures — a binary is missing, crashed, or spoke nonsense.
    "openburrow.harness_not_found": EXIT_HARNESS,
    "openburrow.harness_crashed": EXIT_HARNESS,
    "openburrow.adapter_error": EXIT_HARNESS,
    "openburrow.output_parse_error": EXIT_HARNESS,
    # Named things that do not exist.
    "openburrow.session_not_found": EXIT_NOT_FOUND,
    "openburrow.lane_not_found": EXIT_NOT_FOUND,
    "openburrow.claim_conflict": EXIT_NOT_FOUND,
}


def exit_code_for(error: BaseException) -> int:
    """Resolve the process exit code for an error.

    Falls back to :data:`EXIT_FAILURE` rather than guessing, so an unmapped code
    is visibly generic instead of silently masquerading as a governance refusal.
    """
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return EXIT_CODES.get(code, EXIT_FAILURE)
    return EXIT_FAILURE


# --------------------------------------------------------------------------
# Help text
# --------------------------------------------------------------------------
HELP = """\
Terminal-native, protocol-grounded multi-harness agent collaboration.

OpenBurrow runs several coding agents side by side in isolated git worktrees and makes them talk to each other over [bold]A2A[/bold] — the same protocol they would use to talk to any other agent on the network. Tool access goes through [bold]MCP[/bold], untouched. Negotiation uses [bold]ACP[/bold] performatives. And because none of those three protocols can answer "who authorised this, and what were they allowed to do", OpenBurrow adds a governance layer that does.

[dim]Run `burrow doctor` first. It tells you what is installed and what is not.[/dim]
"""

EPILOG = """\
[bold]Getting started[/bold]
  burrow init                       create openburrow.yaml and seed AGENTS.md
  burrow doctor                     check the environment end to end
  burrow session start              bring up a session and its lanes
  burrow observability watch        follow what the lanes are doing
  burrow radar scan                 see where two lanes are about to collide

[bold]When something is wrong[/bold]
  burrow doctor --fix               repair what can be repaired automatically
  burrow daemon status              is the daemon actually up?
  burrow observability logs -f      read the append-only event log

[bold]Documentation[/bold]
  docs/architecture/overview.md  how the pieces fit together
  docs/protocols/                the A2A, MCP, and ACP bindings we implement
  docs/governance/               the delegation model and its invariants
"""

# --------------------------------------------------------------------------
# Root app
# --------------------------------------------------------------------------
app = typer.Typer(
    name="burrow",
    help=HELP,
    epilog=EPILOG,
    no_args_is_help=True,
    # Without this, Click rejects `burrow --version` with "Missing command": the
    # group has arguments, so no_args_is_help does not fire, but there is no
    # subcommand for the group to dispatch to. Every flag on the callback is
    # unusable without it — including --version, which is the one people type
    # before they have read anything.
    invoke_without_command=True,
    add_completion=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)

#: The context built by the callback, kept here so the error boundary in
#: :func:`main` can honour ``--json`` when it renders a failure.
#:
#: Click gives the callback the parsed flags and gives the exception handler the
#: exception, but never both at once. Rather than re-parse ``sys.argv`` (which
#: would drift from what Click actually did) or swallow the error inside the
#: callback (which would lose the exit code), we remember the one context we
#: built. It is process-local, single-valued, and written exactly once per run.
_active_context: CliContext | None = None


def _absorb(target: typer.Typer, source: typer.Typer) -> None:
    """Move a sub-app's commands and groups onto ``target``.

    ``burrow init`` reads better than ``burrow workspace init``, so the workspace
    module — which is where the standalone verbs live — is flattened instead of
    nested. Its two real groups (``config``, ``adapters``) come along with it and
    stay groups, because ``burrow config show`` genuinely is a two-level verb.

    Mounting the app as a group and *also* absorbing it would register every
    command twice, so callers use one or the other, never both.
    """
    target.registered_commands.extend(source.registered_commands)
    target.registered_groups.extend(source.registered_groups)


@app.callback()
def _root(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON on stdout. Errors are JSON too."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Print only the essential line of output."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Debug-level logging on stderr."),
    ] = False,
    no_daemon: Annotated[
        bool,
        typer.Option(
            "--no-daemon",
            help="Never contact the daemon. Commands that need it will fail instead of trying.",
        ),
    ] = False,
    assume_yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Answer yes to confirmation prompts."),
    ] = False,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Operate on this repository instead of walking up from the current directory.",
            file_okay=False,
            resolve_path=True,
        ),
    ] = None,
    show_version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Print the version and exit."),
    ] = False,
) -> None:
    """Global options. Every subcommand inherits the context this builds."""
    global _active_context  # noqa: PLW0603 - one process-wide Typer context

    if show_version:
        typer.echo(f"burrow {__version__}")
        raise typer.Exit(code=EXIT_OK)

    if json_output and quiet:
        # Not merely redundant: OutputMode is one value, so accepting both would
        # mean silently discarding one of the flags the user typed.
        typer.echo("--json and --quiet are mutually exclusive", err=True)
        raise typer.Exit(code=EXIT_USAGE)

    if repo is not None and not repo.exists():
        typer.echo(f"--repo does not exist: {repo}", err=True)
        raise typer.Exit(code=EXIT_USAGE)

    _active_context = build_context(
        json_output=json_output,
        quiet=quiet,
        verbose=verbose,
        no_daemon=no_daemon,
        assume_yes=assume_yes,
        repo_root=repo,
    )
    ctx.obj = _active_context

    if ctx.invoked_subcommand is None:
        # Flags but no command. `no_args_is_help` covers the truly empty case;
        # this covers `burrow --json`, which would otherwise print nothing at all
        # and exit zero — a command that silently succeeds at doing nothing is
        # the worst kind of success.
        typer.echo(ctx.get_help())
        raise typer.Exit(code=EXIT_USAGE)


# --- mounts ---------------------------------------------------------------
# Workspace is flattened: init / doctor / version are top-level verbs, while its
# config and adapters groups ride along as groups.
_absorb(app, commands.workspace.app)

# Everything else is a genuine noun and stays a group.
app.add_typer(commands.daemon.app, name="daemon")
app.add_typer(commands.session.app, name="session")
app.add_typer(commands.coordination.app, name="coordination")
app.add_typer(commands.governance.app, name="governance")
app.add_typer(commands.knowledge.app, name="knowledge")
app.add_typer(commands.observability.app, name="observability")
app.add_typer(commands.radar.app, name="radar")
app.add_typer(commands.integrations.app, name="hook")
app.add_typer(commands.auth.app, name="login")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def _report(error: OpenBurrowError) -> int:
    """Render a failure in the mode the user asked for, and return its exit code."""
    if _active_context is not None and _active_context.is_json:
        payload = {"ok": False, "error": error.to_dict()}
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
        sys.stdout.flush()
    else:
        render_error(error)
    return exit_code_for(error)


def main() -> None:
    """Console-script entry point.

    ``OpenBurrowError`` is the only exception class we render ourselves. Anything
    else is a bug in OpenBurrow, and the right thing to do with a bug is let the
    traceback through — a friendly "something went wrong" message would hide the
    one piece of information that makes it fixable.
    """
    try:
        app(prog_name="burrow")
    except OpenBurrowError as exc:
        raise SystemExit(_report(exc)) from None
    except KeyboardInterrupt:
        if _active_context is not None and not _active_context.is_quiet:
            info("interrupted")
        raise SystemExit(EXIT_INTERRUPTED) from None


if __name__ == "__main__":
    main()
