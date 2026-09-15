# openburrow-adapters

Ten harness adapters behind one seven-operation protocol.

## Supported harnesses

| Harness | Structured | MCP | Notes |
|---|---|---|---|
| **claude-code** | yes | yes | stream-json mode |
| **codex** | yes | yes | its own `--sandbox` and approval prompts, surfaced as `auth_required` |
| **opencode** | yes | yes | headless server mode, with a PTY fallback |
| **crush** | no | yes | ANSI stripped before parsing; reads `AGENTS.md` natively |
| **gemini** | yes | yes | |
| **aider** | no | no | commit SHA extracted from output |
| **goose** | no | yes | deliberately the awkward case |
| **custom** | no | no | bring your own, behind a trust gate |
| **mock** | yes | no | scriptable; the basis of the chaos tests |
| **generic** | — | — | the base class other adapters extend |

`burrow adapters list` reports which are installed on this machine. It never fails
because a harness is absent — the registry reports availability rather than requiring
it.

## The protocol

Five operations plus two hooks:

```python
build_spawn_spec(lane) -> SpawnSpec     # MUST be pure
start(lane) -> SpawnSpec
send_prompt(lane, text)
read_output() -> AsyncIterator[HarnessOutput]
stop(lane)
status() -> HarnessStatus

translate_output(raw) -> str | None     # hook
inject_message(lane, message) -> bool   # hook
```

### `build_spawn_spec` is pure, and that is the point

It returns the exact command, arguments, environment, and working directory without
executing anything. The policy gate calls it to inspect a command **before** it runs.

An implementation with side effects cannot be inspected safely, which makes the gate
advisory — and a gate that can only advise is not a gate.

### `read_output` is an iterator, not a callback

Pull, not push. The daemon decides when to consume, which is what makes backpressure
expressible. A callback interface would let a fast harness flood a slow consumer with
no way to say "slow down".

### `HarnessOutput.structured` is an honesty flag

Set it `True` only when you actually parsed something with a defined schema. Consumers
use it to distinguish "the harness told us it changed 3 files" from "we guessed from
the diff output". Setting it `True` for a heuristic makes that difference invisible.

### `parse_usage` returns `{}` when it does not know

Never an estimate. An estimated token count makes a cost report look complete while
being wrong, and a number wrong in a plausible direction is worse than a missing one
because nobody goes looking for it.

## Credential isolation

`base_env(lane)` builds each lane's environment slice. A lane sees its own harness's
keys and nothing else. There is no shared "agent token", because a shared credential
makes every audit record ambiguous — you can no longer tell which lane did the thing.

## Capability declarations report the truth

`effective_capabilities()` returns the capabilities of whichever mode is **actually in
use**. The OpenCode adapter has a structured server mode and a PTY fallback; reporting
the union of what the harness *could* do would make the capability-mismatch detector
fire on a lane that never claimed the capability.

That is noise, and noise in a governance detector trains people to ignore the detector
— the worst outcome for a security control.

## The interpretation order is fixed

`GenericCliAdapter._interpret()` tries: JSONL → diff → usage limit → plan-by-regex →
plain text.

Fixed rather than configurable, because a configurable order would mean two
installations interpreting the same harness output differently, which makes a bug
report untriable.

## Custom adapters and the trust gate

Adapters loaded from `OPENBURROW_CUSTOM_ADAPTER_DIR` are gated by
`OPENBURROW_CUSTOM_ADAPTER_TRUST` (`prompt` / `allow` / `deny`).

This exists because **loading arbitrary code from a repository checkout without asking
is how you get a supply-chain incident inside an agent orchestration tool.** A
malicious adapter has the same privileges as the daemon.

## The mock harness

Built for the chaos tests, and it is why the daemon's failure paths are testable at
all. It can be scripted to hang, crash, rate-limit, or emit malformed output, and it
implements the same protocol as a real adapter.

A real harness will not fail when you ask it to. Testing crash-restart handling
therefore requires a harness that will.

## Goose is deliberately ugly

`goose.py` is the least capable adapter in the tree. Goose is a black box with no
structured mode and no reliable prompt protocol, so its adapter reports
`structured=False` throughout, translates conservatively, and declares a limited
capability set.

It is kept as-is rather than special-cased because it is the honest floor of what this
protocol can do. Hiding it would make the protocol look cleaner than it is.

## Documentation

- [docs/adapters/authoring.md](../../../docs/adapters/authoring.md) — writing an adapter
- [ADR 0010](../../../docs/architecture/adr/0010-adapter-protocol.md) — why seven operations
