"""The ``burrow`` command-line surface.

This package is the only place in OpenBurrow that talks to a human by default.
Everything below it — the daemon, the protocols, the governance ledger — is
designed to be driven by another process, so the CLI's job is to make the
control plane legible rather than to hold any state of its own.

Two rules shape the layout:

* **Commands never print directly.** They build a payload and hand it to
  :func:`openburrow.cli.output.emit`, which decides between human and JSON
  rendering. That is why ``--json`` cannot drift out of sync with the normal
  output: there is exactly one place where the decision is made.
* **Commands never construct their own context.** :func:`openburrow.cli.main.main`
  builds one :class:`~openburrow.cli.context.CliContext` per invocation and
  attaches it to ``ctx.obj``. Config is loaded once, the daemon client is created
  once, and the output mode is resolved once.

The TUI (:mod:`openburrow.cli.tui`) is deliberately *not* the primary surface.
It is a convenience over the same IPC calls the CLI makes, so anything it can
show you, ``burrow watch`` or ``burrow report`` can also show you — including
when you are on a terminal that cannot run a full-screen app.
"""

from openburrow.core.version import __version__

__all__ = ["__version__"]
