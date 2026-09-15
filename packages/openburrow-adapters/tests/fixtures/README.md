# Adapter fixture corpus

Captured harness output, kept as files rather than inline string literals so
that the bytes the tests assert against are reviewable in isolation. A fixture
buried in a test function is a string the test author wrote to match the parser
they had just written; a fixture in a file is something a second person can look
at and say "no, Claude Code does not emit that".

The roadmap's Stage 10 entry claims the adapter is **verified by "each
interpretation path is exercised with fixture output"**. This directory is what
makes that claim checkable rather than aspirational.

## Files

| File | Interpretation path | Expected outcome |
| --- | --- | --- |
| `claude_stream_json.jsonl` | JSON, structured mode | five frames; text and usage extracted from the nested envelope |
| `diff_plain.txt` | diff | one `diff` output, `structured=False`, one `TaskArtifact.diff` |
| `plan_numbered.txt` | plan | one `plan` output, three steps |
| `plan_bulleted.txt` | plan | one `plan` output, three steps |
| `rate_limit_notice.txt` | usage limit | one terminal `error`, `data["usage_limit"]` set |
| `prose_plain.txt` | plain text | one `text` output — the honest fallback |
| `adversarial_diff_line_429.txt` | diff, *not* usage limit | one `diff` output |
| `adversarial_prose_rate_limiting.txt` | plain text, *not* usage limit | one `text` output |
| `goose_tui_chunk.txt` | fallback parser (Stage 12) | one `plan` output, three steps, `structured=False` |
| `gemini_tui_chunk.txt` | fallback parser (Stage 12) | one `text` output, `structured=False` |
| `aider_diff_and_commit.txt` | diff + commit anchor (Stage 12) | one `diff` output carrying `data["commit"]` |
| `adversarial_hex_in_diff.txt` | diff, *not* commit (Stage 12) | one `diff` output, no `commit` anywhere |
| `jsonl_unadvertised.jsonl` | JSON from an undeclared harness (Stage 12) | three structured frames; usage and `terminal` recovered |
| `unmapped_frame.json` | structured frame that maps to nothing (Stage 12) | one output, `data["openburrow:unmappedKeys"]` set |

## The two adversarial fixtures

These are the ones worth reading. Both encode a false positive that used to
destroy real work, and neither looks unusual at a glance.

`adversarial_diff_line_429.txt` is an ordinary diff whose hunk header reads
`@@ -429,6 +429,7 @@` and whose body contains `== 429`. The old usage-limit
detector substring-matched `"429"` anywhere in a chunk, so this diff was
replaced by a terminal rate-limit error and the lane's actual output was
discarded. The digits are a *line number*, not a status code.

`adversarial_prose_rate_limiting.txt` is a coding agent describing its own plan:
"I'll add rate limiting to the retry loop". The old detector substring-matched
`"rate limit"`, which is a prefix of `"rate limiting"`, so a plan became a
terminal error. The gerund is how a person *discusses* the concept; the noun and
the participle are how a provider *announces* it.

Both are detectors, and per
[ADR 0009](../../../../../docs/architecture/adr/0009-detection-vs-enforcement.md)
detectors are allowed to be noisy. What is not allowed is a noisy detector wired
to an action: the usage-limit path returns `terminal=True`, which discards the
message and marks the stream finished. A false positive there does not raise a
flag, it deletes work.

## `adversarial_hex_in_diff.txt` (Stage 12)

The same shape, one layer down. Aider's adapter recorded the lane's git commit
SHA by searching its output for `\b([0-9a-f]{7,40})\b` — any run of hex digits
with word boundaries, anywhere in the chunk. Aider's output is mostly diffs, and
a diff body is arbitrary source code, so this fixture contains three tokens that
match: a quoted digest, a seed, and a legacy id. Each would have been published
as the lane's commit, to a checkpoint layer whose entire purpose is to *not*
guess.

The fixture also adds a line that reads exactly like a commit notice:

```
+Commit 1234567 this line is fixture data, not a git revision
```

The anchor requires the line to *begin* with `Commit`, and inside a unified diff
every line carries a `+`, `-`, or space prefix — so no diff line can begin with
it. That is not merely narrower, it is structurally safe, and the fixture is what
holds it to that.

This file is paired with a falsification row: the old pattern is re-implemented
and asserted to *match* this fixture. Without that, a fixture the old code also
handles proves nothing.

## What is *not* stored here, and why

**ANSI-wrapped output.** A colour-capable TUI wraps every diff line in SGR
codes, and the base interpreter strips them before classification. The ANSI
variant is built by the test from `diff_plain.txt` rather than stored, because
the escape bytes are invisible in a diff view — a reviewer cannot tell a fixture
that lost its escapes from one that never had them. Constructing it in the test
also documents precisely which corruption is being defended against.

**Split frames.** A PTY read is not aligned to anything, so a real JSONL frame
arrives in pieces. The split offsets are chosen by the test rather than frozen
into a file, so the test can sweep every offset instead of asserting one.

**Codex `--json` output.** Not included. The adapter has a `_from_json` override
that assumes frames look like `{"id": ..., "msg": {"type": ...}}`, and that
assumption has never been checked against real output. Inventing a fixture that
matches the code would turn the guess into a passing test, which is worse than
leaving the gap visible. It needs a capture from a real Codex run.
