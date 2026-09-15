# 0010 — A seven-operation adapter protocol

**Status:** Accepted

## Context

OpenBurrow wraps ten harnesses: Claude Code, Codex, OpenCode, Crush, Gemini, Aider,
Goose, a generic CLI adapter, a scriptable mock, and a bring-your-own custom
adapter. They differ in how they are launched, how output is produced, whether they
have a structured mode, how they surface approvals, and how they report token
usage.

The question is how much surface an adapter has to implement.

## Decision

Five operations plus two translation hooks:

```
build_spawn_spec(lane) -> SpawnSpec     # pure; no side effects
start(lane) -> SpawnSpec
send_prompt(lane, text)
read_output() -> AsyncIterator[HarnessOutput]
stop(lane)
status() -> HarnessStatus

translate_output(raw) -> str | None     # hook
inject_message(lane, message) -> bool   # hook
```

`build_spawn_spec` is **pure** — it returns the exact command, arguments,
environment, and working directory without executing anything, so the policy gate
can inspect a command *before* it runs.

## Rejected

**A minimal two-method interface (`start` and `send`).** Rejected because `stop`
is where the hard cases live. Terminating a harness cleanly — SIGTERM, wait, then
SIGKILL — is genuinely different per harness, and a protocol without `stop` pushes
that into each caller.

**An event-driven interface with a callback per output type.** Rejected because it
inverts control in a way that makes the daemon's output pump much harder to reason
about. An async iterator keeps the pull model: the daemon asks for output, which
means backpressure is expressible.

**Letting adapters write to the bus directly.** Rejected. An adapter that can emit
bus events is an adapter that can forge them, and the bus log's value depends on
every event having come through the daemon. Adapters return `HarnessOutput` and the
daemon decides what becomes an event.

**Requiring structured output.** Rejected because most harnesses do not have it.
`HarnessOutput.structured` is an explicit boolean saying whether the payload was
parsed or interpreted, so a consumer can tell the difference between "the harness
told us it changed 3 files" and "we guessed from the diff output".

**A plugin registry with dynamic discovery from `PATH`.** Rejected for security:
loading arbitrary code from a repository checkout without asking is how you get a
supply-chain incident inside an agent orchestration tool. Custom adapters load from
an explicit directory (`OPENBURROW_CUSTOM_ADAPTER_DIR`) behind a
`prompt`/`allow`/`deny` trust gate.

## Consequences

**Accepted costs:**

- **Harnesses with unusual models fit awkwardly.** Goose is the documented example:
  it is a black box with no structured mode and no reliable prompt protocol, so its
  adapter is the honest floor of what this protocol can do. The adapter is
  deliberately kept as the awkward case rather than special-cased away, because
  hiding it would make the protocol look cleaner than it is.
- **Capability declaration is on the honour system.** `effective_capabilities()`
  reports what a harness claims, and the mismatch detector can only catch
  undeclared-*used* capabilities when the harness mentions them in output. A lane
  that silently uses an undeclared capability will not be caught.
- **`translate_output` returns `None` conservatively.** A hook that guesses at
  structure produces confidently wrong parsed output, so it returns `None` for
  anything that is merely chatter. This means more raw text reaching the plan
  extractor than an aggressive parser would produce.
- **Seven operations is more than a minimal interface.** Each adapter is 200–400
  lines rather than 50. The `GenericCliAdapter` base absorbs most of it, which is
  why the per-harness adapters are thin.

**Benefits:**

- The policy gate can inspect a command before execution, because `build_spawn_spec`
  is pure. This is the single most important property of the protocol.
- The mock adapter implements the same protocol and is scriptable, so chaos tests
  (hang, crash, rate-limit, malformed output) run without any real harness
  installed. That is what makes the failure paths testable at all.
- `parse_usage()` returns `{}` rather than estimating, so a cost report never
  contains a fabricated number.
