# Security policy

## Reporting a vulnerability

**Do not open a public issue.**

Email the maintainers, or use GitHub's private vulnerability reporting on this
repository. Include:

- what you found and where
- how to reproduce it
- what an attacker gains
- whether you have disclosed it elsewhere

We will acknowledge within 72 hours and aim to have a fix or a mitigation within 30
days. If you want credit, say so; if you want to stay anonymous, that is fine too.

## Threat model

OpenBurrow runs coding agents that execute code, read source, and hold credentials.
The threat model follows from that, and it is worth being explicit about what is
defended and what is not — an overstated security posture is worse than a documented
boundary.

### In scope

| Threat | Defence |
|---|---|
| **Prompt injection via a peer message** | `detect_injection` (23 markers), cross-boundary classification, explicit attribution on every injected message so a lane can tell a teammate from a file it just read |
| **Poisoned lesson** | `detect_poisoned_lesson` runs independently of the Brain's classifier — the one place where duplication is the point |
| **Authority escalation via sub-delegation** | Scope intersection; `SilentAuthorityCreepError` raises rather than trimming |
| **Delegation with no human behind it** | `Delegation` refuses to construct without an `authorized_by` naming a human |
| **Impersonation between lanes** | `detect_impersonation` on inbound messages, checked before injection |
| **Credential leakage between lanes** | Per-lane environment slices; a lane sees its own keys and nothing else |
| **Credential leakage into a reel** | Redaction runs over the whole bundle before anything is written or signed |
| **Credential leakage into logs** | `Settings.redacted_dump()` masks secrets; `scan_for_secrets` returns truncated matches only |
| **Supply chain via a custom adapter** | Custom adapters load behind an explicit `prompt`/`allow`/`deny` trust gate |
| **Local privilege escalation via the daemon socket** | `0600` applied explicitly, not left to the umask |
| **Tampered share link** | HMAC-SHA256 with constant-time comparison; expiry enforced |
| **Relay running without auth** | Startup refuses in production without a JWT secret |

### Out of scope

These are documented limits, not oversights. See
[docs/protocols/mcp.md](docs/protocols/mcp.md#if-you-need-tool-level-control) for the
reasoning.

| Not defended | Why, and what to do instead |
|---|---|
| **Tool calls inside a running lane** | MCP is passed through untouched. A lane that shells out to edit a denied file is not stopped by the ledger. **Run lanes in containers** — that is the only mechanism that holds when a harness does not cooperate. |
| **A harness's own sandbox configuration** | Codex has `--sandbox`, Claude Code has a permission model. OpenBurrow layers on top rather than replacing either. `burrow doctor` reports what it detects; it cannot force a mode on. |
| **A malicious harness binary** | An adapter spawns a process with the lane's credentials. If the harness is malicious, the lane is compromised. Vetting harnesses is the operator's responsibility. |
| **A malicious `openburrow.yaml` in a repo you clone** | Config can request wide authority, but it cannot grant it — authority originates with a human. It can, however, request a harness and flags. Review the config of a repo before running sessions in it, the same as you would review a `Makefile`. |
| **Denial of service from a peer** | Rate limiting exists per-peer; a determined local attacker can still exhaust resources. |
| **Side channels** | Timing, memory, and cache side channels are not addressed. |

## The most important thing

**A session reel contains real source code, real prompts, and possibly real
credentials.** Redaction is best-effort pattern matching. It will miss things.

Before sharing a reel outside your machine:

```bash
burrow reel preview ./reels/<session>    # shows what would be redacted
burrow reel share ./reels/<session>      # redacts, then signs
```

Read the preview. The report is generated from already-redacted text so the preview
itself cannot leak, but "no redactions applied" is not the same as "safe to share" —
it may mean the patterns did not match, not that nothing sensitive is present.

A share link is a signed, expiring pointer, not an access control system. It is a
reasonable way to hand a reel to a colleague and a bad way to protect a secret.

## Governance is not a sandbox

Worth repeating because it is the most likely misunderstanding:

> The governance layer constrains what a lane is **authorised** to do. A container
> constrains what a lane **can** do. They are complementary, and the container is the
> one that holds when a harness does not cooperate.

If you need enforcement that does not depend on a harness participating, use
containers. `docker/` provides images.

## Secret handling

- Provider keys are read from the environment or `.env`, never written to the database.
- Each lane's environment slice contains only its own harness's keys.
- `Settings.redacted_dump()` masks anything matching `key`, `secret`, `token`, or
  `password` before it is logged.
- `scan_for_secrets` returns **truncated** matches. A scanner that prints what it found
  has become a leak vector.
- Reel redaction covers provider key formats, JWTs, private keys, connection strings,
  bearer headers, `KEY=value` assignments, emails, and home-directory paths.

## Supported versions

Pre-1.0. Security fixes land on `main` and in the next release. There are no
backports yet, and claiming otherwise would set an expectation we cannot meet.

## Dependencies

Dependency updates are reviewed rather than automated blindly, because this project
spawns arbitrary subprocesses and holds credentials — a compromised transitive
dependency has an unusually large blast radius here.

Two pins in the tree are load-bearing and documented at their site:

- `uvicorn[standard]` — without the `[standard]` extra, uvicorn falls back to a pure
  Python HTTP parser with a keep-alive bug that presents as route-matching failures.
  See [docs/protocols/a2a.md](docs/protocols/a2a.md#a-note-on-the-http-parser).
- `a2a-sdk` — the reference implementation of the protocol we claim to speak.
