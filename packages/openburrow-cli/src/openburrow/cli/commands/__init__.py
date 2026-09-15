"""Command modules, one per verb family.

Each module owns a slice of the CLI and exposes a ``typer.Typer`` app named
``app``. Nested groups (``config``, ``adapters``, ``plan``, ``lane``,
``brain``, ``lessons``, ``approvals``, ``policy``) are attached to their parent
*inside* the module that defines them, so the file you open to add a command is
the file that decides where the command appears.

:mod:`openburrow.cli.main` is the only place that mounts these apps onto the
root command. Keeping that in one file means ``burrow --help`` is a complete
description of the tool — you never have to go looking for a hidden mount.

The modules are imported eagerly. That is a deliberate trade: mounting a Typer
app requires the object, so deferring the import would only move the cost, not
remove it. The two genuinely expensive imports in here — ``openburrow.daemon``
and ``textual`` — are both deferred to the moment their command runs, which is
why ``burrow --help`` stays fast.
"""

from openburrow.cli.commands import (
    coordination,
    daemon,
    governance,
    knowledge,
    observability,
    session,
    workspace,
)

__all__ = [
    "coordination",
    "daemon",
    "governance",
    "knowledge",
    "observability",
    "session",
    "workspace",
]
