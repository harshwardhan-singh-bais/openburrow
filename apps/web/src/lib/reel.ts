import {
  CAST_INPUT,
  CAST_OUTPUT,
  EMPTY_BUNDLE,
  type CastEvent,
  type CastHeader,
  type ParsedCast,
  type ReelBundle,
  type ReelLane,
  type ReelManifest,
  type TimelineEntry,
} from "@/types/openburrow";

/**
 * Session reel parsing and interrogation.
 *
 * Three jobs, in increasing order of how much they matter:
 *
 * 1. **Parse.** asciinema v2 is a header line followed by JSON arrays, and the
 *    only interesting decision is what to do with a truncated final line — which
 *    is what a cast looks like when the daemon was killed mid-write. The answer
 *    is to keep every complete event and set `truncated`, not to throw: a reel
 *    whose last line is cut is still the most useful artefact of an incident.
 *
 * 2. **Render ANSI.** A transcript with raw escape sequences in it is unreadable,
 *    and stripping them loses the thing that made the transcript legible. So the
 *    escapes are interpreted into spans. This is *not* a terminal emulator: it
 *    handles SGR (colour and weight) and deliberately ignores cursor movement,
 *    with unsupported sequences rendered as a visible marker rather than
 *    silently dropped. See the note on `sgrSpans`.
 *
 * 3. **Walk the causal chain.** `caused_by` links entries into a graph. The walk
 *    is the reason the reel exists: "lane C stopped" is a fact, "lane C stopped
 *    because it received a handoff, which happened because lane B hit an approval
 *    gate, which happened because a claim was refused" is an answer.
 */

// ---------------------------------------------------------------------------
// Cast parsing
// ---------------------------------------------------------------------------

/**
 * Parse an asciinema v2 cast.
 *
 * Tolerant of a truncated final line on purpose. The recorder flushes every 32
 * events and a killed daemon leaves a partial write; refusing to load the file
 * would mean the recording of the crash is unavailable precisely when it is
 * wanted.
 */
export function parseCast(text: string): ParsedCast {
  const lines = text.split("\n");
  let header: CastHeader = { version: 2, width: 80, height: 24 };
  const events: CastEvent[] = [];
  let truncated = false;
  let sawHeader = false;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;

    if (!sawHeader) {
      sawHeader = true;
      try {
        const parsed: unknown = JSON.parse(line);
        if (parsed && typeof parsed === "object") {
          header = parsed as CastHeader;
        }
      } catch {
        // A cast whose header is unparseable is still worth reading for its
        // events, with a default geometry. Marking it truncated is honest: the
        // caller can see that something was wrong at the top of the file.
        truncated = true;
      }
      continue;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      // The last line of a killed recording. Anything earlier being malformed is
      // a corrupt file, and either way the correct response is the same: keep
      // what parsed, flag the rest.
      truncated = true;
      continue;
    }

    if (!Array.isArray(parsed) || parsed.length < 3) {
      truncated = true;
      continue;
    }

    const [time, kind, data] = parsed as [unknown, unknown, unknown];
    if (typeof time !== "number" || typeof kind !== "string") {
      truncated = true;
      continue;
    }
    events.push([time, kind, typeof data === "string" ? data : String(data ?? "")]);
  }

  events.sort((a, b) => a[0] - b[0]);
  return { header, events, truncated };
}

// ---------------------------------------------------------------------------
// Bundle parsing
//
// The bundle arrives as untrusted JSON — from a file the user dropped on the
// page, or from a share link. Every field is checked and defaulted rather than
// asserted, because a viewer that throws on a malformed reel shows the user
// nothing at all, and the failure mode of "render what parsed" is far better.
// ---------------------------------------------------------------------------

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function parseTimelineEntry(raw: unknown): TimelineEntry {
  const record = asRecord(raw);
  return {
    t: asNumber(record.t),
    seq: asNumber(record.seq),
    type: asString(record.type),
    lane_id: asString(record.lane_id),
    summary: asString(record.summary),
    caused_by: asString(record.caused_by),
    id: asString(record.id),
    payload: asRecord(record.payload),
  };
}

function parseLane(raw: unknown): ReelLane {
  const record = asRecord(raw);
  const events: Array<[number, string]> = [];
  if (Array.isArray(record.events)) {
    for (const item of record.events) {
      if (!Array.isArray(item) || item.length < 2) continue;
      const time = asNumber(item[0], -1);
      if (time < 0) continue;
      events.push([time, asString(item[1])]);
    }
  }
  return {
    id: asString(record.id),
    title: asString(record.title, asString(record.id)),
    has_cast: record.has_cast === true,
    reason_missing: record.reason_missing == null ? null : asString(record.reason_missing),
    output_events: asNumber(record.output_events),
    input_events: asNumber(record.input_events),
    events,
  };
}

export function parseBundle(raw: unknown): ReelBundle {
  const record = asRecord(raw);
  if (Object.keys(record).length === 0) return EMPTY_BUNDLE;

  const session = asRecord(record.session);
  return {
    session: {
      id: asString(session.id),
      name: asString(session.name),
      duration_s: asNumber(session.duration_s),
      started_at: asString(session.started_at),
    },
    lanes: Array.isArray(record.lanes) ? record.lanes.map(parseLane) : [],
    timeline: Array.isArray(record.timeline) ? record.timeline.map(parseTimelineEntry) : [],
    coverage: Array.isArray(record.coverage)
      ? record.coverage.map((item) => {
          const entry = asRecord(item);
          return {
            lane_id: asString(entry.lane_id),
            has_cast: entry.has_cast === true,
            reason_missing:
              entry.reason_missing == null ? null : asString(entry.reason_missing),
          };
        })
      : [],
  };
}

export function parseManifest(raw: unknown): ReelManifest | null {
  const record = asRecord(raw);
  if (Object.keys(record).length === 0) return null;
  return {
    session_id: asString(record.session_id),
    session_name: asString(record.session_name),
    exported_at: asString(record.exported_at),
    exporter: asString(record.exporter),
    cast_files: asRecord(record.cast_files) as Record<string, string>,
    a2a_trace_file: asString(record.a2a_trace_file),
    timeline_file: asString(record.timeline_file),
    plan_file: asString(record.plan_file),
    audit_file: asString(record.audit_file),
    metrics_file: asString(record.metrics_file),
    lane_count: asNumber(record.lane_count),
    event_count: asNumber(record.event_count),
    negotiation_count: asNumber(record.negotiation_count),
    governance_event_count: asNumber(record.governance_event_count),
    duration_seconds: asNumber(record.duration_seconds),
    total_cost_usd:
      typeof record.total_cost_usd === "number" ? record.total_cost_usd : null,
    // These four were being dropped on the floor. `signed`, `signature` and
    // `expires_at` are the ones that matter: a viewer that cannot see a link is
    // signed or about to expire cannot warn anyone, which is the entire point of
    // scoping a share link. `is_expired` is read from the manifest rather than
    // recomputed here, so the viewer agrees with the server instead of comparing
    // against the browser's clock.
    signed: record.signed === true,
    signature: asString(record.signature),
    expires_at: typeof record.expires_at === "string" ? record.expires_at : null,
    is_expired: record.is_expired === true,
    allowed_orgs: Array.isArray(record.allowed_orgs)
      ? record.allowed_orgs.map((org) => asString(org))
      : [],
    public: record.public === true,
    viewer_version: asString(record.viewer_version),
  };
}

// ---------------------------------------------------------------------------
// ANSI
// ---------------------------------------------------------------------------

// Written with `\u001b` escapes rather than literal escape bytes, which is
// why this needs no `no-control-regex` suppression: the rule looks for
// control characters in the pattern source, and there are none here.
const CSI_PATTERN = /\u001b\[([0-9;?]*)([A-Za-z])/g;
const OSC_PATTERN = /\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)/g;

/** Remove every escape sequence. For summaries, tooltips and search. */
export function stripAnsi(text: string): string {
  return text.replace(OSC_PATTERN, "").replace(CSI_PATTERN, "");
}

/**
 * The 16 ANSI colours, plus the 216-colour cube and greyscale ramp.
 *
 * Fixed values, not theme variables. A recording of a terminal is a transcript
 * of a terminal — it looked like *that*, on that machine, with that palette — and
 * re-theming it to match the surrounding page would be a quiet lie about what the
 * transcript shows. The replay pane therefore keeps a dark terminal surface in
 * both themes, and this is why.
 */
const ANSI_16 = [
  "#1c1c1c", "#cc3e28", "#4f9c31", "#c99b2e",
  "#2f6fb5", "#9b4dca", "#2f9b9b", "#c7c7c7",
  "#5c5c5c", "#f16d5c", "#7ddc63", "#f5d05c",
  "#5aa9e6", "#c98bdc", "#5fd4d4", "#ffffff",
];

function ansi256(index: number): string {
  if (index < 16) return ANSI_16[index] ?? ANSI_16[7]!;
  if (index >= 232) {
    // Greyscale ramp: 232..255 maps to 8..238 in steps of 10.
    const level = 8 + (index - 232) * 10;
    return `rgb(${level},${level},${level})`;
  }
  // 6x6x6 colour cube, 16..231.
  const cube = index - 16;
  const steps = [0, 95, 135, 175, 215, 255];
  const r = steps[Math.floor(cube / 36) % 6] ?? 0;
  const g = steps[Math.floor(cube / 6) % 6] ?? 0;
  const b = steps[cube % 6] ?? 0;
  return `rgb(${r},${g},${b})`;
}

export interface SgrSpan {
  text: string;
  color?: string;
  background?: string;
  bold?: boolean;
  dim?: boolean;
  italic?: boolean;
  underline?: boolean;
  /** Set when the transcript contained a sequence this renderer does not handle. */
  unsupported?: string;
}

/**
 * Interpret SGR escapes into styled spans.
 *
 * **This is not a terminal emulator, and saying so is the point.** It handles
 * `SGR` (colour, weight, underline) and drops cursor movement and erase
 * sequences, because rendering a transcript is not the same as replaying one: the
 * cast already records what the screen *looked like* line by line, so honouring
 * cursor moves would mean re-implementing a VT100 to arrive at the same output.
 *
 * Anything that is neither SGR nor a recognised control sequence is emitted as a
 * span with `unsupported` set, so the viewer can show it as a visible marker. A
 * renderer that silently swallows what it does not understand is a renderer that
 * makes a corrupt transcript look clean.
 */
export function sgrSpans(input: string): SgrSpan[] {
  const spans: SgrSpan[] = [];
  const state: Omit<SgrSpan, "text"> = {};
  let cursor = 0;

  // Strip OSC first: title-setting and hyperlinks have no visual effect on the
  // characters and their payloads can contain anything, including `[`.
  //
  // Everything below indexes into `text`, never into `input`, and that is not a
  // stylistic choice. The scan measures positions in the OSC-stripped string, so
  // a slice taken from `input` with one of those positions is offset by the length
  // of every OSC that preceded it. Slicing `input` here rendered
  // `\x1b]0;evil [31m\x07visible` as the escape junk `\x1b]0;evi` — the text the
  // reader wanted, replaced by the bytes that were supposed to be invisible, and
  // a PTY sets a window title on essentially every session.
  const text = input.replace(OSC_PATTERN, "");

  const flush = (end: number) => {
    if (end <= cursor) return;
    spans.push({ ...state, text: text.slice(cursor, end) });
  };

  CSI_PATTERN.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CSI_PATTERN.exec(text)) !== null) {
    const [sequence, params, final] = match as unknown as [string, string, string];
    flush(match.index);

    if (final === "m") {
      applySgr(state, params);
    } else {
      // Cursor movement, erase, scroll. Rendered as a marker so a reader can see
      // that something was there rather than silently losing it.
      spans.push({ text: "", unsupported: sequence });
    }
    cursor = match.index + sequence.length;
  }
  flush(text.length);

  return spans.filter((span) => span.text.length > 0 || span.unsupported);
}

function applySgr(state: Omit<SgrSpan, "text">, params: string): void {
  const codes = params.split(";").map((part) => (part === "" ? 0 : Number(part)));

  for (let index = 0; index < codes.length; index += 1) {
    const code = codes[index];
    if (code === undefined || Number.isNaN(code)) continue;

    if (code === 0) {
      delete state.color;
      delete state.background;
      delete state.bold;
      delete state.dim;
      delete state.italic;
      delete state.underline;
    } else if (code === 1) state.bold = true;
    else if (code === 2) state.dim = true;
    else if (code === 3) state.italic = true;
    else if (code === 4) state.underline = true;
    else if (code === 22) {
      delete state.bold;
      delete state.dim;
    } else if (code === 23) delete state.italic;
    else if (code === 24) delete state.underline;
    else if (code >= 30 && code <= 37) state.color = ANSI_16[code - 30];
    else if (code === 39) delete state.color;
    else if (code >= 40 && code <= 47) state.background = ANSI_16[code - 40];
    else if (code === 49) delete state.background;
    else if (code >= 90 && code <= 97) state.color = ANSI_16[code - 90 + 8];
    else if (code >= 100 && code <= 107) state.background = ANSI_16[code - 100 + 8];
    else if (code === 38 || code === 48) {
      // Extended colour: `38;5;N` or `38;2;R;G;B`.
      const target = code === 38 ? "color" : "background";
      const mode = codes[index + 1];
      if (mode === 5) {
        const value = codes[index + 2];
        if (value !== undefined) {
          state[target] = ansi256(value);
          index += 2;
        }
      } else if (mode === 2) {
        const [r, g, b] = [codes[index + 2], codes[index + 3], codes[index + 4]];
        if (r !== undefined && g !== undefined && b !== undefined) {
          state[target] = `rgb(${r},${g},${b})`;
          index += 4;
        }
      }
    }
    // Everything else (blink, reverse, conceal, fonts) is deliberately ignored:
    // it does not change the text, and implementing it would not make the
    // transcript more readable.
  }
}

// ---------------------------------------------------------------------------
// Lane tracks — O(1) scrubbing
// ---------------------------------------------------------------------------

export interface LaneTrack {
  lane: ReelLane;
  /** Every output event concatenated, in order. */
  joined: string;
  /** Character offset into `joined` after each event. `offsets[i]` ends event i. */
  offsets: number[];
  /** The time of each event. */
  times: number[];
  /** Total characters, before the display cap. */
  totalChars: number;
}

/**
 * Precompute a lane's output so scrubbing is O(1) instead of O(n).
 *
 * Concatenating five thousand strings on every animation frame is the difference
 * between a scrubber that tracks the cursor and one that stutters, and the fix is
 * to do the concatenation once and index into it.
 */
export function buildLaneTrack(lane: ReelLane): LaneTrack {
  const joined = lane.events.map(([, text]) => text).join("");
  const offsets: number[] = new Array(lane.events.length);
  const times: number[] = new Array(lane.events.length);
  let running = 0;
  for (let index = 0; index < lane.events.length; index += 1) {
    const event = lane.events[index]!;
    running += event[1].length;
    offsets[index] = running;
    times[index] = event[0];
  }
  return { lane, joined, offsets, times, totalChars: running };
}

/** Characters rendered at once. Beyond this the DOM cost outruns the value. */
export const MAX_RENDER_CHARS = 60_000;

/** Text visible at time `t`. Returns the text and whether it was clipped. */
export function textAt(
  track: LaneTrack,
  t: number,
): { text: string; clipped: boolean } {
  if (track.offsets.length === 0) return { text: "", clipped: false };

  // Binary search for the last event at or before `t`.
  let low = 0;
  let high = track.times.length - 1;
  let found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if ((track.times[mid] ?? 0) <= t) {
      found = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  if (found < 0) return { text: "", clipped: false };

  const end = track.offsets[found] ?? 0;
  const clipped = end > MAX_RENDER_CHARS;
  const start = clipped ? end - MAX_RENDER_CHARS : 0;
  return { text: track.joined.slice(start, end), clipped };
}

// ---------------------------------------------------------------------------
// Causal walk
// ---------------------------------------------------------------------------

export interface CausalChain {
  /** From the root cause forward to the entry that was asked about. */
  entries: TimelineEntry[];
  /** True when the walk stopped at `MAX_CHAIN_DEPTH` rather than at a root. */
  truncated: boolean;
  /** Ids that are referenced but absent from the timeline. */
  dangling: string[];
}

/**
 * Walk `caused_by` backwards from an entry to its root cause.
 *
 * Mirrors `MAX_CHAIN_DEPTH = 64` in `reel/timeline.py`. The cap is not
 * decorative: a cycle in `caused_by` (which a buggy recorder can produce) would
 * otherwise hang the browser tab, and a viewer that can be hung by its own data
 * is a viewer that gets closed and not reopened.
 *
 * `dangling` is reported rather than hidden. A `caused_by` pointing at an entry
 * that is not in the timeline means the export was truncated or the timeline was
 * filtered — and a chain that silently starts mid-way is worse than one that says
 * where it lost the thread.
 */
export const MAX_CHAIN_DEPTH = 64;

export function causalChain(
  timeline: readonly TimelineEntry[],
  startId: string,
): CausalChain {
  const byId = new Map(timeline.map((entry) => [entry.id, entry]));
  const reversed: TimelineEntry[] = [];
  const seen = new Set<string>();
  const dangling: string[] = [];
  let truncated = false;
  let current = byId.get(startId);

  while (current) {
    if (seen.has(current.id)) {
      // A cycle. Stop, and let the caller see the chain as it was walked.
      truncated = true;
      break;
    }
    seen.add(current.id);
    reversed.push(current);

    if (reversed.length >= MAX_CHAIN_DEPTH) {
      truncated = true;
      break;
    }
    if (!current.caused_by) break;

    const parent = byId.get(current.caused_by);
    if (!parent) {
      dangling.push(current.caused_by);
      break;
    }
    current = parent;
  }

  return { entries: reversed.reverse(), truncated, dangling };
}

/** Entries directly caused by `id`, in time order. */
export function effectsOf(
  timeline: readonly TimelineEntry[],
  id: string,
): TimelineEntry[] {
  return timeline.filter((entry) => entry.caused_by === id).sort((a, b) => a.t - b.t);
}

/** Index entries by id. Built once and memoised by the caller. */
export function indexTimeline(
  timeline: readonly TimelineEntry[],
): Map<string, TimelineEntry> {
  return new Map(timeline.map((entry) => [entry.id, entry]));
}

/** Timeline entries grouped by lane, each group in time order. */
export function groupByLane(
  timeline: readonly TimelineEntry[],
): Map<string, TimelineEntry[]> {
  const grouped = new Map<string, TimelineEntry[]>();
  for (const entry of timeline) {
    const bucket = grouped.get(entry.lane_id);
    if (bucket) bucket.push(entry);
    else grouped.set(entry.lane_id, [entry]);
  }
  for (const bucket of grouped.values()) bucket.sort((a, b) => a.t - b.t);
  return grouped;
}

/**
 * The reel's own clock, which is not wall-clock time.
 *
 * Every timeline entry and every lane event is measured in seconds from the
 * session start, so the two can be laid on the same axis without timezone
 * arithmetic. Deriving the duration from the data rather than trusting
 * `session.duration_s` matters for a reel whose session never closed.
 */
export function reelDuration(bundle: ReelBundle): number {
  let max = bundle.session.duration_s || 0;
  for (const entry of bundle.timeline) {
    if (entry.t > max) max = entry.t;
  }
  for (const lane of bundle.lanes) {
    const last = lane.events[lane.events.length - 1];
    if (last && last[0] > max) max = last[0];
  }
  return max;
}

/** Types present in the timeline, with counts. Drives the filter chips. */
export function typeCounts(timeline: readonly TimelineEntry[]): Array<[string, number]> {
  const counts = new Map<string, number>();
  for (const entry of timeline) {
    counts.set(entry.type, (counts.get(entry.type) ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/** Lanes that appear in the timeline but have no cast, and why. */
export function coverageGaps(bundle: ReelBundle): Array<{ laneId: string; reason: string }> {
  return bundle.coverage
    .filter((entry) => !entry.has_cast)
    .map((entry) => ({
      laneId: entry.lane_id,
      reason: entry.reason_missing || "no reason recorded",
    }));
}

export { CAST_INPUT, CAST_OUTPUT };
