# openburrow-a2a

The A2A binding. Agent Cards, the task lifecycle, JSON-RPC/SSE transport, the
per-lane server, and the peer client.

## Why this package exists

OpenBurrow's bus speaks [A2A](https://a2aproject.github.io/A2A/) rather than a
bespoke envelope. A custom message format would take a week to build and would be
exactly as good as we made it — and it would make "interoperable" a claim about our
own code. See [ADR 0001](../../../docs/architecture/adr/0001-protocol-grounding.md).

## What's in it

| Module | Responsibility |
|---|---|
| `card/builder.py` | Agent Card construction and read-back helpers |
| `lifecycle/manager.py` | The eight-state task lifecycle, timeouts, retries |
| `transport/jsonrpc.py` | JSON-RPC 2.0 envelopes, error codes, SSE framing |
| `server/lane_server.py` | The per-lane Starlette app and port assignment |
| `client/peer.py` | Peer client, card cache, and the client pool |

## A lane is a real A2A server

Not a client of a central one. Each lane serves:

| Path | Method |
|---|---|
| `/.well-known/agent-card.json` | GET |
| `/` | POST (JSON-RPC) |
| `/stream` | GET (SSE) |
| `/health` | GET |

Lanes talk to each other **directly**, peer to peer. The bus in OpenBurrow is a log,
not a router — a central broker would be a single point of failure and a throughput
ceiling, and A2A is explicitly peer-to-peer.

Ports are assigned deterministically by lane index from `OPENBURROW_A2A_PORT_BASE`, so
a restarted daemon reclaims the same ports and a peer's cached card does not point at a
dead endpoint.

## The task lifecycle

All eight A2A states, with an explicit transition graph. `transition()` validates and
raises `IllegalTaskTransitionError` on an illegal hop — it does not coerce.

```
submitted → working → input_required | auth_required
          → completed | failed | canceled | rejected
```

Three decisions worth knowing:

**`rejected` is not `canceled`.** One means the assignee refused the work; the other
means somebody withdrew it. Conflating them loses the only signal that a lane is being
asked to do something it considers out of scope.

**Terminal states are absorbing.** `retry()` creates a *new* task with a
`parent_task_id`. A system where `completed` can become `working` again has no reliable
way to answer "is this task done".

**Every transition does two things atomically** — mutate the model and append to the
bus log. Doing them separately allows a state change the log does not contain, which
breaks the system's first invariant.

## Authority lives in the Agent Card

`openburrow:authorityScope` is a card metadata extension. This turned out to be the
most valuable consequence of adopting A2A: because every lane already publishes a card,
**"what is this lane allowed to do" is discoverable by any peer before it delegates**.
A governance layer would otherwise have needed a discovery mechanism of its own.

A `delegate_task` that omits the scope means **no authority**, not unlimited authority.
Failing closed on a missing field is the difference between a governance layer and a
suggestion.

## The retry policy is narrow on purpose

`PeerClient.call()` retries transport failures and 5xx with exponential backoff, and
**never 4xx**. A rejected delegation must not be retried into a loop — a governance
refusal that gets retried until it succeeds is not a refusal.

## SSE framing is explicit

`sse_frame()` builds frames as a function rather than an inline template, so the exact
wire bytes are testable. `iterate_sse()` buffers on `\n\n` rather than on chunk
boundaries, because a chunk boundary can split a frame and a naive parser drops events
only under load — which is precisely when you cannot afford it.

## A dependency note

`uvicorn[standard]` is a hard requirement, and the `[standard]` extra is load-bearing.
Without it uvicorn falls back to the pure-Python `h11` parser, which on Windows
corrupts every request after the first one on a keep-alive connection — the path is
mangled into something matching no route, so the server answers 404 to routes that
exist. It presents exactly as a routing bug in our own code.

`scripts/probe_lane_server.py` exists to make that diagnosable in under a minute. The
full story is in the comment at the dependency site.

## Documentation

- [docs/protocols/a2a.md](../../../docs/protocols/a2a.md) — the full binding
- [ADR 0001](../../../docs/architecture/adr/0001-protocol-grounding.md) — why A2A
