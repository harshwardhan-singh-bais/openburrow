# OpenBurrow — Feature Status

**What is built, what is reachable, what works, and what does not.**

This document answers four questions about all 22 stages of the roadmap:

| Question | Column |
|---|---|
| Is the code written? | **Built** |
| Can a user reach it from the CLI or the web app? | **Reachable** |
| Has it been run, and did it work? | **Works** |
| What is left? | **Remaining** |

These are deliberately four separate axes, because collapsing them is how a
project ends up claiming a feature works when nothing can call it. The sharpest
case in this codebase is the governance layer: its engine passes every invariant
in the smoke test, and two of the three CLI commands that front it fail with
`no handler for 'governance.audit'`.

## Method

Every claim marked **Works** below was produced by running the thing and reading
its output — not by reading the source and inferring. Every claim marked **Does
not work** includes the verbatim error. Where something was not run, it says so
instead of being counted either way.

The evidence commands are in [Verification log](#verification-log) at the end.

---

## The short version

| | Count |
|---|---|
| Stages whose user-facing surface works end to end | **12** of 22 |
| Stages built but unreachable, or only partly reachable | **8** |
| Stages built but not exercised in this pass | **2** |
| Leaf commands observed working | **18** |
| Leaf commands observed failing | **12** |
| Daemon methods the CLI calls that the daemon does not register | **23** |
| Packages with no tests at all | **6** of 11 |

Stage 10 moved out of "not exercised" in an earlier pass: the generic CLI
adapter is now covered by 46 tests, including a real child process on a real
pseudo-terminal. Stage 11 is exercised but stays **◐** rather than **●** for one
specific reason — its Codex wire format is assumed rather than captured. Stage 12
is now exercised too, and stays **◐** for the same shape of reason plus one of
its own: the four harness contracts were checked against each vendor's own
documentation, but no harness binary is installed here, and two of the four need
a lane lifecycle that does not exist yet.

See [Part III](#part-iii--harnesses-stages-1013) for what that verification
found: **twenty** defects across three stages, none of them visible by reading
the code, and eight of them documented behaviour that was not true.

The single most important number is the last-but-one. It is not a list of missing
features; it is a list of features that exist and cannot be called. The 12
observed failures are a subset of it — the remaining 11 methods were confirmed by
diffing the registry against its callers rather than by running each command,
because the method name never reaches a handler in either case.

---

## The one structural finding

The daemon is a JSON-RPC server over a Unix socket (POSIX) or a named pipe
(Windows). It registers **17** handlers. The CLI calls **40** distinct methods.

23 of them have no server-side handler:

```
approvals.list            governance.audit          plan.add_step
approvals.respond         governance.delegation_chain   plan.diff
brain.add                 handoff.execute           plan.get
brain.diff                handoff.offer             plan.remove_step
brain.list                lessons.list              plan.take
brain.retire              lessons.retire            reel.export
bus.message               claim.create              report.generate
claim.list                claim.release
```

What that looks like to a user:

```
$ burrow governance audit smoke-1
✗ no handler for 'governance.audit'
  code: openburrow.bus_error
```

The engine behind that method is complete and correct — the smoke test asserts
all four delegation invariants and all of them pass. The method simply was never
registered. This is not a roadmap gap; it is a wiring gap, and it is the reason
the roadmap's own Status table (which says Stage 14 is "Built and verified")
reads as more optimistic than the CLI does.

**There is no `--no-daemon` escape hatch.** `burrow --no-daemon governance audit`
fails identically, because `CliContext.require_daemon()` always dials the daemon
and has no local fallback. The hint printed on failure — "Commands that do not
need live lanes can run with --no-daemon" — is therefore false for every command
that uses it. It is true only for `burrow init`.

---

## Stage-by-stage

Legend: **●** works · **◐** built, not reachable · **○** built, not exercised ·
**—** not built

### Part I — Foundations (stages 1–4)

| Stage | Built | Reachable | Works | Notes |
|---|---|---|---|---|
| 1 — Workspace and configuration | ● | ● | ● | |
| 2 — Domain model | ● | ● | ● | |
| 3 — Persistence | ● | ● | ● | |
| 4 — Adapter protocol, mock harness | ● | ● | ● | |

**Stage 1 — Workspace and configuration.** `burrow init` creates
`openburrow.yaml`, `.openburrow/`, `AGENTS.md`, and the runtime directories.
`burrow config show` renders the merged configuration with sources;
`burrow config validate` reports `✓ configuration is valid (4 lane template(s))`.
`burrow doctor` runs 16 checks: **11 pass**, 4 are optional harnesses that are
not installed, and **1 fails** — `llm providers`, "none configured (Radar /
classifier disabled)". That failure is honest and expected on a machine with no
provider key; it is the only red check.

**Stage 2 — Domain model.** `scripts/check_enum_parity.py` reports
`12 declarations match` — the enums that are declared in more than one place
agree. The model layer is exercised heavily by the smoke test's governance
section.

**Stage 3 — Persistence.** `burrow init` creates a **16-table** SQLite schema at
`schema_version = 1`, in WAL mode. `burrow doctor` reports
`database  wal mode, schema v1`.

**Stage 4 — Adapter protocol and the mock harness.** Nine adapters are
registered (`aider`, `claude-code`, `codex`, `crush`, `custom`, `gemini`,
`goose`, `mock`, `opencode`). `burrow adapters list` prints their capability
matrix; `burrow adapters health` probes each. `scripts/check_imports.py` imports
every module in all 11 packages: **106 imported, 0 skipped, 0 failed**.

### Part II — Protocols (stages 5–9)

| Stage | Built | Reachable | Works | Notes |
|---|---|---|---|---|
| 5 — A2A transport | ● | ● | ● | |
| 6 — Agent Cards | ● | ● | ● | |
| 7 — Task lifecycle | ● | ● | ● | |
| 8 — ACP performatives | ● | ● | ● | |
| 9 — Lane servers, peer client | ● | ● | ● | |

This is the strongest part of the project. `scripts/smoke_test.py` runs **3/3
suites** and its first two cover stages 5–9 with real network I/O:

- **A2A, two live lanes, real HTTP** — 9 assertions. Two Starlette lane servers
  come up on ephemeral ports, exchange a real JSON-RPC message, and the Agent
  Card is checked for conformance, base skills, and the `openburrow:*`
  extensions. A reviewer lane's card gains the `approve` skill. The health
  endpoint answers and counts SSE subscribers.
- **ACP, a real conflict resolved by negotiation** — 4 assertions. The
  performative sequence is
  `lane_alice propose → lane_bob counter → lane_alice counter → lane_bob accept`,
  the outcome is `agreed=True escalated=False`, and the collision is recorded as
  avoided.

`burrow session show <id>` surfaces the result of all of this: the lane's A2A
endpoint, its Agent Card URL, and its git worktree path.

### Part III — Harnesses (stages 10–13)

| Stage | Built | Reachable | Works | Notes |
|---|---|---|---|---|
| 10 — Generic CLI adapter | ● | ● | ● | verified: 46 tests, real PTY round trip |
| 11 — Claude Code, Codex, OpenCode, Crush | ● | ● | ◐ | mappings verified; real binaries not installed |
| 12 — Gemini, Aider, Goose, custom | ● | ● | ◐ | contracts verified against upstream docs; binaries not installed here |
| 13 — Credential isolation, spawn policy | ● | ● | ◐ | policy gate works; isolation unverified here |

**Stage 10 — the generic CLI adapter now works, and it did not before.** The
roadmap claims this stage is "verified by exercising each interpretation path
with fixture output"; `fixtures/` was empty and referenced by nothing, so the
claim was unsupported. There is now a corpus under
`packages/openburrow-adapters/tests/fixtures/` and **46 tests** covering it,
including a real child process driven over a real PTY.

Six defects were found by building that verification, and every one of them was
invisible to reading the code:

| Defect | Why it mattered |
|---|---|
| Interpretation order ran usage-limit **first** | contradicted the roadmap's documented JSON → diff → usage-limit → plan order |
| A bare `429` matched anywhere | a diff touching line 429 became a *terminal* error; the work was discarded |
| `rate limit` matched the gerund | "I'll add rate limiting" became a terminal error |
| PTY excluded on Windows at the **call site** | `use_pty=True` did nothing on Windows; a pipe was used instead |
| `send_prompt` wrote `\n` to a PTY | a PTY submits on **CR**; every harness would hang in `readline()` forever |
| `_read_pty` called a blocking read on the loop | one idle harness starved the whole daemon event loop |

The `\n`-versus-`\r` defect is the one that settles the stage. Measured against
a real child: with `"hello\n"` the child never responded; with `"hello\r"` it
responded immediately. A PTY is a terminal, and the Enter key is CR. Since
`send_prompt` is the only way a prompt reaches a harness, the PTY path could not
have worked for any harness, on any platform — it had simply never been run.

Also fixed while verifying: `signal.SIGKILL` does not exist on Windows, so
`stop(force=True)` raised `AttributeError` inside its own `except` block and
leaked the process; `winpty` returns `str` where `ptyprocess` returns `bytes`,
so both the read and the write paths were type-wrong; and ANSI escape sequences
arriving in the same read as real output broke the `^`-anchored diff detector.

Two honest gaps remain, both recorded rather than papered over:

* **`prompt_as_arg` / `prompt_flag` are inert.** Declared in two adapters and
  read nowhere. They describe a one-shot `-p <prompt>` invocation, while
  OpenBurrow keeps a long-lived harness per lane. Documented as inert in
  `generic.py` rather than left to imply behaviour they do not have.
* **Windows PTY latency is seconds, not milliseconds.** Measured on this
  machine: a child's first output arrived ~3.7 s after it was written, and EOF
  was reported ~5 s after the last byte. That is a `winpty`/ConPTY property, not
  a defect here, but any stall detector must not treat a 2-second gap as a hang.

**Stage 11 — the mappings are verified; the binaries are not.**
`ClaudeCodeAdapter` had no `_from_json` override, so its frames mapped to kinds
(`"assistant"`, `"system"`) that `translate_output` does not broadcast — the
lane was talking and the bus heard nothing, with no error and no warning. The
same class of defect ran through all four adapters, and each one failed
*differently*, which is why none of them looked like the same bug:

| Adapter | What was silently lost |
|---|---|
| Claude Code | every frame — kind `"assistant"` is not broadcast |
| Codex | every frame — the `msg` envelope was never unwrapped, so text was always `""` |
| OpenCode | the cost, and every diff that did not start at offset 0 |
| Crush | nothing on the bus, but a second, divergent definition of ANSI stripping |

Fixed: Claude Code maps the four `stream-json` frame types, extracts text from
the nested `message` envelope (it was being stringified into a Python `repr`),
records usage from `message.usage`, and captures the init frame's `session_id`
so `is_resumable` has something to resume. Codex unwraps its envelope,
classifies the inner event, and recovers the terminal frame's text from
`last_agent_message`. OpenCode records cost independently of token counts,
finds a diff anywhere in a chunk rather than only at offset 0, and shares the
base ANSI stripper. Crush's private `_strip_ansi` — a narrower pattern that
missed the two-character escapes — is gone.

Two further defects were found and fixed in OpenCode, and both were
*documented behaviour that was not true*:

* **`effective_capabilities()` was called by nothing.** It exists so that a
  fallback to PTY mode reports degraded capabilities, and its docstring said the
  governance layer compares declared against observed. The Agent Card was built
  from `adapter.capabilities` directly, so a degraded OpenCode kept advertising
  `structured_output=True` and the capability-mismatch detector had nothing to
  catch. Promoted to a real method on the base class and wired at the call site.
* **The per-lane server port was not stable.** `_server_port_for` used
  `abs(hash(lane.id)) % 100`, and CPython randomises `hash()` for `str` per
  process. Measured over four runs of the same lane id: **16, 30, 33, 49** —
  a different port every restart, which is exactly the failure the docstring
  claimed to prevent. Now a `blake2b` digest, stable across processes.

**What is still unverified, and it matters:** the Codex `--json` envelope and
event names are **assumed from documentation**, not confirmed against a capture.
The binary is not installed here and no capture exists in the repo. The fixture
is named `codex_assumed_shape.jsonl` and the module docstring says so; a test
asserts the disclaimer is still present, so promoting this to "verified" takes a
deliberate deletion rather than a quiet edit. The Claude Code mapping, by
contrast, is driven end to end over a real PTY. **Do not treat the two as
equally established.** Verify against a real `codex --json` run before trusting
the field names.

**Stage 12 — one bug class, four symptoms, again — plus a detector that invented
git history.** The same shape as Stage 11, and the same reason none of it looked
like one bug:

| Adapter | What was silently lost |
|---|---|
| custom | every JSONL frame — the JSON branch was gated on a *declaration* the adapter cannot honestly make |
| custom | any `command:` written as a **list**, which is the documented form — stringified and shell-split into mangled tokens |
| Aider | the truth — any hex-looking token in a diff body was recorded as the lane's commit SHA |
| Aider | the commit itself — the notice arrives in its own read, and `text` is not broadcast |
| Goose | every prompt — `goose run` is one-shot and ignores stdin unless given `-i -` |
| Gemini | the Agent Card's honesty — `structured_output=True` while a TUI was what ran |
| all | the daemon's own broadcast set had drifted from the adapter's, dropping `status` |

**A flag that answered "can you?" was used to answer "should you?".**
`CustomScriptAdapter` documents that its process "may emit JSONL on stdout, which
is parsed like any structured harness". It also cannot claim structured output,
because it knows nothing about the command it runs — so its declaration is
`False`, and the JSON branch was gated on that same flag. Every frame was
classified `text`, a kind the bus does not broadcast. The lane was talking and
the bus heard nothing. Split into two flags: `has_structured_mode` is the
declaration, `parses_json_frames` is the decision, and they are allowed to
disagree. `response` was also missing from the candidate text fields, so a
`{"type": "result", "response": "..."}` frame mapped to an empty body. And a
frame that maps to *nothing* now names its keys under
`data["openburrow:unmappedKeys"]` and logs — because "this harness is quiet" and
"we do not understand this harness" were indistinguishable, and that ambiguity
has now cost four adapters.

**Aider's commit anchor matched anything.** The detector was
`\b([0-9a-f]{7,40})\b` — any hex run with word boundaries, anywhere in the chunk.
Aider's output is mostly diffs, and a diff body is arbitrary source code, so a
quoted digest, a seed, or a legacy id would each have been published as the
lane's git revision, to a checkpoint layer whose entire purpose is to *not*
guess. Aider's own source prints its notice from one place —
`f"Commit {commit_hash} {commit_message}"`, short SHA, bold — so the anchor is
now the line-leading literal. That is not just narrower: inside a unified diff
every line carries a `+`, `-`, or space prefix, so no diff line can *begin* with
`Commit` and the anchor cannot match diff content even in principle. Separately,
the notice usually arrives in its own read, and a lone notice classifies as
`text`, which is not broadcast — so the commit was detected correctly and then
dropped at the last step. It is now reclassified `status`.

**`--no-auto-commits` turned off the feature the module was built around.** The
adapter's docstring says "Because Aider commits as it goes, the adapter records
the commit SHA from its output so the checkpoint layer can anchor to real git
state rather than guessing." The spawn args passed `--no-auto-commits`, and
Aider's `--auto-commits` is an `argparse.BooleanOptionalAction` that **defaults
to True** — so the flag was not opting into anything, it was switching off
exactly that. Nothing else in OpenBurrow produces a commit SHA for a lane (no
`git commit` call site exists outside this adapter), so the extraction had no
input and the feature could not fire. The flag is gone; a lane works in its own
disposable worktree, which is the isolation the whole design rests on.

**The bring-your-own adapter carried a fourth copy of every PTY bug.** It
extended `HarnessAdapter` directly rather than `GenericCliAdapter`, so it never
inherited a single Stage 10 fix: `"\n"` written to a PTY where a line submits on
**CR** (no harness could ever have received a prompt), a blocking `self._pty.read`
inside an `async` generator, an unconditional `.decode()`, and a fresh
`GenericCliAdapter` instantiated inside `read_output` purely to borrow
`_interpret`. "Conservative" describes the *claims* this adapter makes, not the
code it runs; those were conflated. It now extends `GenericCliAdapter` and
overrides only where the command comes from and what it can promise.

**Goose's prompt went nowhere, twice over.** The adapter spawned `goose run` and
wrote prompts to its stdin. Goose's reference rules that out: `run` reads stdin
only via `-i -` ("Use `-` for stdin"), and `run` is one-shot by design — it
"exits the session automatically once the task is complete". The command the
reference describes as "Start or resume **interactive chat sessions**" is
`goose session`, which is what this adapter now spawns. Goose's
`--output-format json|stream-json` is real and is declined on purpose: it is a
`run` option, `json` is not streaming, and Stage 12's design note asks for Goose
to stay "the honest floor of what the protocol can do".

**Gemini declared structured output it could not reach.** `has_structured_mode =
True` with `--output-format json`, spawned under a PTY with the prompt written to
stdin. Gemini's reference: the positional prompt "Defaults to interactive mode
**in a TTY**", `-p` "Forces non-interactive mode", and headless mode is triggered
by a non-TTY environment **or** `-p`. `--output-format` is a headless option, so
headless never engaged, the flag was inert, and what ran was the TUI — while the
Agent Card advertised JSON. The declaration was the thing lying, and the same
flag drove line-delimited assembly, which would have split a TUI diff into one
output per line. The declaration is corrected rather than the feature restored:
making it work needs `-p <prompt>`, which is one-shot, and that is the trade-off
`prompt_as_arg` already documents as the reason it is inert.

**A hook called by nothing, for the second time in two stages.**
`translate_output` is documented as the adapter's "is this worth broadcasting"
decision and was called only by tests. The daemon kept its own hardcoded set,
and the two had drifted — the adapter broadcasts `status`, the daemon did not, so
a lane's state reports were classified correctly and dropped at the last step.
The daemon now consults the adapter, and still builds its own event shape,
because it emits to the *event* bus for observability rather than sending an A2A
message.

**What is still unverified, and it matters.** No harness binary is installed
here, so every contract above was checked against each vendor's own published
documentation rather than against a capture. Where that documentation was silent
— Goose's interactive prompt protocol, Gemini's `stream-json` event payloads —
the adapter declines the capability rather than guessing at it. The named gap is
a **one-shot lane lifecycle**: Gemini's structured mode and Goose's `run` mode
both need a fresh process per turn with a reader that survives the process
exiting between turns, and the daemon currently runs one long-lived reader per
lane. That is a lifecycle feature, not an adapter fix, and it is the first item
for whichever stage picks it up. The documented schemas are recorded in
`gemini.py` so the work is a mapping job rather than a research job.

Verified by 52 tests in `test_stage12_adapters.py` — including the registry
surviving a simulated missing optional dependency, and a test that the
documented `command:` list form produces the argv the user wrote — plus a
13-row falsification pass (`make falsify`, `scripts/falsify_stage12.py`) in which
every new test is shown to *fail* against the code it replaced. The falsification
pass itself caught one of its own rows being wrong: reverting only the Aider
regex made the check raise `IndexError` instead of asserting, which would have
counted as a pass. That verdict is now reported separately as **WEAK**, because a
revert that raises has not tested anything.

**Stage 13 — the policy gate works.** `burrow governance policy test "git push
--force"` returns `✓ allowed (default)`. This is a local, deterministic path and
it does not depend on the daemon. Per-lane credential slicing is asserted in the
smoke test's governance section only indirectly; it was not independently
verified here.

### Part IV — Governance (stage 14)

| Stage | Built | Reachable | Works | Notes |
|---|---|---|---|---|
| 14 — The governance layer | ● | ◐ | ◐ | engine ●, CLI ◐ |

**The engine is the best-tested component in the repository.** The smoke test
asserts **10/10** of its invariants:

```
[  ok  ] delegation narrows authority
[  ok  ] granted capability is allowed
[  ok  ] denial beats a wildcard grant
[  ok  ] non-denied capability still allowed
[  ok  ] a delegation without a human is refused
[  ok  ] prompt injection detected  — credential_access_attempt
[  ok  ] injection is treated as a violation
[  ok  ] benign message is not flagged
[  ok  ] secret-looking token detected
[  ok  ] poisoned lesson detected independently  — poisoned_lesson
```

**The CLI front-end does not work.** Of the three governance commands:

| Command | Result |
|---|---|
| `burrow governance policy test <cmd>` | ● works |
| `burrow governance audit <session>` | ✗ `no handler for 'governance.audit'` |
| `burrow governance approvals list` | ✗ `no handler for 'approvals.list'` |

`burrow governance audit --delegation <id>` additionally needs
`governance.delegation_chain`, also unregistered.

So: the accountability report that this stage exists to produce is computed
correctly and cannot be printed. That is the single highest-value thing to fix.

### Part V — Coordination (stages 15–17)

| Stage | Built | Reachable | Works | Notes |
|---|---|---|---|---|
| 15 — Claims and handoffs | ● | ◐ | ◐ | all five methods unregistered |
| 16 — The plan | ● | ◐ | ◐ | all five methods unregistered |
| 17 — The Merge Radar | ● | — | ○ | no CLI command, no tests |

**Stage 15 — claims and handoffs.** Every command in this group fails:

| Command | Result |
|---|---|
| `burrow coordination claims -s <session>` | ✗ `no handler for 'claim.list'` |
| `burrow coordination claim <path> -s <session>` | ✗ `no handler for 'claim.create'` |
| `burrow coordination release <id>` | ✗ `claim.release` unregistered |
| `burrow coordination offer <step> <lane>` | ✗ `no handler for 'handoff.offer'` |
| `burrow coordination handoff <step> <lane>` | ✗ `handoff.execute` unregistered |

**Stage 16 — the plan.** Same shape:

| Command | Result |
|---|---|
| `burrow coordination take <step> -s <session>` | ✗ `no handler for 'plan.take'` |
| `burrow coordination plan get` | ✗ `plan.get` unregistered |
| `burrow coordination plan diff` | ✗ `plan.diff` unregistered |
| `burrow coordination plan add-step` | ✗ `plan.add_step` unregistered |
| `burrow coordination plan remove-step` | ✗ `plan.remove_step` unregistered |

The `has_cycle()` DFS, `ready_steps()` cascade, snapshots, and rollback that the
roadmap describes are all in the model layer. None of them is reachable.

**Stage 17 — the Merge Radar.** The package imports cleanly (`5/5 modules`) and
has **no CLI command at all** and **no tests**. It is the least exercised stage
in the project. The `llm providers` failure in `burrow doctor` is what disables
the optional judge, so the deterministic half would run — but nothing invokes it.

### Part VI — Memory (stages 18–19)

| Stage | Built | Reachable | Works | Notes |
|---|---|---|---|---|
| 18 — The Brain | ● | ◐ | ◐ | four methods unregistered |
| 19 — Lessons | ● | ◐ | ◐ | two methods unregistered |

**Stage 18 — the Brain.** `brain.add`, `brain.list`, `brain.retire`, and
`brain.diff` are all unregistered, so every `burrow knowledge brain` command
fails. AGENTS.md handling does work: `burrow init` seeds it.

**Stage 19 — Lessons.** `lessons.list` and `lessons.retire` are unregistered.
The TTL expiry, hit-rate tracking, and eviction logic exist in
`packages/openburrow-brain/`; none of it is observable from the CLI.

### Part VII — Observability (stages 20–22)

| Stage | Built | Reachable | Works | Notes |
|---|---|---|---|---|
| 20 — Event bus, file watching | ● | ● | ● | bus ●; file watching untested |
| 21 — Session reels | ● | ◐ | ◐ | `reel.export` unregistered |
| 22 — CLI, TUI, daemon control plane | ● | ● | ● | daemon fixed on Windows |

**Stage 20 — the event bus works.** `bus.stream` and `bus.tail` are registered.
`burrow session watch <session>` streamed real events:

```
6  session.created    session 'smoke-1' created on branch burrow/smoke-1
```

`burrow observability logs <session>` returns `· no matching events` for a quiet
session, which is a correct answer rather than a failure. File watching
(`filewatch.py`) has no test coverage.

**Stage 21 — session reels.** `burrow observability replay <session>` runs
(`· replaying 0 event(s) at 1x`) but `burrow observability export` fails with
`no handler for 'reel.export'`, so there is no way to produce a bundle to replay.
The web app's `/reels` and `/reels/[id]` pages exist and are typechecked.

**Stage 22 — the control plane.** This is where the most work landed in this
pass. See [What was fixed](#what-was-fixed-in-this-pass).

| Command | Result |
|---|---|
| `burrow daemon start` | ● starts, detached, reports pid + endpoint |
| `burrow daemon start --foreground` | ● runs until signalled |
| `burrow daemon status` | ● pid, uptime, endpoint, sessions, lanes, requests |
| `burrow daemon logs` | ● reports the log path |
| `burrow session start --name <n>` | ● creates the session, branch, and lanes |
| `burrow session list` | ● |
| `burrow session show <id>` | ● lanes, A2A endpoints, card URLs, worktrees |
| `burrow session watch <id>` | ● streams bus events |
| `burrow session close <id>` | ● (interactive confirm) |
| `burrow observability logs <id>` | ● |
| `burrow observability replay <id>` | ● |
| `burrow observability report <id>` | ✗ `no handler for 'report.generate'` |
| `burrow observability export <id>` | ✗ `no handler for 'reel.export'` |
| `burrow observability watch <id>` | ✗ `no handler for 'approvals.list'` |
| `burrow observability tui` | not verified — needs an interactive terminal |

The TUI is unverified rather than broken. It is the one command whose output
cannot be captured by a non-interactive shell.

### After stage 22

| Item | State |
|---|---|
| Next.js frontend (`apps/web`) | ● source complete and typechecks; **never built** |
| The relay (FastAPI + WebSocket + Postgres) | ● built, 7 test files; not run end to end |
| Test suites | ◐ 410 tests pass; 7 packages have empty `tests/` |
| Packaging (PyInstaller / Nuitka) | — not started |
| Docker | ● Dockerfile present; **never built** |

**The frontend typechecks but has never been compiled.** `tsc --noEmit` exits 0
with no output across 49 source files — 7 pages, 14 API routes, 16 components,
and 9 modules under `src/lib/`. `.next/` does not exist, because `npm install`
failed partway through with `ECONNRESET` (the log is at
`apps/web/npm-install.log`), so `npm run build` has never completed.

**The frontend does *not* inherit the 23-method gap, and that is worth
recording.** `src/lib/daemon-bridge.ts` is a socket bridge — the daemon does not
speak HTTP, so each route handler opens a control-plane connection and relays the
result. The 10 routes that call it use exactly **11 distinct daemon methods, all
of which are registered**:

```
adapters.list   bus.tail      daemon.health   daemon.status   lane.prompt
lane.start      lane.status   lane.stop       session.list    session.show
task.list
```

So the web surface is narrower than the CLI's, and every method it reaches
exists. The 4 remaining routes (`/api/reels/*`, `/api/relay/*`) read reel files
from disk and proxy the relay rather than talking to the daemon at all. When
`npm run build` passes, the pages that depend on those 11 methods should work;
the ones that would have needed `report.generate` or `reel.export` were never
written, which is why the gap did not surface here.

**Test coverage is uneven and the distribution matters.**

| Package | Test files |
|---|---|
| `openburrow-relay` | 7 |
| `openburrow-core` | 5 |
| `openburrow-governance` | 3 |
| `openburrow-adapters` | 1 |
| `openburrow-a2a` | 0 |
| `openburrow-acp` | 0 |
| `openburrow-brain` | 0 |
| `openburrow-cli` | 0 |
| `openburrow-daemon` | 0 |
| `openburrow-radar` | 0 |
| `openburrow-reel` | 0 |

The two packages with zero tests that matter most are **`openburrow-daemon`** and
**`openburrow-cli`** — precisely where the 23-method gap lives. A single test
asserting that every method the CLI calls is registered on the daemon would have
caught all 23 at once. That test does not exist. This is the highest-leverage
test in the project.

---

## What works

Verified by running it in this pass.

| Area | Evidence |
|---|---|
| Workspace | `burrow init` → config, runtime dirs, AGENTS.md, 16-table schema |
| Configuration | `burrow config show`, `burrow config validate` |
| Environment | `burrow doctor` → 11 pass / 4 optional / 1 expected failure |
| Adapters | `burrow adapters list`, `burrow adapters health` |
| Daemon | `start`, `start --foreground`, `status`, `logs` |
| Sessions | `start`, `list`, `show`, `watch`, `close` |
| Policy gate | `burrow governance policy test "git push --force"` → allowed |
| A2A | smoke suite 1, 9/9 assertions, real HTTP between two live servers |
| ACP | smoke suite 2, 4/4 assertions, agreement reached |
| Governance engine | smoke suite 3, 10/10 assertions |
| Observability | `logs`, `replay` |
| Quality gates | `ruff` clean, 106/106 modules import, enum parity 12/12, falsification 13/13 |
| Python tests | `pytest` → **410 passed** |
| Frontend types | `tsc --noEmit` → clean |

## What does not work

Every line below is a verbatim failure.

```
burrow governance audit <session>        ✗ no handler for 'governance.audit'
burrow governance approvals list         ✗ no handler for 'approvals.list'
burrow coordination claims -s <s>        ✗ no handler for 'claim.list'
burrow coordination claim <path> -s <s>  ✗ no handler for 'claim.create'
burrow coordination take <step> -s <s>   ✗ no handler for 'plan.take'
burrow coordination offer <step> <lane>  ✗ no handler for 'handoff.offer'
burrow knowledge brain list              ✗ no handler for 'brain.list'
burrow knowledge lessons list            ✗ no handler for 'lessons.list'
burrow observability report <session>    ✗ no handler for 'report.generate'
burrow observability export <session>    ✗ no handler for 'reel.export'
burrow observability watch <session>     ✗ no handler for 'approvals.list'
```

Plus one command that does not exist at all — it was named in the top-level
`--help` epilog until this pass:

```
burrow watch                             ✗ No such command 'watch'.
```

The real command is `burrow observability watch`. Two stale references were
corrected in `main.py`; the same file also told users to run
`burrow logs --bus-only`, which is `burrow observability logs --bus-only`.

Three argument-signature inconsistencies also surfaced, none of which are
documented anywhere:

| Command | Actual signature |
|---|---|
| `governance audit` | positional session; `--session` is rejected |
| `coordination claims` | `-s/--session` |
| `observability logs` / `report` | positional session; `-s` is rejected |
| `governance approvals` | session is positional on the *subcommand*, not the group |
| `--no-daemon` | a global flag, valid only before the subcommand |

## What was fixed in this pass

The status above is not a description of a frozen codebase — several of the
"works" rows were "does not work" before this pass.

1. **The daemon had never started on Windows.**
   `IpcServer.start()` called `asyncio.start_server(handler, path=...)`. That
   keyword does not exist; it went through to `loop.create_server` and raised
   `TypeError: BaseEventLoop.create_server() got an unexpected keyword argument
   'path'` on every start. Every daemon-backed command failed behind it.
   Fixed by serving named pipes through `loop.start_serving_pipe` with a
   `Protocol`, and adding a `_Connection` abstraction so the framing protocol
   runs unchanged on both transports.

2. **`connection_made` is deferred.** The first fix read `protocol.connection`
   immediately after `await create_pipe_connection(...)` and got `None`, because
   the proactor schedules `connection_made` with `call_soon`. Replaced with a
   future that `connection_made` resolves.

3. **The Windows pipe name was not per-repo.** It was derived from the username
   alone, while the POSIX socket lives in the repo's own `.openburrow/`. Since
   Windows refuses a second server on a bound pipe name, the first repo to start
   a daemon would have owned the endpoint for the whole machine. Now namespaced
   by user *and* a hash of the repo root.

4. **`"SessionRow" object has no field "is_open"`.** `Session` declares
   `is_open` and `lane_count` as `@computed_field`, so `model_dump()` emits them;
   the row mapper passed them to `setattr` on the row. It only failed on the
   *update* path, because `SessionRow(**data)` on insert silently ignores extras
   — so `burrow session start` created a session and then exited non-zero.

5. **The daemon's readiness probe could never succeed**, because of (1). Fixed
   with it.

6. **Stale help text** naming `burrow watch` and `burrow logs`, neither of which
   exist.

Regression tests were added for (3) and (4): `test_paths.py` (4 tests) and
`test_repository_round_trip.py` (3 tests). The suite went from 272 to **279
passing**.

## What remains

Ordered by leverage, not by stage number.

**1. Register the 23 missing daemon methods.** This is the whole of the "does not
work" column. The engines exist; the handlers do not. Nothing else on this list
moves the CLI from 6-of-12 working families to 12-of-12.

**2. Add the drift test that would have caught all 23.**

```python
# Every method the CLI asks for must be registered on the daemon.
def test_cli_methods_are_registered():
    assert cli_methods() - registered_methods() == set()
```

The daemon has **zero** tests and the CLI has **zero** tests. One assertion in
either package closes a 23-method hole permanently.

**3. Build the frontend.** `tsc` is clean; `npm install` never completed. Until
`npm run build` passes, `apps/web` is source code rather than a product.

**4. Give the `--no-daemon` hint a local path, or delete it.** As written it
promises something `require_daemon()` does not deliver for any command that
prints it.

**5. Fill in the empty test directories.** 7 of 11 packages have no tests.
`openburrow-a2a` and `openburrow-acp` are the surprising ones — they are the
best-verified stages in the project, and the verification lives entirely in
`scripts/smoke_test.py` rather than in the packages themselves.

**6. Exercise the adapters against a real harness.** Stages 10–12 are unverified
here only because no harness is installed. Add the `fixtures/` that stage 10
claims to be verified against.

**7. Resolve the 111 mypy errors in 25 files.** Down from 129 in 34. 80 of the
remaining are the SQLModel `column == value` cluster in `repository.py` (46) and
`store.py` (34), which is a known upstream typing limitation and is deliberately
not bulk-suppressed.

**8. Build the Docker image.** The Dockerfile has never been built. `uv.lock`
now exists, so `uv sync --frozen` — which all five CI jobs run — can finally
resolve.

**9. Reconcile `.python-version` (3.12) with the local venv (3.13.14).**

**10. Decide what to do about Stage 17.** The Merge Radar has no CLI surface and
no tests. Either expose it or record it as deliberately dormant.

---

## Verification log

Run on Windows, Python 3.13.14, in a scratch repo at `%TEMP%\obtest3`.

```
$ burrow init --force
  OpenBurrow initialised          runtime, worktrees, harnesses, 4 lanes
$ burrow config validate
  ✓ configuration is valid (4 lane template(s))
$ burrow doctor
  11 ✓ · 4 · (optional) · 1 ✗ (llm providers)
$ burrow daemon start
  ✓ daemon started (pid 13724)
  · endpoint: \\.\pipe\openburrow-Acer-f2ccc219cb98
$ burrow daemon status
  pid 27096 · up 28s   sessions 0   lanes 0   requests 2
$ burrow session start --name smoke-1
  session started   smoke-1 (sess_01M2JWBRMSBY)  active
  branch burrow/smoke-1 · 1 lane(s)
$ burrow session show smoke-1
  lanes: claude-code-2  http://127.0.0.1:7400
         card: http://127.0.0.1:7400/.well-known/agent-card.json
$ burrow session watch smoke-1
  6  session.created   session 'smoke-1' created on branch burrow/smoke-1
$ burrow governance policy test "git push --force"
  ✓ allowed    (default)
$ python scripts/smoke_test.py
  3/3 suites passed                A2A 9/9 · ACP 4/4 · governance 10/10
$ pytest -q
  410 passed
$ python scripts/check_imports.py
  106 imported, 0 skipped, 0 failed
$ python scripts/check_enum_parity.py
  enum parity: 12 declarations match
$ ruff check packages scripts
  All checks passed!
$ node node_modules/typescript/bin/tsc --noEmit    # in apps/web
  (no output, exit 0)
```

### A note on how the daemon was tested

`burrow daemon start` spawns a detached process. In this sandbox, orphaned
background processes are reaped when the launching shell exits, so a detached
daemon is alive for the duration of the command that started it and gone by the
next one. That was confirmed rather than assumed: a detached daemon answered
`daemon status` for **69 consecutive seconds** inside a single shell invocation,
and was gone in the following one. Spawn-flag isolation
(`CREATE_NEW_PROCESS_GROUP`, `DETACHED_PROCESS`, `CREATE_BREAKAWAY_FROM_JOB`, and
all combinations) showed children surviving their parent in every case, which
rules out the daemon as the cause.

The daemon was therefore verified by running it as a tracked background process,
under which it stayed up across many separate invocations. **The daemon does not
crash; the sandbox reaps it.** This distinction matters, because it is the
difference between a bug report and an environment note.

---

## See also

- [`docs/roadmap/README.md`](docs/roadmap/README.md) — the 22 stages, 258 items,
  and the design note behind each
- [`docs/architecture/overview.md`](docs/architecture/overview.md) — how the
  pieces fit together
- [`docs/architecture/adr/`](docs/architecture/adr/) — the ten decisions that
  shaped the build, including the three this document leans on
