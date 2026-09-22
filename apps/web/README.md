# OpenBurrow web

The browser surface for OpenBurrow. Two things it does that the terminal cannot,
and nothing else it tries to do:

- **Watch many lanes at once.** A terminal shows you one lane. The board shows
  you the state of every lane in a session, side by side, and highlights the ones
  that are waiting on a human.
- **Replay a session.** A reel is a per-lane terminal recording plus a timeline
  that records what caused what. The viewer makes the causal links walkable,
  which is the one question a wall of transcript cannot answer.

It is a **read-mostly** surface. It can stop a lane and send it a prompt; it
cannot create sessions, edit policy, or approve anything. Those live in the CLI,
where they have a terminal and an exit code.

---

## The decision that shapes everything: the daemon does not speak HTTP

This is the first thing to understand, because it explains the shape of the whole
directory.

The burrow daemon's control plane is **newline-delimited JSON-RPC over a Unix
domain socket** (POSIX) or a **named pipe** (Windows). That was chosen
deliberately: filesystem permissions *are* the access control, a socket at mode
`0600` has no port to scan and no firewall rule to get wrong, and
`nc -U .openburrow/burrow.sock` makes it debuggable by hand.

The consequence is that **a browser cannot reach the daemon, and should not be
able to.** So:

```
browser ──HTTP──▶ Next route handlers ──unix socket / named pipe──▶ burrow daemon
                                     ──HTTP─────────────────────▶ openburrow relay
```

The route handlers under `src/app/api/` are a **bridge**, not a proxy. They run
on the server, they are the only place that knows the socket path, and the browser
sees an ordinary JSON API. `src/lib/daemon-bridge.ts` is that bridge and is worth
reading before anything else — it is where the two rules that make the rest of
the app honest are enforced:

1. **A call either returns the daemon's result or throws.** There is no "return a
   plausible default" path. A dashboard that renders an empty lane list when the
   daemon is down is a dashboard that reports success.
2. **One connection per call.** The daemon's protocol is request/response. A
   pooled socket that dies between requests is an intermittent error nobody can
   reproduce.

This is also why `OPENBURROW_DAEMON_SOCKET` is deliberately **not** prefixed
`NEXT_PUBLIC_`. A control-plane endpoint in the browser bundle is a control-plane
endpoint someone eventually calls from a laptop on the same network.

The relay proxy uses `OPENBURROW_WEB_RELAY_URL`, not `OPENBURROW_RELAY_URL`.
Those are different things with the same name: the latter is the CLI's *client*
setting and its value is a WebSocket URL with a path (`ws://host:8787/ws`), while
this proxy needs an HTTP origin. Sharing the name would mean one of the two is
silently wrong, and it would surface as "could not reach the relay" with nothing
to explain why.

---

## Why polling, not streaming

The daemon *can* stream — `bus.stream` yields events over one long-lived request.
The frontend polls instead, and that is a real trade with a real reason.

A Next route handler that held a connection open for the life of a page would not
survive a serverless deployment and would leak a socket in every other one. So
the board calls `bus.tail?since_seq=N` on an interval.

The reason this is not a downgrade: **the cursor lives on the client.** A dropped
tick is recovered by the next one, because the next request asks for everything
after the last `seq` it actually saw. A stream that drops a frame is missing that
frame forever unless the server kept a buffer for you. Polling with an explicit
cursor is self-healing.

`burrow observability watch` keeps the streaming path, because a terminal can afford to hold a
connection.

---

## Layout

```
src/
  app/
    layout.tsx           root layout; inlines the no-flash theme script
    page.tsx             the board — open sessions + harness availability
    sessions/            session list, and the session detail (lanes + bus)
    reels/               reel index, and the replay viewer
    relay/               relay health, room membership, invite redemption
    system/              the daemon's self-report
    api/                 the bridge: route handlers, one directory per resource
  components/
    ui/                  primitives (button, card, badge, input, tabs, …)
    reel/                the player and the transcript renderer
    *.tsx                feature components
  lib/
    daemon-bridge.ts     ⭐ the socket bridge. Read this first.
    api.ts               the typed client the components use
    http.ts              the two response envelopes, and parameter validation
    reel.ts              cast parsing, SGR, O(1) scrubbing, the causal walk
    reel-files.ts        reading reels off disk, safely
    use-poll.ts          the polling and bus-cursor hooks
    format.ts            the tone maps, mirroring the CLI's palette
    yjs.ts               the live-document provider (CRDT)
    utils.ts             cn(), clipboard
  types/
    openburrow.ts        the domain vocabulary, mirrored from Python
```

---

## The two contracts

### The error envelope

Every failure, from anywhere, is:

```json
{ "error": { "code": "…", "message": "…", "hint": "…", "context": {} } }
```

and every success is `{ "data": … }`. The daemon, the relay and the bridge all
speak it, so `ApiError` in `lib/api.ts` has one parser and the UI has one error
panel.

`hint` is the field that matters. It is written to be actionable — "a socket
exists but nothing is listening, so a daemon likely crashed; try
`burrow daemon restart`" — and the UI renders it as the thing to read, not as
grey filler under a generic message. Collapsing a message and a hint into one
paragraph is how a UI ends up saying "an error occurred" when the daemon took the
trouble to say exactly what was wrong.

### The enum mirror

`src/types/openburrow.ts` is hand-written, not generated. That is deliberate: a
generated file would be correct and unreadable, and the parts that matter are the
*relationships* — which states are terminal, which transitions are legal — not
the string literals.

The cost is drift, and it is enforced:

```bash
python scripts/check_enum_parity.py
```

It parses the arrays out of the TypeScript and compares them against the Python
enums. **This is not hypothetical.** The first run of that script found
`PERFORMATIVE_REPLIES` in TypeScript claiming `reject` and `withdraw` were dead
ends, while the Python says they are answered by `propose` and `inform`. Both
languages were internally consistent and no test would have caught it; the effect
would have been a "counter this" button disabled in exactly the case where it is
most useful.

---

## Colour is a fact, not a convention

`lib/format.ts` holds the tone maps, and they mirror `output.STATE_STYLES` in the
Python CLI. A lane that is amber in the terminal is amber here, because two
surfaces that disagree about what "blocked" looks like are two surfaces people
stop trusting.

Two rules follow:

- **Components never name a colour.** They take a `StateTone` and look it up.
  That is what stops someone reaching for `text-amber-500` in one file and
  `text-yellow-600` in another.
- **Governance has its own hue.** `--governance` is not `--state-failed`,
  because a refusal is a deliberate act by a human or a policy and a crash is
  not. Colouring both red teaches people that the governance layer is a source of
  errors, which is the opposite of what it is.

The palette is OKLCH — one hue rotation over a fixed lightness ramp — so "make
the warning a bit warmer" is a number rather than a new colour picked by eye.
Dark-mode values are **re-picked, not reused**: the lightness that reads as amber
on white reads as mud on near-black.

---

## The reel viewer

The reel is the durable artefact of a session. The viewer exists for one
question: **why did this happen.**

A transcript tells you what each lane printed. It does not tell you that lane C
stopped because lane A refused a proposal. That link lives in `caused_by`, and
the viewer makes it walkable — click an entry and you get the chain back to its
root cause and the effects forward from it.

Four things worth knowing before changing it:

- **The playhead advances on `requestAnimationFrame` but commits to state at
  100 ms.** A reel is measured in tenths, so committing every frame would
  re-render everything sixty times a second to display the same tenth.
- **Lane tracks are built once.** `buildLaneTrack` concatenates a lane's whole
  output up front so scrubbing is a binary search plus a slice, not a
  re-concatenation of five thousand strings per frame.
- **Only the focused lane's text is sliced.** Every lane computes *how far* it
  has got (free, on a precomputed array), but only one lane's characters are
  extracted and parsed into spans.
- **`MAX_CHAIN_DEPTH = 64`, mirroring the Python.** The cap is not decorative: a
  cycle in `caused_by` — which a buggy recorder can produce — would otherwise
  hang the tab, and a viewer that can be hung by its own data is a viewer that
  gets closed and not reopened. A broken chain reports `dangling` rather than
  quietly starting mid-way.

`sgrSpans` is **not a terminal emulator** and says so. It handles SGR (colour,
weight, underline) and drops cursor movement, because the cast already records
what the screen looked like line by line. Anything it does not understand is
rendered as a **visible marker** rather than swallowed — a renderer that silently
drops what it cannot parse makes a corrupt transcript look clean, and a
clean-looking transcript is the whole product.

---

## Running it

```bash
cd apps/web
cp .env.example .env.local     # then edit
npm install
npm run dev
```

You need a daemon for anything to render. In the repo you want to watch:

```bash
burrow daemon start
```

If the board says the daemon is not answering, it prints the socket it tried and
the three usual causes. `OPENBURROW_REPO_ROOT` is the fix when `next start` runs
from somewhere that is not the repo — a dashboard that silently shows a
*different* repo's sessions is worse than one that fails.

### Build shapes

| Mode | Command | What it is |
| --- | --- | --- |
| Server | `npm run build && npm start` | Route handlers bridge the socket. The default. |
| Static | `OPENBURROW_WEB_STATIC=1 npm run build` | A folder of HTML. No route handlers, so it can only reach a daemon over HTTP via `NEXT_PUBLIC_OPENBURROW_API`. |

The static build is a flag rather than a fork, because two forks drift.

### Scripts

```bash
npm run dev        # next dev
npm run build      # next build
npm run lint       # eslint
npm run typecheck  # tsc --noEmit
```

---

## What this app deliberately does not do

- **It does not hold a daemon token.** There isn't one. The daemon's access
  control is the socket's file permissions.
- **It does not hold a relay token.** Relay tokens are per-member and per-room,
  and the browser sends its own. A server-side "service token" would be a
  credential that acts as every member at once — precisely the ambient authority
  the governance layer exists to prevent.
- **It does not proxy WebSocket upgrades.** `/stream` and `/doc` are dialled by
  the browser directly, because a Next route handler cannot proxy a long-lived
  bidirectional socket.
- **It does not merge CRDT documents.** Live documents are CRDTs; the relay
  stores snapshots and forwards updates. Merging is the clients' job, and the
  relay's snapshot is explicitly *not* authoritative.
- **It does not retry a write.** A retried `POST /rooms/{room}/events` would
  double-append from the caller's point of view. The relay dedupes by
  `(origin_repo, origin_seq)`, but that is a safety net, not a licence.
- **It does not invent a reason.** Stopping a lane asks for one and passes it
  through unmodified. "Why did this stop" is the single most asked question of a
  reel, and a UI that fabricates an answer poisons the record.
