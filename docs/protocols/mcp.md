# MCP passthrough

OpenBurrow does not mediate MCP. Each lane's harness connects to MCP servers
directly, using its own configuration. There is no proxy, no inspection of tool
calls, and no policy check in the MCP path.

This document explains what that rules out, because a decision about what *not* to
build is only useful if the consequence is stated.

## What passthrough means

```
lane process
├── harness (claude-code, codex, opencode, …)
│   └── MCP client  ──────────►  MCP servers          ← direct, untouched
└── burrow adapter
    └── reads harness output, writes prompts          ← this is all we do
```

The adapter's job is to launch the harness, feed it prompts, and interpret its
output. Tool calls happen inside the harness, over connections the harness owns.

## Why

Three reasons, and they compound rather than being independent arguments.

### 1. Proxying breaks harnesses

MCP clients are configured per-harness, and harnesses differ in which transports and
features they use — stdio, HTTP, SSE, sampling, roots, notifications. A proxy has to
implement all of them correctly for every harness it fronts. A proxy that works for
Claude Code and breaks OpenCode is worse than no proxy, because the failure is silent
and harness-specific: the tool just is not there, and nobody knows why.

### 2. Tool policy belongs at the filesystem, not at MCP

"Deny writes to `.env`" is a filesystem policy. Enforcing it at the MCP layer catches
the harnesses that route file writes through MCP and misses the ones that shell out to
`sed`, or that write through a patch tool, or that use a language server. A policy
enforced on one of three paths is worse than no policy, because it produces
confidence that is not warranted.

The place to enforce a filesystem policy is the filesystem — a container, a sandbox,
or the harness's own permission model. Codex has `--sandbox`, Claude Code has its own
permission model, and OpenBurrow **layers on top of** those rather than replacing
them.

### 3. It would make the daemon a single point of failure for every tool call

Every tool call in every lane would depend on the daemon being up and correct. The
blast radius of a daemon bug becomes "all work stops". And the daemon is the
least-tested component, because it is the hardest to test — it supervises subprocesses
and holds sockets.

Adding a hard dependency on it in the hot path of every tool call is a trade that buys
a feature nobody asked for and costs availability.

## What OpenBurrow does instead

Governance operates on **delegation and action scope**, not on tool invocations.

| Layer | Mechanism |
|---|---|
| What a lane is allowed to do | `AuthorityScope` on the delegation, published in the Agent Card |
| Whether a specific action is permitted | `DelegationLedger.check_action()`, called immediately before execution |
| Whether a command is allowed to run at all | `policy.yaml` rules applied by the **adapter** to the spawn spec, before the process starts |
| Whether a harness is sandboxed | The harness's own configuration. `burrow doctor` reports what it can detect. |

The spawn-spec check is the strongest thing available, and it works because
`build_spawn_spec()` is pure: the policy gate can inspect the exact command, arguments,
environment, and working directory **before** anything executes. A builder with side
effects could not be inspected, which would make the gate advisory.

`burrow policy test --lane alice --command "rm -rf /"` dry-runs the gate, so the
answer to "would this be blocked" is available without trying it.

## What this does not cover

Stated plainly. A governance layer that overstates its reach is worse than one with a
documented boundary.

- **A running lane's tool calls are its own business.** Once a lane is running, the
  ledger does not see its tool calls. A lane that shells out to edit a denied file is
  not stopped by OpenBurrow.
- **Capability mismatch detection relies on declarations.** `detect_capability_mismatch`
  can catch a lane *using* an undeclared capability only when the harness reports it
  in output. A lane that silently uses an undeclared capability will not be caught.
- **A lane that declares no skills is reported as unverified, not as clean.** When
  `declared_skills` is empty but the lane has observed behaviour, the detector raises
  `capability_undeclared` at NOTICE severity rather than passing silently. It is not
  blocking, because an adapter with no skill introspection leaves the field at its
  default and a blocking flag would stall every lane on that harness. So the signal
  means "the card could not be checked", which is weaker than "the card was wrong".
- **Sandboxing is the operator's job.** A lane that runs unsandboxed because its
  harness defaults to that is a configuration problem. `burrow doctor` reports which
  harnesses it found and whether each has a sandbox mode available, but it cannot
  force one on.

## If you need tool-level control

The honest options, in order of strength:

1. **Run lanes in containers.** `docker/` provides images. A container is a real
   boundary and does not depend on any protocol participating in its own enforcement.
2. **Use the harness's sandbox.** `codex --sandbox`, Claude Code's permission mode,
   and equivalents. Configure them in the lane's `openburrow.yaml` entry.
3. **Use `policy.yaml` to constrain spawn specs.** This bounds what a lane can be
   started as. It does not constrain what a running lane does.
4. **Write a custom adapter** that wraps the harness in your own policy layer, if you
   need enforcement between the harness and its tools. `GenericCliAdapter` gives you
   the scaffolding, and the custom-adapter trust gate means this requires explicit
   opt-in.

Option 1 is the one that actually works, and the documentation says so rather than
implying that the governance layer covers it.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `OPENBURROW_MCP_PASSTHROUGH` | `true` | Present so the decision is visible in configuration. Setting it to `false` is not supported and logs a warning. |
| `OPENBURROW_MCP_CONFIG_PATHS` | `[]` | Paths to MCP config files, recorded in the audit for reproducibility. Not read by us — listed so an audit can see what a lane *could* reach. |
| `OPENBURROW_MCP_INVENTORY` | `warn` | `off` / `warn` / `record`. `record` writes an inventory of configured servers to the bus log at session start, which is useful for audits. |

## Reading the code

| Concern | Module |
|---|---|
| Spawn spec construction (pure, inspectable) | `packages/openburrow-adapters/src/openburrow/adapters/base.py` |
| Capability declarations | `packages/openburrow-a2a/src/openburrow/a2a/card/builder.py` |
| Capability mismatch detection | `packages/openburrow-governance/src/openburrow/governance/detectors.py` |
| The policy gate | `packages/openburrow-cli/src/openburrow/cli/commands/governance.py` |
