# CLI reference

`burrow` is the control surface. Every command is a thin wrapper over an IPC call
to the daemon; nothing holds state of its own.

```bash
burrow [GLOBAL OPTIONS] COMMAND [ARGS]...
```

## Global options

| Option | Effect |
|---|---|
| `--json` | Machine-readable JSON on stdout. Errors are JSON too. |
| `--quiet`, `-q` | Only the essential line of output. |
| `--verbose`, `-v` | Debug logging on stderr. |
| `--no-daemon` | Never contact the daemon. Commands that need it fail instead of trying. |
| `--yes`, `-y` | Answer yes to confirmation prompts. |
| `--repo <dir>` | Operate on this repository instead of walking up from the cwd. |
| `--version`, `-V` | Print the version and exit. |

`--json` and `--quiet` are mutually exclusive. There is one output mode, so
accepting both would mean silently discarding one of the flags you typed.

## Exit codes

The reason to have more than one. A governance refusal and a configuration typo both
exit non-zero, but they are different events — the first is the system working, the
second is the system being set up wrong. Collapsing them into `1` would make a CI job
that asserts "no governance violations occurred" impossible to write.

| Code | Meaning | Error codes |
|---|---|---|
| `0` | Success | — |
| `1` | Unclassified failure | anything unmapped |
| `2` | Usage error | bad flags, missing command |
| `3` | Setup problem | `repo_not_initialized`, `config_error`, `config_schema_error` |
| `4` | Daemon lifecycle | `daemon_not_running`, `daemon_already_running` |
| `5` | Human needed | `approval_required`, `approval_timeout` |
| `6` | Governance refused | `authority_scope_violation`, `silent_authority_creep`, `redelegation_denied`, `impersonation_detected`, `policy_violation`, `injection_error` |
| `7` | Protocol failure | `a2a_illegal_transition`, `a2a_protocol_error`, `a2a_conformance_error`, `acp_negotiation_error` |
| `8` | Harness failure | `harness_not_found`, `harness_crashed`, `adapter_error`, `output_parse_error` |
| `9` | Not found | `session_not_found`, `lane_not_found`, `claim_conflict` |
| `130` | Interrupted | SIGINT |

An unmapped code falls back to `1` rather than guessing, so it is visibly generic
instead of silently masquerading as a governance refusal.

```bash
# a CI gate that means what it says
burrow session start --json > session.json
burrow report --session "$(jq -r .id session.json)" --json > report.json
test "$(jq -r '.governance.unresolved_flags' report.json)" = "0"
```

## Workspace

```
burrow init [--name NAME] [--force]
burrow doctor [--fix] [--json]
burrow version [--check]
burrow config show | validate
burrow adapters list | health [HARNESS]
```

`init` writes `openburrow.yaml` and seeds `AGENTS.md`. It only writes lane templates
for harnesses that are **actually installed**, so the config never claims a capability
the machine does not have.

`doctor` checks in dependency order — Python, git, repo, config, paths, database,
adapters, protocols, then optional network — and stops reporting on a category whose
prerequisite failed, because a failure caused by an earlier failure is noise. `--fix`
repairs what can be repaired automatically and says what it could not.

## Daemon

```
burrow daemon start [--foreground] [--port N]
burrow daemon stop | restart | status | logs
```

`start` detaches by default: it re-execs itself with `--foreground` and returns, so
both modes share one code path. `status` pings the daemon rather than reading the PID
file, because a stale PID file is exactly the case where the naive check lies — the
file exists, the process does not, and every subsequent command fails confusingly.

## Sessions and lanes

```
burrow session start [--name NAME] [--template T] [--lanes N]
burrow session list | show [SESSION] | watch [SESSION] | close [SESSION]
burrow lane start --session S --name N --harness H --role R
burrow lane list | stop
burrow ask --lane L "prompt" [--wait]
```

`ask --wait` is worth explaining: it is not a custom blocking call. The task moves to
`input_required` in the A2A lifecycle, and the command polls for the transition. The
reply-wait mechanic *is* the protocol, which means it works identically whether the
lane is local or on the other side of the relay.

## Coordination

```
burrow take STEP | claim PATH | release PATH | claims
burrow offer PATH --from L --to L
burrow handoff STEP --to L [--force]
burrow plan show | edit | diff
```

`take` versus `offer` is the split worth knowing:

- **`take`** — the step is unowned. First claim wins, no ceremony.
- **`offer`** — the step is already owned. This opens a real ACP exchange, because
  taking someone's work is a negotiation, not an assignment.

`handoff --force` records the override in the audit log with who did it.

## Governance

```
burrow audit [--session S] [--delegation D] [--export PATH --format jsonl|csv|html]
burrow approvals list | respond ID --approve|--deny [--note TEXT]
burrow policy show | test --lane L --command "CMD"
```

`audit` reads only the immutable bus log, so a compliance export cannot contain
anything the operators could not see. `--delegation` traces a chain and shows what was
requested, what was held, and what was refused.

`policy test` dry-runs the gate without executing anything. The answer to "would this
be blocked" should not require trying it.

## Knowledge

```
burrow brain list [--path P] [--all] | add | retire ID | export | diff
burrow lessons list | retire ID
```

`brain diff` compares the Brain against `AGENTS.md` and reports what would change, so
exporting is a reviewed operation rather than a surprise.

## Observability

```
burrow watch [SESSION] [--interval S]
burrow logs [--lane L] [--bus-only] [--follow]
burrow report [--session S] [--json]
burrow export PATH [--format json|jsonl|html]
burrow replay [SESSION] [--speed N] [--from T]
burrow tui [--session S]
```

`watch` uses ANSI in-place refresh rather than a full TUI, so it works over any
terminal and degrades gracefully when piped into a log. `report` surfaces
`negotiation_precision` and per-lane `message_effectiveness` — a bus with high volume
and low effectiveness is expensive noise, and that is the number that tells you.

`replay` re-emits the append-only log in order. It cannot disagree with what happened,
because there is nothing else for it to disagree with.

`tui` needs `textual`. If it is missing you get a one-line message telling you how to
install it rather than an import traceback.

## Patterns

```bash
# start a pair, watch it, and get a report — the common loop
burrow session start --template pair && burrow watch

# what did governance refuse today?
burrow audit --export ./audit.jsonl --format jsonl
grep '"outcome": "denied"' ./audit.jsonl

# is this command going to be blocked?
burrow policy test --lane alice --command "git push --force"

# CI: fail if any governance flag went unresolved
burrow report --json | jq -e '.governance.unresolved_flags == 0' > /dev/null
```
