# Roadmap

**22 stages, 258 items, in build order.**

The roadmap is a *sequence*, not a backlog. Items are ordered so that each stage
depends only on stages before it, which means the project is always in a state where
everything built so far works. That property is worth more than any individual item,
because it is what makes the build verifiable at every point rather than only at the
end.

> **A note on this document.** The stage structure below is the working breakdown:
> stage names, the theme of each, and what it delivers. The item-level detail within
> each stage is maintained as the build progresses, because an item that is
> specified before its stage is reached tends to be specified against a wrong
> assumption about the stage before it. If your canonical 258-item list differs,
> this document should be reconciled to it — the sequence is the part that matters
> and the part that has been verified by building it.

## How to read a stage

Each stage has four properties:

**Depends on** — the stages that must be complete first. Nothing here depends on a
later stage, by construction.

**Delivers** — what exists at the end of the stage that did not exist before.

**Verified by** — how you check it works. Every stage has something runnable.
Stages without a verification method are how a roadmap becomes a wish list.

**Design note** — the decision in this stage that was not obvious, and what it cost.
These are the parts worth reading if you are trying to understand the codebase rather
than the plan.

---

## Part I — Foundations (stages 1–4)

### Stage 1 — Workspace and configuration

**Delivers:** the `uv` workspace with its package boundaries, the layered
configuration model, and the path resolution that everything else depends on.

**Verified by:** `burrow doctor` reports the environment; `burrow config show` renders
the merged configuration with its sources.

**Design note:** configuration has three layers — `openburrow.yaml` (committed,
team), environment and `.env` (machine, secret), and defaults — with a deliberate
asymmetry: **environment can make governance stricter but never weaker.** A machine
cannot silently disable a control the team agreed on. A developer who wants to work
around a governance rule has to change the committed configuration and get it
reviewed, rather than setting a variable.

The cost: a legitimate local override sometimes cannot be expressed, and the error
message has to explain why rather than just refusing.

### Stage 2 — Domain model

**Delivers:** every model — sessions, lanes, tasks, messages, claims, plans, brain
entries, lessons, delegations, approvals, metrics — with validation, lifecycle
methods, and graph queries.

**Verified by:** the model layer has the densest unit tests in the project. Illegal
transitions raise, graph cycles are detected, `content_hash` is stable across
processes.

**Design note:** `BurrowModel.content_hash` hashes the *semantic* content of a model,
excluding ids and timestamps. It is what gives the bus log free idempotency: replaying
a log that was already applied produces identical hashes and the duplicate append is
rejected. Without it, crash recovery would double-apply everything and would be a
corruption bug rather than a feature.

### Stage 3 — Persistence

**Delivers:** SQLModel tables, the async engine with its PRAGMAs, the repository
layer, and the append-only bus log.

**Verified by:** the log rejects duplicate content hashes; `tail()` and `replay()`
agree; `prune()` leaves audit rows alone.

**Design note:** `seq` is an integer primary key, not a ULID. ULIDs sort at
millisecond granularity, which is not fine-grained enough for a bus that can emit
several events in the same millisecond — the order would be decided by the random
component. Event *identity* is a ULID; event *order* is `seq`.

### Stage 4 — Adapter protocol and the mock harness

**Delivers:** the seven-operation adapter protocol, the registry, and the scriptable
mock harness.

**Verified by:** the mock harness runs the full lifecycle with no real harness
installed, and can be scripted to hang, crash, rate-limit, or emit malformed output.

**Design note:** the mock harness is built in stage 4, not in the test stage. Every
failure path in the daemon — crash restart, rate limit backoff, malformed output
handling — is only testable against a harness that fails on demand, and a real
harness will not fail when you ask it to. Building the mock early made those paths
testable from the moment they existed.

---

## Part II — Protocols (stages 5–9)

### Stage 5 — A2A transport

**Delivers:** JSON-RPC 2.0 envelopes, the error code mapping, SSE framing, and the
frame parser.

**Verified by:** SSE frames survive chunk boundaries; `OpenBurrowError` subclasses map
onto wire codes.

**Design note:** `sse_frame()` is an explicit function rather than an inline string
template, so the exact wire bytes are testable. `iterate_sse()` buffers on `\n\n`
rather than on chunk boundaries, because a chunk boundary can split a frame and a
naive parser drops events only under load — which is precisely when you cannot afford
it.

### Stage 6 — Agent Cards

**Delivers:** card construction with `openburrow:*` metadata extensions, the skill
sets, capability declarations, and the read-back helpers.

**Verified by:** `card_is_conformant()` passes; a reviewer lane declares `approve`; a
PTY-fallback lane reports its degraded capabilities.

**Design note:** capability declarations report the truth **including the degraded
case**. OpenCode has a structured server mode and a PTY fallback, and
`effective_capabilities()` reports whichever is in use rather than the union of what
the harness could do. Reporting the optimistic set would make the capability-mismatch
detector fire on a lane that never claimed the capability — noise that trains people
to ignore the detector.

### Stage 7 — Task lifecycle

**Delivers:** the eight-state graph, the transition manager, timeout enforcement, and
the retry-with-parent mechanism.

**Verified by:** illegal transitions raise; terminal states are absorbing; a retry
creates a new task with a `parent_task_id`; `wait_for_terminal` works cross-process.

**Design note:** every transition does two things atomically — mutate the model and
append to the bus log. Doing them separately would allow a state change the log does
not contain, which breaks the system's first invariant. The two are one operation or
they are a bug.

### Stage 8 — ACP performatives

**Delivers:** the performative set, the legality graph, message construction, and the
negotiation driver.

**Verified by:** the smoke test runs a full negotiation to agreement; an illegal
performative escalates rather than being coerced.

**Design note:** an illegal performative sequence **escalates instead of being
repaired.** Coercing it would make the driver more robust and would hide a real bug —
almost always in a harness adapter's translation layer. A negotiation that "resolved"
without either side agreeing is worse than one that visibly failed.

### Stage 9 — Lane servers and the peer client

**Delivers:** the per-lane Starlette app, deterministic port assignment, the peer
client with its card cache, and the client pool.

**Verified by:** two live servers exchange a real JSON-RPC message over HTTP; a
rejected delegation is not retried.

**Design note:** the retry policy retries transport failures and 5xx, and **never
4xx.** A rejected delegation must not be retried into a loop — a governance refusal
that gets retried until it succeeds is not a refusal.

Also in this stage: the `uvicorn[standard]` requirement, discovered the hard way. See
[protocols/a2a.md](../protocols/a2a.md#a-note-on-the-http-parser).

---

## Part III — Harnesses (stages 10–13)

### Stage 10 — The generic CLI adapter

**Delivers:** `GenericCliAdapter`, the base that absorbs the shared 80%: JSONL
detection, diff detection, usage-limit detection, plan extraction, plain text
fallback.

**Verified by:** each interpretation path is exercised with fixture output —
`packages/openburrow-adapters/tests/fixtures/` (the corpus) and
`tests/test_generic_interpretation.py` (28 tests, including the two adversarial
cases below). The PTY path is exercised end to end against a real child process
on a real pseudo-terminal by `tests/test_pty_reader.py` (9 tests).

**Design note:** the interpretation order is JSON → diff → usage limit → plan → plain
text, and it is fixed rather than configurable. A configurable order would mean two
installations interpreting the same harness output differently, which makes a bug
report untriable.

The code disagreed with this note until Stage 10 was actually verified: the
usage-limit check ran **first**, so a diff containing the digits `429` — a hunk
header, a port, a byte count — was replaced by a terminal error and the lane's
real output was discarded. The order is now the one written here, and the two
adversarial fixtures pin it down. The same pass found that `send_prompt` wrote
`"\n"` to a PTY, where a line is submitted on **CR**: no harness could ever have
received a prompt. See `FEATURE_STATUS.md`, Part III, for the full list.

### Stage 11 — First-class harness adapters

**Delivers:** Claude Code, Codex, OpenCode, Crush.

**Verified by:** `burrow adapters health` per harness; the smoke test against whichever
is installed.

**Design note:** Codex surfaces its own approval prompts, and OpenBurrow maps them onto
A2A's `auth_required` state rather than replacing them. **Layering, not replacing** —
a harness's own permission model is usually better than anything we would put in front
of it, and the operator configured it deliberately.

### Stage 12 — Remaining harness adapters

**Delivers:** Gemini, Aider, Goose, and the bring-your-own custom adapter.

**Verified by:** `burrow adapters list` reports availability without a harness
installed; a missing optional dependency disables one adapter rather than breaking the
registry.

**Design note:** Goose is deliberately kept as the awkward case. It is a black box with
no structured mode and no reliable prompt protocol, and its adapter is the honest floor
of what the protocol can do. Special-casing it away would make the protocol look
cleaner than it is.

Verified in a later pass, and the verification changed the code in four places. The
registry half of the claim holds — a simulated missing optional dependency drops one
adapter and leaves the other eight, and `describe_all()` works with nothing installed.
The adapter half did not hold at all:

* **The custom adapter's documented JSONL support was dead.** `_interpret` gated its
  JSON branch on `has_structured_mode`, which is a *declaration* feeding
  `capabilities.structured_output`. The custom adapter documents that its process "may
  emit JSONL on stdout, which is parsed like any structured harness", and cannot
  honestly claim structured output — so the flag was `False`, every frame became
  `text`, and `text` is not broadcast. Split into `has_structured_mode` (the
  declaration) and `parses_json_frames` (the decision). This is the same defect as
  Stage 11's `effective_capabilities`, one stage later and one layer down: a flag
  answering "can you?" used to answer "should you?".
* **Aider's commit anchor matched any hex token anywhere**, so a diff body could be
  recorded as the lane's git revision. Anchored to the line-leading `Commit` literal
  that Aider actually prints, which is also structurally safe: inside a unified diff
  every line carries a prefix, so no diff line can begin with it.
* **Goose was spawned with `goose run` and a prompt on stdin.** `run` reads stdin only
  via `-i -` and is one-shot by design; the interactive command is `goose session`. The
  prompt went nowhere. The design note above stands — Goose remains the fallback-parser
  case, and its `--output-format json|stream-json` is declined on purpose.
* **Gemini declared `structured_output=True` and could not reach it.** Headless mode
  needs a non-TTY or `-p`, and a lane is a PTY with no `-p`, so the TUI ran while the
  Agent Card advertised JSON. The declaration is corrected; restoring the capability
  needs a one-shot lane lifecycle, which is recorded as remaining.

Also found: the daemon kept a private set of broadcastable output kinds that had drifted
from the adapter's documented `translate_output` decision, dropping `status` — a hook
called by nothing for the second stage running. And the custom adapter extended
`HarnessAdapter` directly, carrying a fourth copy of every PTY defect Stage 10 had
already fixed, because a class that does not inherit a fix does not get it.

See `FEATURE_STATUS.md`, Part III, for the full list, and
`scripts/falsify_stage12.py` for the 13-row pass that shows each new test failing
against the code it replaced.

### Stage 13 — Credential isolation and spawn policy

**Delivers:** per-lane environment slices, `build_spawn_spec` purity, the policy gate,
and the custom-adapter trust gate.

**Verified by:** a lane never sees another lane's keys; `burrow policy test` dry-runs
the gate.

**Design note:** `build_spawn_spec` is pure — it returns the exact command, arguments,
environment, and working directory without executing anything, so the policy gate can
inspect a command *before* it runs. A builder with side effects could not be inspected,
which would make the gate advisory.

Custom adapters load behind an explicit `prompt`/`allow`/`deny` trust gate, because
loading arbitrary code from a repository checkout without asking is how you get a
supply-chain incident inside an agent orchestration tool.

---

## Part IV — Governance (stage 14)

### Stage 14 — The governance layer

**This is the stage the project exists for.** A2A, MCP, and ACP each document what they
do not specify, and the omissions overlap on one point: none of them can express who is
allowed to delegate what, what authority transferred, who is accountable, or what must
be audited across a trust boundary.

**Delivers:** the delegation ledger with all four invariants enforced, the detection
heuristics, the audit log with chain reconstruction, approvals, and the policy gate.

**Verified by:** the smoke test asserts authority narrows, a denial beats a wildcard
grant, a delegation without a human is refused, injection is detected, and a poisoned
lesson is caught independently of the Brain's classifier.

**Design note:** four decisions, each covered in its own ADR:

- Authority originates with a human and can only narrow
  ([ADR 0003](../architecture/adr/0003-governance-layer.md))
- Detection and enforcement are separate components with opposite defaults
  ([ADR 0009](../architecture/adr/0009-detection-vs-enforcement.md))
- `SilentAuthorityCreepError` **raises rather than trimming** the scope to the
  intersection, because the escalation attempt is the most interesting thing that can
  happen and discarding it destroys the signal that an upstream component is
  compromised
- `detect_poisoned_lesson` runs independently of the Brain's classifier — the one place
  in the codebase where duplication is the point

---

## Part V — Coordination (stages 15–17)

### Stage 15 — Claims and handoffs

**Delivers:** the four claim kinds, overlap detection, the claim board, and handoff
with ownership history.

**Verified by:** a directory claim overlapping a file claim is reported; a handoff
preserves `original_owner_lane`.

**Design note:** claims are **advisory, not locks**
([ADR 0008](../architecture/adr/0008-claims-not-locks.md)). Hard locks across
heterogeneous harnesses deadlock on crash, and a recovery path that requires deleting
a lock file by hand at 2am is a recovery path that will be run wrong. Enforcement is
negotiation; the cost is that two lanes *can* write the same file, and the mitigation
is visibility rather than prevention.

### Stage 16 — The plan

**Delivers:** the step graph, dependency queries, cycle detection, snapshots, rollback,
and diffing.

**Verified by:** `has_cycle()` catches a cycle via WHITE/GREY/BLACK DFS; `ready_steps()`
cascades correctly; a rollback restores the prior plan.

**Design note:** the plan's `original_owner_lane` is preserved through a handoff. The
current owner answers "who is doing this"; the original owner answers "who understood
the problem". Losing the second makes a plan that has been handed off twice
unreadable, and handed-off work is exactly where a plan gets confusing.

### Stage 17 — The Merge Radar

**Delivers:** intent extraction, the optional LLM judge, conflict prediction, and the
scan loop with deduplication and coverage reporting.

**Verified by:** a file collision is reported as certain; a judge outage reports zero
coverage rather than zero conflicts.

**Design note:** two decisions.

**Files come from records; descriptions may come from a model.** A model can never add
a file to an intent. The moment it can, the deterministic half of the Radar stops being
deterministic and every claim about "we detected this collision" stops being auditable.

**`RadarStats.judge_coverage` is reported.** If the judge is down, the coverage number
collapses and the report says the semantic signal did not exist for that period. A
report showing "0 semantic conflicts found" without showing "the judge was unreachable
for 100% of calls" would be actively misleading — and it is exactly the number that
ends up in a slide deck.

---

## Part VI — Memory (stages 18–19)

### Stage 18 — The Brain

**Delivers:** anchored entries, staleness sweeps, corroboration-based promotion,
AGENTS.md ingestion and export, and the CRDT projection.

**Verified by:** an entry whose anchored file changed goes stale; an entry from one
lane is not injected until corroborated; AGENTS.md round-trips without clobbering
hand-written content.

**Design note:** corroboration is **asymmetric**. A session-scoped observation is
trusted on first sight — the point is to reach the next lane within seconds, and the
blast radius of a wrong session lesson is one session. A repository-scoped entry needs
a second independent witness, because it outlives the session and will be read by
people who were not there. Corroboration from the *same* lane does not count: a harness
repeating itself is not a second opinion.

### Stage 19 — Lessons

**Delivers:** the lesson store, TTL expiry, hit-rate tracking, automatic eviction, and
the classifier with its optional LLM refinement.

**Verified by:** a lesson injected past the sample threshold with a low hit rate is
retired; an expired lesson is not injected.

**Design note:** eviction uses a hit **rate** with a minimum sample count, not an
absolute count. Retiring after one unhelpful injection would discard a good lesson that
simply did not apply; keeping on a low rate forever would let the prompt fill with
advice nobody follows. The mechanism exists because otherwise injected context grows
monotonically over a long session — and context is the scarcest resource in the system.

---

## Part VII — Observability (stages 20–22)

### Stage 20 — The event bus and file watching

**Delivers:** persist-then-publish, bounded subscriber queues, debounced worktree
watching, and the bus health metrics.

**Verified by:** a slow subscriber drops events rather than stalling a lane; one editor
save produces one bus message.

**Design note:** the publish order is **persist, then publish**, never the reverse. If
it were reversed, a crash between the two steps would leave a subscriber that acted on
an event the log does not contain — and since the entire recovery story is "replay the
log", that state is unrecoverable.

File watching debounces and collapses duplicates: a file saved three times is one
`modified` event. It emits a *summary*, not a filesystem journal.

### Stage 21 — Session reels

**Delivers:** asciinema v2 casts, the causal timeline with `caused_by`, the passive
recorder, the static HTML transcript scrubber, and redact-then-sign share links.

**Verified by:** a replay reconstructs a causal chain; redaction runs before signing;
the exported HTML opens from `file://` with no network requests.

**Design note:** **redact, then sign. Never the reverse.** Sign-then-redact either
breaks verification or produces a signature that attests to content the recipient
cannot see — and a signature that vouches for hidden content is worse than no
signature, because it is trusted.

The HTML viewer is a transcript scrubber rather than a terminal emulator, and the
`.cast` files ship alongside it. Writing an ANSI renderer for the browser would mean
either a large dependency or a subtly wrong one, and a subtly wrong replay is worse
than an honest transcript.

### Stage 22 — CLI, TUI, and the daemon control plane

**Delivers:** the `burrow` command surface, the Textual dashboard, the IPC layer, and
the supervision loop.

**Verified by:** `burrow --help` covers every command; a killed lane restarts with
backoff; shutdown ordering leaves no lost events.

**Design note:** three decisions.

**One output funnel.** Commands build a payload and hand it to `output.emit()`. That is
why `--json` cannot drift out of sync with human output: there is exactly one place
where the decision is made.

**Exit codes are semantic.** A governance refusal and a configuration typo both exit
non-zero, but they are different events — the first is the system working, the second
is the system being set up wrong. Collapsing them into `1` would make a CI job that
asserts "no governance violations occurred" impossible to write.

**The TUI is not the primary surface.** `burrow watch` renders the same data as plain
ANSI, so it works over any terminal and degrades gracefully when piped.

---

## After stage 22

Deliberately not numbered, because they are additive rather than sequential:

- **The Next.js frontend** — the static replay viewer and the relay dashboard
- **The relay** — FastAPI + WebSocket + Postgres, tenancy, quotas, fair scheduling
- **Test suites** — unit, integration, e2e, chaos, and governance markers are
  configured; coverage is the work
- **Packaging** — PyInstaller/Nuitka builds for air-gapped deployment
- **More harnesses** — the adapter protocol is the extension point

## Status

| Part | Stages | State |
|---|---|---|
| I — Foundations | 1–4 | **Built.** Workspace, models, persistence, adapter protocol, mock harness. |
| II — Protocols | 5–9 | **Built and verified.** Real HTTP between live lane servers; full negotiation; 3/3 smoke suites pass. |
| III — Harnesses | 10–13 | **Built.** All ten adapters, credential isolation, spawn policy gate. |
| IV — Governance | 14 | **Built and verified.** All four invariants asserted in the smoke test. |
| V — Coordination | 15–17 | **Built.** Claims, plan, Radar. |
| VI — Memory | 18–19 | **Built.** Anchoring, corroboration, lessons, eviction, AGENTS.md, CRDT. |
| VII — Observability | 20–22 | **Built.** Bus, file watching, reels, CLI, TUI, daemon. |
| After | — | Frontend, relay, and test suites outstanding. |

> **Read this table as "the code exists", not as "you can use it".** The state
> column records whether a stage is built, and every stage is. It does not record
> whether anything can reach the result. Several cannot: the governance engine
> passes all four invariants while `burrow governance audit` fails with
> `no handler for 'governance.audit'`, because 23 of the daemon methods the CLI
> calls were never registered.
>
> [**FEATURE_STATUS.md**](../../FEATURE_STATUS.md) is the companion to this table.
> It separates built from reachable from verified, names the 23 methods, and
> records what was actually run.

## Reading the code alongside this

| Stage | Where |
|---|---|
| 1–3 | `packages/openburrow-core/` |
| 4, 10–13 | `packages/openburrow-adapters/` |
| 5–9 | `packages/openburrow-a2a/`, `packages/openburrow-acp/` |
| 14 | `packages/openburrow-governance/` |
| 15–17 | `packages/openburrow-radar/`, `packages/openburrow-core/src/openburrow/core/models/plan.py` |
| 18–19 | `packages/openburrow-brain/` |
| 20–22 | `packages/openburrow-daemon/`, `packages/openburrow-cli/`, `packages/openburrow-reel/` |
