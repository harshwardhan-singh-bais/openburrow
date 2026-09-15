# Writing an adapter

OpenBurrow ships ten adapters. When you need an eleventh — a harness we do not
support, an internal tool, a wrapper around something — this is the guide.

Read [`packages/openburrow-adapters/src/openburrow/adapters/base.py`](../../packages/openburrow-adapters/src/openburrow/adapters/base.py)
alongside this. The protocol is seven methods and the docstrings explain the
non-obvious ones.

## The protocol

```python
class HarnessAdapter(ABC):
    # five operations
    def build_spawn_spec(self, lane: Lane) -> SpawnSpec: ...  # MUST be pure
    async def start(self, lane: Lane) -> SpawnSpec: ...
    async def send_prompt(self, lane: Lane, prompt: str) -> None: ...
    def read_output(self) -> AsyncIterator[HarnessOutput]: ...
    async def stop(self, lane: Lane) -> None: ...
    def status(self) -> HarnessStatus: ...

    # two translation hooks
    def translate_output(self, raw: str) -> str | None: ...
    async def inject_message(self, lane: Lane, message: BusMessage) -> bool: ...
```

Most adapters extend `GenericCliAdapter`, which implements all seven in terms of a
handful of class attributes. A harness with a structured mode needs about 150 lines;
a plain CLI harness needs about 40.

## Start here: `GenericCliAdapter`

```python
class MyHarnessAdapter(GenericCliAdapter):
    name = "myharness"
    binary = "myharness"
    supports_structured = True

    structured_args = ("--output-format", "stream-json", "--print")
    prompt_flag = ("--prompt",)
    model_flag = ("--model",)

    def _command(self, lane: Lane) -> list[str]:
        return [self.binary, *self.structured_args, *self.model_flag, lane.model]
```

That is a working adapter for a harness that accepts a prompt on the command line and
emits JSON lines. `GenericCliAdapter` handles the PTY, the environment slice, the
output pump, termination, and the interpretation order.

## The five operations

### `build_spawn_spec` must be pure

```python
def build_spawn_spec(self, lane: Lane) -> SpawnSpec:
    return SpawnSpec(
        command=[self.binary, "--print"],
        cwd=lane.worktree_path,
        env=self.base_env(lane),
        ...
    )
```

**No side effects. No filesystem writes. No subprocess.** This is not a style
preference: the policy gate calls this method to inspect a command *before* it runs.
An implementation that creates files or spawns anything cannot be inspected safely,
which makes the gate advisory — and a gate that can only advise is not a gate.

If you need to prepare something before launch, do it in `start()`, which is allowed
to have effects and is not called by the gate.

`base_env(lane)` returns the lane's credential slice. Use it. A lane that sees another
lane's keys makes every audit record ambiguous, because you can no longer tell which
lane did the thing.

### `read_output` is an async iterator, not a callback

```python
async for output in adapter.read_output():
    ...
```

Pull, not push. The daemon decides when to consume, which is what makes backpressure
expressible. A callback-based interface would let a fast harness flood a slow consumer
with no way to say "slow down".

Yield `HarnessOutput`:

```python
HarnessOutput(
    text="...",
    structured=True,  # did you parse this, or interpret it?
    kind="diff",  # "text" | "diff" | "plan" | "result" | "error"
    usage={"input_tokens": 1200, "output_tokens": 340},
    data={...},  # parsed payload when structured
)
```

**`structured` is an honesty flag, not a boast.** Set it `True` only when you actually
parsed something with a defined schema. A consumer uses it to distinguish "the harness
told us it changed 3 files" from "we guessed from the diff output", and setting it
`True` for a heuristic makes the difference invisible.

**`parse_usage` returns `{}` when you do not know.** Never estimate. An estimated
token count makes a cost report look complete while being wrong, and a number that is
wrong in a plausible direction is worse than a missing number because nobody goes
looking for it.

### `stop` is where the hard cases live

```python
async def stop(self, lane: Lane) -> None:
    # SIGTERM, wait, SIGKILL
```

The base class implements the sequence. Override only if your harness needs something
else — some need a specific signal, some need a control message on stdin, some fork
children that outlive the parent and have to be reaped.

Leaving orphans is the most common adapter bug. If your harness spawns a language
server or a daemon, `stop` must deal with it, or the next `burrow doctor` reports a
stale process and nobody knows where it came from.

### `status` should not lie

`status()` is called by the supervisor to decide whether a lane is healthy. Reporting
`RUNNING` for a process that has exited means the supervisor never restarts it and the
lane silently stops producing output — the failure mode where `burrow watch` shows a
lane that looks fine and is doing nothing.

## The two hooks

### `translate_output`

Converts raw harness output into a form the plan extractor and the bus can use.
Return `None` for anything that is merely chatter.

```python
def translate_output(self, raw: str) -> str | None:
    if raw.startswith("Step ") and ":" in raw:
        return raw
    return None  # not a plan step; leave it as raw text
```

**Be conservative.** A hook that guesses at structure produces confidently wrong
parsed output, and wrong-but-structured is worse than right-but-unstructured because
the consumer trusts it. Returning `None` more often than you think you should is the
correct instinct.

### `inject_message`

Delivers a bus message into a running harness. The base class implements a documented
three-tier strategy:

1. A native input channel, if the harness has one (stdin, a control socket).
2. A control file the harness watches, if configured.
3. Terminal injection via the PTY, as a last resort.

Terminal injection works but is fragile — the harness may be mid-output, or in a
modal state. `render_injection()` wraps the message with **explicit attribution**:

```
[openburrow from lane_bob · claim] src/api/routes.py is claimed by bob
```

The attribution is not cosmetic. It is what makes the poisoned-message test
meaningful: a lane that receives an unattributed instruction cannot tell whether it
came from a teammate or from a file it just read, and neither can the auditor.

## Capability declarations

```python
def effective_capabilities(self) -> HarnessCapabilities:
    return HarnessCapabilities(
        structured_output=self._server_mode_active(),
        streaming=True,
        resumable=False,
        mcp_tools=True,
        supports_interrupt=True,
        supports_file_context=True,
        native_a2a=False,
    )
```

**Report the truth, including the degraded case.** If your adapter has a rich mode and
a fallback, this must return the capabilities of whichever is *currently in use*, not
the union of what the harness could do in principle.

Reporting the optimistic set makes the capability-mismatch detector fire on a lane that
never claimed the capability. That is noise, and noise in a governance detector trains
people to ignore the detector — which is the worst outcome for a security control.

## Registering

Built-in adapters are added to `_BUILTIN_MODULES` in `registry.py`. Loading is lazy,
so a missing optional dependency disables one adapter rather than breaking the
registry — if your harness needs a package, make it optional and let the adapter
report unavailable.

Aliases go in `_ALIASES` (`claude` → `claude-code`), so users can type the short name.

## Custom adapters and the trust gate

Adapters loaded from `OPENBURROW_CUSTOM_ADAPTER_DIR` are gated:

| `OPENBURROW_CUSTOM_ADAPTER_TRUST` | Behaviour |
|---|---|
| `prompt` (default) | Ask before loading each adapter. |
| `allow` | Load without asking. |
| `deny` | Never load. |

This gate exists because **loading arbitrary code from a repository checkout without
asking is how you get a supply-chain incident inside an agent orchestration tool.** A
malicious adapter has the same privileges as the daemon: it can read the database,
emit bus events, and spawn processes.

If you are distributing an adapter, document that installing it is a trust decision.
Do not tell people to set `allow` globally.

## Testing your adapter

```python
# scripts/smoke_test.py exercises whatever is installed
uv run python scripts/smoke_test.py

# per-harness health
burrow adapters health myharness
```

Test these specifically, because they are where adapters break:

- **Stop leaves no orphans.** Run a lane, stop it, check the process list.
- **Malformed output does not crash the pump.** Feed it garbage; the adapter should
  yield it as unstructured text.
- **`parse_usage` returns `{}` for an unrecognised format**, not zeros and not an
  estimate.
- **`build_spawn_spec` has no side effects.** Call it twice and assert nothing changed
  on disk.
- **A rate-limit message is detected.** `detect_usage_limit` should catch it, or the
  supervisor will restart a lane into a wall.

The `mock` adapter implements the same protocol and can be scripted to hang, crash,
rate-limit, or emit malformed output. Use it to test the daemon's failure handling
without needing your harness to fail on demand.

## Worked example: the awkward case

`goose.py` is deliberately the ugliest adapter in the tree. Goose is a black box with
no structured mode and no reliable prompt protocol, so its adapter cannot parse much
and says so — `structured=False` throughout, conservative `translate_output`, and a
capability set that reflects what it actually provides.

It is kept as-is rather than special-cased because it is the honest floor of what this
protocol can do. An adapter for a harness with no structured output *should* be thin
and limited. Hiding that would make the protocol look cleaner than it is, and the next
person writing an adapter for a similarly opaque harness would wonder what they were
doing wrong.
