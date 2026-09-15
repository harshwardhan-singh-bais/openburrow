# OpenBurrow documentation

Start with the root [README](../README.md) if you have not read it. It gives the
short version and the architecture diagram. Everything here goes deeper.

## If you want to…

| Goal | Read |
|---|---|
| Understand how the pieces fit | [architecture/overview.md](architecture/overview.md) |
| Know why a decision was made | [architecture/adr/](architecture/adr/) |
| Implement or consume the A2A binding | [protocols/a2a.md](protocols/a2a.md) |
| Understand what "MCP passthrough" rules out | [protocols/mcp.md](protocols/mcp.md) |
| Understand negotiation semantics | [protocols/acp.md](protocols/acp.md) |
| Understand the delegation model | [governance/model.md](governance/model.md) |
| Know what is built and what is next | [roadmap/README.md](roadmap/README.md) |
| Use the CLI | [cli/README.md](cli/README.md) |
| Add a harness we do not support | [adapters/authoring.md](adapters/authoring.md) |
| Deploy for a team | [deployment/README.md](deployment/README.md) |
| Work on the web frontend | [web/README.md](web/README.md) |
| Browse the decision records | [architecture/adr/](architecture/adr/) — ten ADRs, numbered, each with what was rejected |

Package-level READMEs live next to the code they describe and are the place to
look for the *module* view rather than the *system* view: `packages/*/README.md`.
The most useful ones to start with are
[openburrow-governance](../packages/openburrow-governance/README.md) and
[openburrow-relay](../packages/openburrow-relay/README.md), because both are
mostly explanation of what they deliberately refuse to do.

## The four documents worth reading in order

If you are going to spend an hour on this codebase, spend it here:

1. **[architecture/overview.md](architecture/overview.md)** — the components and,
   more importantly, the invariants between them. Most of the design is in the
   invariants; the components are almost obvious once you know them.
2. **[governance/model.md](governance/model.md)** — the reason the project exists.
   Four invariants, each with the code that enforces it.
3. **[protocols/acp.md](protocols/acp.md)** — why negotiation is a typed exchange
   rather than agents being polite at each other, and why an illegal move
   escalates instead of being repaired.
4. **[roadmap/README.md](roadmap/README.md)** — 22 stages, 258 items, in build
   order. Read this before proposing a change; the thing you want may already have
   a decided place in the sequence.

## Conventions used throughout

**Every module docstring states the tradeoff, not the contents.** You can tell a
documented module from an undocumented one by whether the docstring explains what
was *rejected*. A docstring that lists methods is a table of contents; a docstring
that explains why a method does not exist is design.

**Comments explain why, never what.** `# increment the counter` is noise.
`# ULIDs sort at millisecond granularity, which is not fine-grained enough for a
bus sequence` is the reason the code looks unusual, and it is the thing that stops
someone "simplifying" it later.

**Honest failure over convenient success.** Several places in this codebase return
"unknown" rather than a plausible answer: the adapter's `parse_usage` returns `{}`
instead of estimating tokens, the Radar's judge distinguishes "no conflict" from
"could not tell", the anchor checker reports `unknown` rather than `fresh`. This is
a consistent position, not a series of coincidences — see
[architecture/overview.md](architecture/overview.md#the-honesty-rule).
