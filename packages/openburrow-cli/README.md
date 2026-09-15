# openburrow-cli

The `burrow` command: a thin client over the daemon's control socket, plus the
handful of verbs that work with no daemon at all.

## Two rules that shape everything

The command modules look repetitive, and that is on purpose. Two rules are
enforced everywhere, and both exist because the alternative produces bugs that
are invisible in review:

**1. Commands never print.** A command function returns a value or raises. It
does not call `typer.echo`. Printing happens in exactly one place — `output.py`
— which is what makes `--json` work everywhere without a single `if json:`
branch in a command. The moment one command prints directly, `--json` becomes a
lie for that command, and nobody notices until a script parses it.

**2. Commands never construct their own context.** `cli/context.py` builds the
`CliContext` — resolved config, daemon client, paths, output mode — and passes
it in. A command that builds its own config would resolve paths differently
depending on the working directory, which is how you get two commands
disagreeing about which repo they are operating on.

## Exit codes are semantic

`0` and `1` are not enough for a CLI that scripts against. The table lives in
`main.py` and is keyed on the error's own `code`, not on its exception class, so
a new error type inherits sensible behaviour by choosing a code rather than by
being remembered in a mapping:

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | generic failure |
| `2` | usage error (bad flags, missing subcommand) |
| `3` | config / repo not initialised |
| `4` | daemon not running |
| `5` | approval required |
| `6` | governance refusal (authority scope, silent creep) |
| `7` | protocol error (illegal A2A transition, illegal ACP performative) |
| `8` | harness error (not found, crashed) |
| `9` | not found |
| `130` | interrupted |

The practical payoff: `burrow session close` returning `6` means a human
deliberately refused something, which a CI script can distinguish from a crash
(`1`) or a missing daemon (`4`). Those want different responses.

## The command tree is flattened on purpose

Workspace verbs are absorbed into the root rather than nested under a group:

```python
_absorb(app, commands.workspace.app)  # burrow init, not burrow workspace init
```

`burrow init` is the first command anyone types and `burrow workspace init` is
a worse version of it. The other groups (`daemon`, `session`, `lane`, `bus`,
`gov`, `brain`, `radar`, `reel`, `adapters`, `relay`) stay nested because they
genuinely are namespaces — `burrow lane start` reads correctly and
`burrow start` would not.

## `--version` needs `invoke_without_command`

A Click group with `no_args_is_help=True` and arguments does not help-and-exit;
it dispatches. With no subcommand present, `burrow --version` therefore failed
with "Missing command" — which is a confusing thing to see when you asked for a
version. The fix is `invoke_without_command=True` plus an explicit branch:

```python
if ctx.invoked_subcommand is None:
    typer.echo(ctx.get_help())
    raise typer.Exit(code=EXIT_USAGE)
```

Exit `2` rather than `0`, because a bare `burrow` with no verb is a usage error,
and a script that ran it by accident should not see success.

## The TUI polls; it does not stream

`burrow watch` renders a Textual dashboard of lanes and the bus feed. It polls
`session.show` and `bus.tail` once a second rather than subscribing to
`bus.stream`.

That is a deliberate reversal of the obvious design. A subscription is more
elegant, but it also means the TUI's view is *only* as correct as its event
handling — miss one event and the pane is silently wrong until restart. Polling
by sequence number is self-healing: the next tick reconciles regardless of what
was missed, and the failure mode of a dropped tick is a stale frame rather than
a wrong one. For a dashboard a human glances at, that trade is correct.

Row updates use `update_row` with a `RowDoesNotExist` fallback rather than
probing for the row's index first — the index lookup was fragile and read badly.

## What it does not cover

- **It holds no state.** Every command is one request and one response against
  the daemon. Kill the CLI mid-command and nothing is corrupted.
- **It does not spawn harnesses itself.** `--no-daemon` runs a short-lived
  in-process path for the verbs that can work that way, but lane supervision is
  always the daemon's.
- **It is not a scripting language.** There is no `burrow run <script>`. If you
  want to sequence commands, `--json` plus a shell or Python script is the
  intended path, and the exit codes above are the contract.

## Layout

| Module | Responsibility |
| --- | --- |
| `main.py` | Typer root, exit-code table, error boundary, group mounting |
| `context.py` | `CliContext` construction — the only place config is resolved |
| `output.py` | The only place that prints. JSON and human renderers |
| `tui.py` | `BurrowTui`, the `burrow watch` dashboard |
| `commands/` | Seven verb families, one module each |

## See also

- [docs/cli/README.md](../../docs/cli/README.md) — the full verb reference
- [openburrow-daemon](../openburrow-daemon/README.md) — the other end of the socket
