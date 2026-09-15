# The A2A binding

OpenBurrow speaks [A2A](https://a2aproject.github.io/A2A/) v1.0 (Linux Foundation)
as its bus. This document describes the binding: what each lane exposes, how the
lifecycle behaves, and what we add on top.

Read [ADR 0001](../architecture/adr/0001-protocol-grounding.md) for why A2A rather
than a bespoke envelope. The short version: a bespoke format makes "interoperable" a
claim about our own code, and the cost is every integration that can never happen.

## Endpoints

Every lane runs a `LaneA2AServer` — a Starlette app served by uvicorn on its own
port.

| Path | Method | Purpose |
|---|---|---|
| `/.well-known/agent-card.json` | GET | The Agent Card. `Cache-Control: no-store`. |
| `/` | POST | JSON-RPC 2.0. `Content-Type: application/json`. |
| `/stream` | GET | Server-Sent Events. Task updates and bus activity. |
| `/health` | GET | Liveness. Returns lane id, harness, status, subscriber count. |

Ports are assigned deterministically by lane index from `OPENBURROW_A2A_PORT_BASE`,
so a restarted daemon reclaims the same ports and a peer's cached card does not
point at a dead endpoint. `port=0` is supported for tests and requests an ephemeral
port.

`/health` is a real health check, not a static 200 — it reports the lane's current
status and how many SSE subscribers are attached, which is what you actually want to
know when a lane looks stuck.

## Agent Cards

A card is spec-shaped, with OpenBurrow's additions in `metadata` under an
`openburrow:` prefix so a conformant consumer that knows nothing about us can read
the card and ignore the extensions.

```json
{
  "name": "alice",
  "description": "claude-code implementer lane in session auth-refactor",
  "url": "http://127.0.0.1:50914",
  "version": "0.1.0",
  "capabilities": { "streaming": true, "pushNotifications": false },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [ ... ],
  "metadata": {
    "openburrow:laneId": "lane_01M2J8VPB5WKGM10QRRSVPZK1Q",
    "openburrow:sessionId": "sess_01M2J8VPB5WKGM10QRRSVPZK1Q",
    "openburrow:harness": "claude-code",
    "openburrow:role": "implementer",
    "openburrow:capabilityFlags": { "structuredOutput": true, "streaming": true, ... },
    "openburrow:authorityScope": { "granted": [...], "denied": [...] },
    "openburrow:delegationId": "del_01M2J8..."
  }
}
```

### Why authority lives in the card

This turned out to be the most valuable consequence of adopting A2A. Because every
lane already publishes a card, **"what is this lane allowed to do" is discoverable
by any peer before it delegates**. A governance layer would otherwise have had to
add a discovery mechanism of its own; here it is the protocol's existing mechanism
carrying one more field.

Read-back helpers: `capability_flags_from_card()`, `authority_scope_from_card()`,
`declared_skills()`.

### Skills

`BASE_SKILLS` is `implement`, `review`, `explain`, `plan`. `ROLE_SKILLS` adds
role-derived skills — a `reviewer` lane also declares `approve`, a `coordinator`
declares `delegate` and `claim`.

Role skills are derived rather than declared because a reviewer that cannot approve
is a role definition that does not mean anything, and a harness author forgetting to
list `approve` would produce a lane that silently cannot do its job.

### Capability declarations

`HarnessCapabilities` declares `structured_output`, `streaming`, `resumable`,
`mcp_tools`, `supports_interrupt`, `supports_file_context`, and `native_a2a`.

**These report the truth including the degraded case.** The OpenCode adapter has a
structured server mode and a PTY fallback. Its `effective_capabilities()` reports the
capabilities of whichever mode is actually in use, not the union of what the harness
can do in principle. Reporting the optimistic set would make the capability-mismatch
detector fire on a lane that never claimed the capability — noise that trains people
to ignore the detector.

## Task lifecycle

The eight standard A2A states:

```
                    ┌──────────────┐
                    │  submitted   │
                    └──────┬───────┘
                           │ start
                    ┌──────▼───────┐
        ┌───────────│   working    │───────────┐
        │           └──────┬───────┘           │
        │                  │                   │
  ┌─────▼──────┐    ┌──────▼───────┐    ┌──────▼──────┐
  │input_required│  │auth_required │    │  completed  │  ← terminal
  └─────┬──────┘    └──────┬───────┘    └─────────────┘
        │ resume           │ resume     ┌─────────────┐
        └──────────────────┴───────────►│   failed    │  ← terminal
                                        └─────────────┘
                                        ┌─────────────┐
                                        │  canceled   │  ← terminal
                                        └─────────────┘
                                        ┌─────────────┐
                                        │  rejected   │  ← terminal
                                        └─────────────┘
```

`TASK_TRANSITIONS` is an explicit graph. `A2ATask.transition()` validates against it
and raises `IllegalTaskTransitionError` on an illegal hop. It does **not** coerce.

### `rejected` versus `canceled`

Worth stating explicitly because it is easy to collapse them:

- **`rejected`** — the assignee refused the work. It considers this out of scope, or
  against its instructions, or not its job.
- **`canceled`** — somebody with authority withdrew it.

Conflating them loses the only signal that a lane is being asked to do something it
considers out of scope, which is a governance-relevant fact and exactly the kind of
thing a coordinator should notice.

### Terminal states are absorbing

`retry()` creates a **new** task with a `parent_task_id`. It never resurrects the
terminal one. A system where `completed` can become `working` again has no reliable
way to answer "is this task done", and every consumer of that question — the plan,
the metrics, the audit — depends on the answer being stable.

The task chain is queryable via `Repository.task_chain()`, so a retry's history is
preserved without mutating history.

### Two things happen atomically on every transition

```python
async def _transition(self, task, to_state, ...):
    task.transition(to_state)                      # validate + record history
    await self.bus.append_model(task, ...)         # append to the log
```

Mutating without logging would produce a state change the log does not contain,
which breaks the first invariant of the whole system
([ADR 0004](../architecture/adr/0004-append-only-bus-log.md)). The two are one
operation or they are a bug.

### Timeouts are enforced, not assumed

`enforce_timeouts()` applies per-state deadlines from configuration. `working` has a
long deadline, `input_required` a short one. A task that has been waiting for input
for an hour is not being worked on, and leaving it in `input_required` forever means
`burrow session show` reports a session as active when it is stuck.

### Waiting is polling, and that is correct

`wait_for_terminal()` polls rather than awaiting an event. The lane that completes a
task is frequently a **different process** from the one waiting on it — it is the
lane's own A2A server, or a harness subprocess. An in-process event would never fire.
Polling the log is the only mechanism that works across that boundary.

## JSON-RPC transport

Standard 2.0 envelopes. `A2A_METHODS` is the supported method set:
`message/send`, `message/stream`, `tasks/get`, `tasks/cancel`, `tasks/pushNotificationConfig/*`.

Error codes include the standard range plus A2A-specific values:

| Code | Meaning |
|---|---|
| `-32700` | Parse error |
| `-32600` | Invalid request |
| `-32601` | Method not found |
| `-32602` | Invalid params |
| `-32603` | Internal error |
| `-32001` | `TASK_NOT_FOUND` |
| `-32002` | `TASK_NOT_CANCELABLE` |
| `-32003` | `PUSH_NOT_SUPPORTED` |
| `-32004` | `UNSUPPORTED_OPERATION` |
| `-32005` | `CONTENT_TYPE_NOT_SUPPORTED` |

`JsonRpcError.from_exception()` maps `OpenBurrowError` subclasses onto wire codes, so
a governance refusal arrives at the peer with a code it can branch on rather than as
an opaque internal error.

### SSE framing is explicit

`sse_frame()` builds frames as an explicit function rather than a string template
inline, so the exact wire bytes are testable. `iterate_sse()` buffers on `\n\n`
rather than on chunk boundaries, because a chunk boundary can split a frame and a
naive parser drops events under load — a bug that appears only when the network is
busy, which is when you least want it.

## The peer client

`PeerClient` wraps a peer's endpoint with a card cache.

### The retry policy is deliberately narrow

```python
if response.status_code >= 500 and attempt < self.max_retries:
    ...retry
if response.status_code >= 400:
    raise A2AProtocolError(...)     # no retry
```

Transport failures and 5xx are retried with exponential backoff. **4xx is never
retried.** A rejected delegation must not be retried into a loop — a governance
refusal that gets retried until it succeeds is not a refusal, and a peer that
answers 400 to a malformed request will answer 400 every time.

### Authority on delegation fails closed

```python
await client.delegate_task(
    ...,
    authority_scope=scope.model_dump(),  # omit this and the delegation carries NO authority
)
```

Omitting `openburrow:authorityScope` means **no authority**, not unlimited authority.
Failing closed on a missing field is the difference between a governance layer and a
suggestion.

### Card conformance is checked, not assumed

`card_is_conformant(card)` returns `(ok, problems)`. It is used in the smoke test and
available to the daemon. "We emit conformant cards" is a claim that should be
checkable by a peer, not just by us.

## Configuration

From `.env.example`:

| Variable | Default | Notes |
|---|---|---|
| `OPENBURROW_A2A_PORT_BASE` | `51000` | Lanes are assigned ports by index from here. |
| `OPENBURROW_A2A_HOST` | `127.0.0.1` | Loopback by default. Changing this exposes lanes on the network. |
| `OPENBURROW_A2A_CARD_PATH` | `/.well-known/agent-card.json` | Spec default; configurable for testing. |
| `OPENBURROW_A2A_TASK_TIMEOUT_S` | `1800` | Default deadline for `working`. |
| `OPENBURROW_A2A_INPUT_TIMEOUT_S` | `300` | Shorter, because waiting for input is not progress. |
| `OPENBURROW_A2A_MAX_RETRIES` | `2` | Applied to transport failures and 5xx only. |
| `OPENBURROW_A2A_RATE_LIMIT_RPS` | `20` | Per-peer inbound limit. |
| `OPENBURROW_A2A_SSE_QUEUE_SIZE` | `256` | Bounded; slow subscribers drop rather than stall a lane. |

## A note on the HTTP parser

Lanes run under `uvicorn[standard]`, which is not decoration. Without it uvicorn
falls back to the pure-Python `h11` parser, and on Windows that parser corrupts every
request after the first one on a keep-alive connection — the path is mangled into
something that matches no route, so the server answers 404 to routes that
demonstrably exist. It looks exactly like a routing bug in our own code and is not
one.

`scripts/probe_lane_server.py` exists to make that diagnosable: it prints the route
table, the status and body of every endpoint, and the ASGI scope uvicorn actually
delivered. See the comment in `packages/openburrow-a2a/pyproject.toml` for the full
story, including why the version bounds were *not* the answer.

## Reading the code

| Concern | Module |
|---|---|
| Card construction and read-back | `packages/openburrow-a2a/src/openburrow/a2a/card/builder.py` |
| Lifecycle and transitions | `packages/openburrow-a2a/src/openburrow/a2a/lifecycle/manager.py` |
| JSON-RPC envelopes, error codes, SSE | `packages/openburrow-a2a/src/openburrow/a2a/transport/jsonrpc.py` |
| The lane server | `packages/openburrow-a2a/src/openburrow/a2a/server/lane_server.py` |
| Peer client and pool | `packages/openburrow-a2a/src/openburrow/a2a/client/peer.py` |
