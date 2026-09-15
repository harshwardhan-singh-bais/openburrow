# openburrow-reel

Session recordings you can watch, search, trace, and share.

## Why it exists

Without a reel, the only record of a session is a scrollback buffer that dies with the
terminal — and the questions people actually ask afterwards have no answer:

- Why did lane C stop working?
- Which lane touched `auth.py` first?
- Was the approval actually granted, or did the lane just proceed?

## Two halves, one clock

| | What it shows | Format |
|---|---|---|
| **casts** | what a terminal showed, one per lane | asciinema v2 |
| **the timeline** | what *drove* it — claims, negotiations, governance, tasks | JSONL with `caused_by` |

Both measure time as **seconds since session start**, which is what lets a viewer put
them on one axis. Absolute timestamps would need a timezone to be read, and a viewer
that has to reason about timezones to align two streams will get it wrong.

Casts are the real asciinema format, so a replay plays in `asciinema play`, in `agg`,
and in the web player people already have. A replay format that needs its own viewer is
a replay format nobody watches.

## `caused_by` is what makes it causal

Every timeline entry may name the entry that produced it. Without it a reel is a list
of things that happened in an order. With it you can walk backwards:

> lane C stopped because it received a handoff, which happened because lane B hit an
> approval gate, which happened because a claim was refused.

That chain is what a person actually wants when they watch a session that went wrong.
`causal_chain()` walks it; the HTML viewer's click-to-trace uses it.

## The recorder never interprets

It observes and writes. That restraint is what makes a reel trustworthy as evidence:
the moment a recorder starts summarising or filtering, a reel stops being a *record* of
the session and becomes a *document about* the session — and a document can be wrong in
ways a record cannot.

Three consequences:

**Nothing is dropped for being uninteresting.** Governance flags, failed negotiations,
and crashed lanes are the parts people need. A reel that shows only the happy path is a
demo.

**Partial coverage is reported.** If a lane's cast cannot be opened, its output still
goes to the timeline and the manifest records the gap. Silently omitting a lane would
produce a reel that looks complete and is not.

**The search index is a convenience, not the source.** It is built from ANSI-stripped
text so queries match what a human read, while the cast keeps the raw bytes so the
replay is faithful. Both are kept because they answer different questions.

## Redact first, then sign. Never the reverse.

The ordering is the whole design, and getting it backwards produces one of two failures
that both look like success:

**Sign-then-redact** means the signature covers bytes no longer in the document.
Verification fails, or someone "fixes" it by verifying against the original — and now
the signature attests to content the recipient cannot see. A signature that vouches for
hidden content is worse than no signature, because it is trusted.

**Redact-then-sign** means the signature covers exactly what the recipient gets.

So `sign()` takes no content argument. Signing unredacted bytes is not expressible.

## The redaction report is the point

A silent redaction gives a person no way to judge whether the result is safe to share,
and "it looked fine" is not a security review.

`preview_redactions()` shows what would be stripped. The samples in the report are read
from the **already-redacted** text, so there is no code path by which a secret reaches
the report — a preview that shows the first 40 characters of what it is about to remove
has just printed the secret.

Covered: OpenAI/Anthropic keys, GitHub tokens, AWS keys, Slack tokens, JWTs, private
keys, connection strings, bearer headers, `KEY=value` assignments, emails, and
home-directory paths.

## The HTML viewer is a transcript, not a terminal

The exported `index.html` is self-contained — inline CSS and JS, no CDN, no network
requests — so it works from `file://`, inside a corporate network, and attached to an
incident ticket.

It renders **ANSI-stripped text**, not a terminal emulator. Writing an ANSI renderer for
the browser means either shipping a large dependency or writing a subtly wrong one, and
a subtly wrong replay is worse than an honest transcript.

The `.cast` files ship alongside it for a faithful replay. Both are included because
they answer different questions: the transcript answers "what was said", the cast
answers "what did it look like".

## A share link is not an access control system

It is a signed, expiring pointer — HMAC-SHA256 with constant-time comparison. A
reasonable way to hand a reel to a colleague; a bad way to protect a secret. A reel
contains real source code and real prompts, so read the redaction preview before a link
leaves the machine.

## Documentation

- [ADR 0007](../../../docs/architecture/adr/0007-crdt-as-projection.md) — the related decision about what is and is not a record
- [docs/web/README.md](../../../docs/web/README.md) — the Next.js replay viewer
