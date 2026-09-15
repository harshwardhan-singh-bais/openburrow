# The web frontend

Two things, one Next.js app, and they have almost nothing in common.

| | Replay viewer | Relay dashboard |
|---|---|---|
| Needs a backend | **no** | yes — the relay |
| Build | `next export`, static | server components + WebSocket |
| Input | a reel bundle (`manifest.json`, `reel.json`, `.cast` files) | a live relay connection |
| Works offline | yes | no |
| Audience | anyone reviewing a session | a team watching one now |

Keeping them in one app is a convenience, not an architectural claim. The replay
viewer must work with no infrastructure, so it is a static export that reads a bundle
directory. The dashboard needs a server, so it talks to the relay.

## The replay viewer

```
apps/web/app/reel/[id]/page.tsx
```

Reads a reel bundle and renders it. The static export means the whole viewer is HTML,
CSS, and JS with no API calls, so it opens from `file://`, works inside a corporate
network, and can be attached to an incident ticket.

**It duplicates a capability the Python exporter already has.** `openburrow-reel`
writes a self-contained `index.html` transcript scrubber. That is deliberate and the
two are not redundant:

| | Exported HTML | Next.js viewer |
|---|---|---|
| Dependencies | none | a build step |
| Causal chain tracing | yes | yes, with better layout |
| Cross-session comparison | no | yes |
| Comments and annotation | no | yes |
| Team sharing | a file | a URL |

The exported HTML is the one that always works. The Next.js viewer is the one that is
nicer when you have a build pipeline. Shipping only the second would mean the replay
feature requires Node, which contradicts the reason the format is a static export in
the first place.

### Rendering casts

The viewer renders the **transcript** — ANSI-stripped text — not a terminal emulator.
Writing an ANSI renderer for the browser means either shipping a large dependency or
writing a subtly wrong one, and a subtly wrong replay is worse than an honest
transcript.

For a faithful terminal replay, the bundle includes the `.cast` files and the viewer
links to `asciinema play`. Both are included because they answer different questions:
the transcript answers "what was said", the cast answers "what did it look like".

### The shared clock

Casts and the timeline both measure seconds since session start, which is what lets
the viewer put them on one axis. The scrubber moves both together, and clicking a
causal event jumps to its timestamp.

`caused_by` is what makes click-to-trace work. Without it the event list would be a
log; with it, you can ask why something happened and walk backwards.

## The relay dashboard

```
apps/web/app/(dashboard)/...
```

Needs a running relay. Shows live lanes across a team, with the same data
`burrow watch` shows locally.

### The CRDT

The plan and the Brain are Yjs documents. The browser applies updates directly with
`Y.applyUpdate` — no translation layer, which is the whole reason for choosing a CRDT
the frontend already speaks. `pycrdt` on the Python side produces Yjs-compatible
update bytes.

**The CRDT is a projection, not the source of truth.** See
[ADR 0007](../architecture/adr/0007-crdt-as-projection.md). The daemon's decision path
never reads the document, so a client can make its own view wrong and cannot make the
system act on it. `rebuild()` from the bus log is a supported operation.

### WebSocket protocol

The relay pushes bus events as they are appended. Messages are the same shape as the
IPC bus frames, so a reader of one can read the other.

Reconnection uses the last seen `seq` as a resume point, so a client that drops
connection resumes without gaps and without replaying. This works because the log is
append-only and `seq` is monotonic — the same property that makes local replay
possible.

## Development

```bash
cd apps/web
npm install
npm run dev            # http://localhost:3000
npm run build          # production
npm run export         # static replay viewer only
```

The relay must be running for the dashboard:

```bash
burrow relay serve --port 8787
OPENBURROW_RELAY_URL=http://localhost:8787 npm run dev
```

## Stack

| Choice | Why |
|---|---|
| App Router | The dashboard needs server components; the viewer needs static export. App Router supports both in one app. |
| Tailwind | The replay viewer's layout is dense and data-shaped. Utility classes keep the timeline components readable. |
| shadcn/ui | Components are copied into the repo rather than installed, so they can be changed. A component library you cannot edit is a component library you work around. |
| Yjs | See above. |
| Server components for the dashboard | The lane list and session header are server-rendered, so the first paint has data rather than a spinner. |

## Status

**Not yet built.** `apps/web/` is an empty directory tree. The replay viewer is the
priority, because it is the piece that makes reels useful to people who are not
running the CLI, and because it has no backend dependency and therefore no
prerequisites.

The relay dashboard depends on `openburrow-relay`, which is also not yet built. See
[../roadmap/README.md](../roadmap/README.md) for the sequence.
