# openburrow-relay

The only component in OpenBurrow that assumes more than one machine. A FastAPI
service that lets remote teammates join a session, and that is deliberately the
dumbest thing in the codebase.

## The design constraint that shapes it

The bus is local. `bus_events` is a SQLite table with a monotonic integer `seq`,
owned by one daemon, on one machine. That is what makes ordering cheap and
unambiguous, and it is not something you can make distributed without giving up
the property the whole system leans on.

So the relay does not try. It does not own a bus, does not assign sequence
numbers, does not reimplement A2A, and does not run lanes. It **relays**: a
daemon connects outbound, publishes its own bus events, and the relay fans them
out to other participants in the same room.

The single hard rule: **the relay never invents an event.** Every frame it emits
carries the originating daemon's `seq` and repo identity. If a client needs
causal order, it orders by `(repo, seq)` — the relay's own arrival order is
explicitly *not* a clock, and the API says so rather than implying otherwise.

## Tenancy is scoped to the repo, not the user

The obvious multi-tenancy model is "one account, many repos". It was rejected
because it puts the wrong thing in the isolation boundary: two people working in
the same repo are collaborators who must see each other's events, and one person
with two repos is two unrelated rooms.

So a *room* is a repo. Membership is a room membership. A user can be in many
rooms and a room can have many users, and neither fact is a permission to do
anything in the other. This also makes the quota model obvious — see below —
because the unit that consumes resources is the repo, not the person.

## Quotas are enforced where the resource is

Rate limiting is per-room per-connection on the WebSocket, not per-user at the
edge. A user with ten rooms legitimately has ten times the event throughput, and
a global per-user limit would punish exactly the heavy users the product is for.
The HTTP surface and the WebSocket share **one** limiter — `ratelimit.TokenBucket`
— because two limiters with two configs and two failure modes is worse than one,
and because a limiter that only guards HTTP is a limiter you can walk around by
staying on the socket.

There is no password anywhere. Access is an invite token with 256 bits of
entropy, stored as a SHA-256 hash. That is why `passlib` is not a dependency: a
password KDF over a token that cannot be guessed is ceremony, and bcrypt's
72-byte truncation would be a silent bug if a token ever did pass through it.

## What it does not cover

- **It is not a bus.** No ordering guarantees beyond what the originating daemon
  put in the payload. No replay of events it never saw.
- **It does not run or proxy harnesses.** A remote participant talks *about* a
  session; the lane processes stay on the machine that owns the worktree. This
  is a security boundary, not an oversight — see `SECURITY.md`.
- **It does not hold credentials.** No harness tokens, no MCP credentials, no
  provider API keys. A relay that held them would be the most valuable thing to
  compromise in the system, and it gains nothing by holding them.
- **It does not make the brain doc authoritative.** The CRDT sync carried over
  the relay is a *projection*; the bus log on the owning daemon remains the
  record of truth, and `rebuild()` works with the relay switched off entirely.
- **It does not authenticate to the daemon.** The daemon connects out; the relay
  accepts. There is no inbound path from the internet to a control socket, which
  is why the relay can be exposed without exposing `burrow.sock`.

## Endpoints

| Path | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. No auth, no database access. |
| `GET /readyz` | Readiness. Checks Postgres. |
| `GET /metrics` | Prometheus. Room count, connection count, event fan-out. |
| `POST /auth/token` | Exchange a room invite for a JWT. |
| `GET /rooms/{room}/events` | HTTP tail of relayed events, for non-WebSocket clients. |
| `WS /rooms/{room}/stream` | The live fan-out. |
| `WS /rooms/{room}/doc` | Yjs/pycrdt update exchange for the brain doc. |

Two health endpoints rather than one, because "the process is up" and "the
process can do its job" fail differently and a load balancer needs to
distinguish them.

## Configuration

Everything comes from the environment; there is no config file. See
`.env.example` for the `OPENBURROW_RELAY_*` block. The secrets that matter:

- `OPENBURROW_RELAY_JWT_SECRET` — no default. The service refuses to start
  without it rather than generating a random one, because a random secret on
  restart silently invalidates every issued token and looks like a bug.
- `OPENBURROW_RELAY_DB_URL` — Postgres, not SQLite. A relay that cannot be
  restarted without losing rooms is not a relay.

## See also

- [docs/web/README.md](../../docs/web/README.md) — the frontend that consumes this
- [ADR 0006 — SQLite and Postgres, not one of them](../../docs/architecture/adr/0006-sqlite-and-postgres.md)
- [ADR 0007 — CRDT as projection](../../docs/architecture/adr/0007-crdt-as-projection.md)
