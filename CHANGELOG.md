# Changelog

All notable changes are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because the project is pre-1.0, minor versions may contain breaking changes. Those are
called out explicitly under **Changed** rather than buried.

---

## [Unreleased]

### Added

**Protocols**

- A2A lane servers: Agent Cards at `/.well-known/agent-card.json`, JSON-RPC 2.0 at `/`,
  SSE at `/stream`, health at `/health`. Deterministic port assignment by lane index so
  a restart reclaims the same ports.
- The full eight-state A2A task lifecycle with an explicit transition graph.
  `IllegalTaskTransitionError` raises rather than coercing. Terminal states are
  absorbing; `retry()` creates a new task with a `parent_task_id`.
- `openburrow:*` Agent Card metadata extensions, including `openburrow:authorityScope`
  — which makes a lane's permitted actions discoverable by any peer before it delegates.
- ACP performatives (`propose`, `counter`, `accept`, `reject`, `inform`, `withdraw`)
  with a legality graph, and a negotiation driver that escalates on an illegal move
  rather than repairing it.
- `PeerClient` with a narrow retry policy: transport failures and 5xx only. A 4xx is
  never retried, so a governance refusal cannot be retried into a loop.

**Governance**

- `DelegationLedger` enforcing four invariants: a human anchor is required, chain depth
  is bounded, re-delegation needs consent, and scope can only narrow.
- `AuthorityScope` where denial beats grant, `narrow()` is the only sanctioned handoff,
  and an empty scope fails closed.
- `detectors.py` with injection, capability-mismatch, impersonation, poisoned-delegation,
  poisoned-lesson, adversarial-intent, authority-creep, cross-boundary, and secret
  detection.
- `detect_poisoned_lesson` running independently of the Brain's classifier — defense in
  depth on the prompt-injection path.
- An append-only audit log with chain reconstruction and JSONL/CSV/HTML export.
- Approval requests with edit-before-approve.

**Coordination**

- Claims in four kinds (file, directory, step, resource), advisory rather than locking,
  with directory-versus-file overlap detection.
- The plan step graph with cycle detection, snapshots, rollback, and diffing.
  `original_owner_lane` survives handoffs.
- Merge Radar: intent extraction from claims and plan steps, an optional LLM judge with
  clamped confidence, and conflict prediction that keeps `certain` separate from
  `predicted`.

**Memory**

- The Brain: git-anchored entries with `fresh`/`stale`/`unknown` staleness, and
  corroboration-based promotion with the session-versus-repository asymmetry.
- Lessons with TTL expiry, hit-rate tracking, and automatic eviction of noise.
- AGENTS.md ingestion and export through a fenced block that never clobbers
  hand-written content.
- A Yjs-compatible CRDT projection via `pycrdt`, rebuildable from the log.

**Harnesses**

- Ten adapters behind a seven-operation protocol: Claude Code, Codex, OpenCode, Crush,
  Gemini, Aider, Goose, a generic CLI base, a scriptable mock, and a bring-your-own
  custom adapter behind a trust gate.
- Credential isolation: each lane gets its own environment slice.
- A pure `build_spawn_spec` so the policy gate can inspect a command before it runs.

**Observability**

- An append-only bus log with persist-then-publish ordering, monotonic `seq`, and
  content-hash deduplication.
- Debounced worktree file watching that emits a summary rather than a filesystem
  journal.
- Session reels: asciinema v2 casts, a causal timeline with `caused_by`, a static HTML
  transcript scrubber, and redact-then-sign share links.
- `burrow watch` in plain ANSI, plus an optional Textual dashboard.

**CLI and daemon**

- `burrow` with roughly 60 commands across 8 verb families, semantic exit codes, and a
  single output funnel so `--json` cannot drift from human output.
- An asyncio daemon with newline-delimited JSON-RPC over a unix socket or Windows named
  pipe, `0600` socket permissions, crash supervision with backoff, and a documented
  shutdown ordering.
- `scripts/smoke_test.py` — two live A2A servers, real HTTP, real negotiation, and
  governance assertions. Exits non-zero on failure.
- `scripts/probe_lane_server.py` — prints the route table, every endpoint's status and
  body, and the ASGI scope uvicorn actually delivered.

### Fixed

- **`uvicorn[standard]` is now a hard requirement.** Without the `[standard]` extra,
  uvicorn falls back to the pure-Python `h11` parser, which on Windows corrupts every
  request after the first one on a keep-alive connection — the path is mangled into
  something matching no route, so the server answers 404 to routes that exist. It
  presents exactly as a routing bug in our own code. Diagnosed with
  `scripts/probe_lane_server.py` after an earlier, wrong diagnosis; see the comment in
  `packages/openburrow-a2a/pyproject.toml`.
- **`burrow --version` failed with "Missing command".** The root group needed
  `invoke_without_command=True`; without it Click rejects a group invocation that has
  flags but no subcommand, which makes every callback flag unusable.
- **Infinite recursion in reel redaction.** `_sample` re-ran `redact` over its own
  output. Rewritten to sample from the already-redacted text, which is also strictly
  safer — no code path now reaches a secret from the report.
- **`Authorization: Bearer <token>` redaction left the token behind.** The pattern
  matched the scheme but not the credential, producing a redaction that looked like it
  worked.

### Security

- Reel bundles are redacted in full before anything is written or signed. Signing
  unredacted bytes is not expressible: `sign()` takes no content argument.
- Share tokens use HMAC-SHA256 with constant-time comparison and enforced expiry.
- The relay refuses to start in production without a JWT secret, with
  `allow_insecure_http`, with an unrestricted sandbox network, or with chaos enabled.

---

## [0.1.0] — unreleased

Initial development. Not yet released; the entry above describes the state of `main`.

See [docs/roadmap/README.md](docs/roadmap/README.md) for what is built and what is next.
