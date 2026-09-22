import { describe, expect, test } from "vitest";

import {
  buildLaneTrack,
  causalChain,
  coverageGaps,
  effectsOf,
  groupByLane,
  indexTimeline,
  MAX_CHAIN_DEPTH,
  MAX_RENDER_CHARS,
  parseBundle,
  parseCast,
  parseManifest,
  parseTimelineEntry,
  reelDuration,
  sgrSpans,
  stripAnsi,
  textAt,
  typeCounts,
} from "@/lib/reel";
import { EMPTY_BUNDLE, type ReelLane, type TimelineEntry } from "@/types/openburrow";

/**
 * Reel parsing, ANSI rendering and the causal walk.
 *
 * Every input here is untrusted in production — a bundle arrives from a file the
 * user dropped on the page or from a share link, and a cast arrives from a daemon
 * that may have been killed mid-write. So the tests are mostly about what happens
 * to malformed input: the design decision throughout is "render what parsed and
 * say what did not", never "throw and show nothing".
 */

const lane = (overrides: Partial<ReelLane> = {}): ReelLane => ({
  id: "lane-1",
  title: "lane one",
  has_cast: true,
  reason_missing: null,
  output_events: 0,
  input_events: 0,
  events: [],
  ...overrides,
});

const entry = (overrides: Partial<TimelineEntry> = {}): TimelineEntry => ({
  t: 0,
  seq: 0,
  type: "lane.started",
  lane_id: "lane-1",
  summary: "",
  caused_by: "",
  id: "e1",
  payload: {},
  ...overrides,
});

describe("parseCast", () => {
  test("reads the header and the events", () => {
    const cast = parseCast('{"version":2,"width":120,"height":40}\n[0.1,"o","hi"]\n[0.2,"i","go"]\n');
    expect(cast.header).toEqual({ version: 2, width: 120, height: 40 });
    expect(cast.events).toEqual([
      [0.1, "o", "hi"],
      [0.2, "i", "go"],
    ]);
    expect(cast.truncated).toBe(false);
  });

  test("a killed recording keeps every complete event and says so", () => {
    // This is the whole reason the parser is tolerant. A cast cut off mid-write
    // is the recording of the crash, which is exactly when someone wants it.
    const cast = parseCast('{"version":2}\n[0.1,"o","one"]\n[0.2,"o","two"]\n[0.3,"o","unfin');
    expect(cast.events).toEqual([
      [0.1, "o", "one"],
      [0.2, "o", "two"],
    ]);
    expect(cast.truncated).toBe(true);
  });

  test("an unreadable header costs the geometry, not the events", () => {
    const cast = parseCast('{"version":\n[0.1,"o","hi"]');
    expect(cast.header).toEqual({ version: 2, width: 80, height: 24 });
    expect(cast.events).toEqual([[0.1, "o", "hi"]]);
    expect(cast.truncated).toBe(true);
  });

  test("events are sorted by time, because the recorder is not obliged to be", () => {
    const cast = parseCast('{"version":2}\n[5.0,"o","c"]\n[1.0,"o","a"]\n[3.0,"o","b"]');
    expect(cast.events.map((event) => event[2])).toEqual(["a", "b", "c"]);
  });

  test("a line that is not a cast event is skipped, not fatal", () => {
    const cast = parseCast('{"version":2}\n"a bare string"\n[1,"o"]\n[2,"o","ok"]');
    expect(cast.events).toEqual([[2, "o", "ok"]]);
    expect(cast.truncated).toBe(true);
  });

  test("a non-string payload is stringified rather than dropped", () => {
    // The alternative is a blank line in the transcript with no explanation.
    const cast = parseCast('{"version":2}\n[1,"o",42]\n[2,"o",null]');
    expect(cast.events).toEqual([
      [1, "o", "42"],
      [2, "o", ""],
    ]);
  });

  test("blank lines are not events", () => {
    const cast = parseCast('{"version":2}\n\n[1,"o","a"]\n\n');
    expect(cast.events).toHaveLength(1);
    expect(cast.truncated).toBe(false);
  });

  test("an empty recording is empty, not truncated", () => {
    const cast = parseCast("");
    expect(cast.events).toEqual([]);
    expect(cast.truncated).toBe(false);
  });
});

describe("parseBundle", () => {
  test("anything that is not an object is the empty bundle", () => {
    for (const raw of [null, undefined, "text", 42, [], true]) {
      expect(parseBundle(raw)).toEqual(EMPTY_BUNDLE);
    }
  });

  test("reads the session, lanes, timeline and coverage", () => {
    const bundle = parseBundle({
      session: { id: "sess_1", name: "nightly", duration_s: 12.5, started_at: "2026-01-01" },
      lanes: [{ id: "l1", events: [[0, "a"], [1, "b"]] }],
      timeline: [{ id: "e1", t: 3, type: "lane.started", lane_id: "l1" }],
      coverage: [{ lane_id: "l1", has_cast: false, reason_missing: "no pty" }],
    });
    expect(bundle.session).toEqual({
      id: "sess_1",
      name: "nightly",
      duration_s: 12.5,
      started_at: "2026-01-01",
    });
    expect(bundle.lanes).toHaveLength(1);
    expect(bundle.lanes[0]?.events).toEqual([
      [0, "a"],
      [1, "b"],
    ]);
    expect(bundle.timeline[0]?.type).toBe("lane.started");
    expect(bundle.coverage[0]).toEqual({
      lane_id: "l1",
      has_cast: false,
      reason_missing: "no pty",
    });
  });

  test("a wrong-typed collection is empty, not a crash", () => {
    const bundle = parseBundle({ lanes: "nope", timeline: {}, coverage: 7 });
    expect(bundle.lanes).toEqual([]);
    expect(bundle.timeline).toEqual([]);
    expect(bundle.coverage).toEqual([]);
  });

  test("a lane event without a usable time is dropped", () => {
    // A negative timestamp would place the event before the session began, which
    // makes the scrubber's axis meaningless.
    const bundle = parseBundle({ lanes: [{ id: "l1", events: [[0, "keep"], [-1, "drop"], ["x", "drop"], [2]] }] });
    expect(bundle.lanes[0]?.events).toEqual([[0, "keep"]]);
  });

  test("has_cast is strict, so a truthy string is not a cast", () => {
    const bundle = parseBundle({ lanes: [{ id: "l1", has_cast: "yes" }] });
    expect(bundle.lanes[0]?.has_cast).toBe(false);
  });

  test("a lane with no title falls back to its id", () => {
    const bundle = parseBundle({ lanes: [{ id: "l1" }] });
    expect(bundle.lanes[0]?.title).toBe("l1");
  });
});

describe("parseManifest", () => {
  test("an empty object is no manifest at all", () => {
    expect(parseManifest({})).toBeNull();
    expect(parseManifest(null)).toBeNull();
    expect(parseManifest("x")).toBeNull();
  });

  test("reads the share-link fields that used to be dropped", () => {
    // These four were being discarded, and `signed` / `expires_at` are the ones
    // that matter: a viewer that cannot see a link is signed or expiring cannot
    // warn anyone, which is the entire point of scoping a share link.
    const manifest = parseManifest({
      session_id: "sess_1",
      signed: true,
      signature: "abc123",
      expires_at: "2026-02-01T00:00:00Z",
      is_expired: false,
      allowed_orgs: ["acme", "globex"],
      public: false,
      viewer_version: "0.1.0",
    });
    expect(manifest?.signed).toBe(true);
    expect(manifest?.signature).toBe("abc123");
    expect(manifest?.expires_at).toBe("2026-02-01T00:00:00Z");
    expect(manifest?.is_expired).toBe(false);
    expect(manifest?.allowed_orgs).toEqual(["acme", "globex"]);
    expect(manifest?.public).toBe(false);
    expect(manifest?.viewer_version).toBe("0.1.0");
  });

  test("is_expired is read, not recomputed against the browser clock", () => {
    // The viewer must agree with the server. Recomputing here would make a link
    // that the server still honours look dead to anyone whose clock is fast.
    const manifest = parseManifest({ expires_at: "2020-01-01T00:00:00Z", is_expired: false });
    expect(manifest?.is_expired).toBe(false);
  });

  test("total_cost_usd stays null when it is absent", () => {
    // Null and 0 are different claims: "we did not record a cost" and "it was
    // free" must not render the same.
    expect(parseManifest({ session_id: "s" })?.total_cost_usd).toBeNull();
    expect(parseManifest({ total_cost_usd: 0 })?.total_cost_usd).toBe(0);
  });
});

describe("parseTimelineEntry", () => {
  test("garbage becomes a fully defaulted entry", () => {
    expect(parseTimelineEntry("nonsense")).toEqual({
      t: 0,
      seq: 0,
      type: "",
      lane_id: "",
      summary: "",
      caused_by: "",
      id: "",
      payload: {},
    });
  });

  test("a non-finite time is zero rather than NaN", () => {
    // NaN in `t` would poison every comparison in the scrubber and the sort.
    expect(parseTimelineEntry({ t: Number.NaN }).t).toBe(0);
    expect(parseTimelineEntry({ t: Number.POSITIVE_INFINITY }).t).toBe(0);
  });

  test("a payload that is an array is not a record", () => {
    expect(parseTimelineEntry({ payload: [1, 2] }).payload).toEqual({});
  });
});

describe("stripAnsi", () => {
  test("removes SGR colour", () => {
    expect(stripAnsi("\u001b[31mred\u001b[0m and plain")).toBe("red and plain");
  });

  test("removes an OSC title, whose payload may contain anything", () => {
    expect(stripAnsi("\u001b]0;a title\u0007visible")).toBe("visible");
  });

  test("removes a hyperlink wrapper but keeps the text", () => {
    expect(stripAnsi("\u001b]8;;http://example.com\u0007link\u001b]8;;\u0007")).toBe("link");
  });

  test("leaves ordinary text alone", () => {
    expect(stripAnsi("nothing to strip")).toBe("nothing to strip");
  });
});

describe("sgrSpans", () => {
  test("a plain string is one unstyled span", () => {
    expect(sgrSpans("plain")).toEqual([{ text: "plain" }]);
  });

  test("colour becomes a colour, not a marker", () => {
    const spans = sgrSpans("\u001b[31mred");
    expect(spans).toHaveLength(1);
    expect(spans[0]?.text).toBe("red");
    expect(spans[0]?.color).toBe("#cc3e28");
  });

  test("reset ends the style and the text continues unstyled", () => {
    const spans = sgrSpans("\u001b[1;31mbold red\u001b[0mplain");
    expect(spans).toEqual([{ bold: true, color: "#cc3e28", text: "bold red" }, { text: "plain" }]);
  });

  test("the 256-colour cube and the greyscale ramp", () => {
    expect(sgrSpans("\u001b[38;5;196mX")[0]?.color).toBe("rgb(255,0,0)");
    // 232 is the first greyscale entry, at level 8.
    expect(sgrSpans("\u001b[38;5;232mX")[0]?.color).toBe("rgb(8,8,8)");
  });

  test("truecolor", () => {
    expect(sgrSpans("\u001b[38;2;10;20;30mX")[0]?.color).toBe("rgb(10,20,30)");
    expect(sgrSpans("\u001b[48;2;1;2;3mX")[0]?.background).toBe("rgb(1,2,3)");
  });

  test("a sequence it cannot render is visible, not silently swallowed", () => {
    // The design note says a renderer that silently drops what it does not
    // understand makes a corrupt transcript look clean. This is that promise: the
    // marker is a span with no text and the sequence it could not render.
    expect(sgrSpans("before\u001b[2Jafter")).toEqual([
      { text: "before" },
      { text: "", unsupported: "\u001b[2J" },
      { text: "after" },
    ]);
  });

  test("an OSC is removed by position, not by luck", () => {
    // Regression, and a bad one. The scan measures positions in the OSC-stripped
    // string, so slicing the *original* string shifted every span by the length of
    // the OSC that preceded it — `\x1b]0;evil [31m\x07visible` came out as the
    // escape junk `\x1b]0;evi`. A PTY sets a window title on essentially every
    // session, so this garbled real transcripts while `stripAnsi` (search,
    // tooltips) stayed correct, which is why it read as intermittent.
    expect(sgrSpans("\u001b]0;evil [31m\u0007visible")).toEqual([{ text: "visible" }]);
  });

  test("an OSC does not disturb the styling that follows it", () => {
    // The strongest form: an OSC and an SGR in one string, where the colour span
    // can only be correct if both the removal and the offsets are.
    expect(sgrSpans("\u001b]0;title\u0007\u001b[31mred")).toEqual([
      { color: "#cc3e28", text: "red" },
    ]);
  });

  test("a hyperlink wrapper is dropped and its text kept", () => {
    expect(sgrSpans("\u001b]8;;http://example.com\u0007link\u001b]8;;\u0007tail")).toEqual([
      { text: "linktail" },
    ]);
  });
});

describe("buildLaneTrack and textAt", () => {
  const track = buildLaneTrack(lane({ events: [[0, "ab"], [1, "c"], [2, "def"]] }));

  test("precomputes the joined text and the offsets", () => {
    expect(track.joined).toBe("abcdef");
    expect(track.offsets).toEqual([2, 3, 6]);
    expect(track.times).toEqual([0, 1, 2]);
    expect(track.totalChars).toBe(6);
  });

  test("returns everything emitted up to the requested time", () => {
    expect(textAt(track, 0).text).toBe("ab");
    expect(textAt(track, 1).text).toBe("abc");
    expect(textAt(track, 2).text).toBe("abcdef");
  });

  test("a time between events shows the last event, not a blank", () => {
    expect(textAt(track, 1.5).text).toBe("abc");
  });

  test("before the first event there is nothing to show", () => {
    expect(textAt(track, -1)).toEqual({ text: "", clipped: false });
  });

  test("past the end holds at the end", () => {
    expect(textAt(track, 999).text).toBe("abcdef");
  });

  test("an empty track is empty rather than an error", () => {
    expect(textAt(buildLaneTrack(lane()), 5)).toEqual({ text: "", clipped: false });
  });

  test("a long lane is clipped and says it was clipped", () => {
    // The cap is a rendering decision, and `clipped` is what lets the viewer
    // tell the reader that what they are looking at starts mid-transcript.
    const long = buildLaneTrack(
      lane({ events: [[0, "a".repeat(50_000)], [1, "b".repeat(20_000)]] }),
    );
    const result = textAt(long, 1);
    expect(result.clipped).toBe(true);
    expect(result.text).toHaveLength(MAX_RENDER_CHARS);
    expect(result.text).toBe("a".repeat(40_000) + "b".repeat(20_000));

    const short = textAt(long, 0);
    expect(short.clipped).toBe(false);
    expect(short.text).toHaveLength(50_000);
  });
});

describe("causalChain", () => {
  const timeline = [
    entry({ id: "a", t: 0, caused_by: "" }),
    entry({ id: "b", t: 1, caused_by: "a" }),
    entry({ id: "c", t: 2, caused_by: "b" }),
  ];

  test("walks backwards to the root and returns it forwards", () => {
    const chain = causalChain(timeline, "c");
    expect(chain.entries.map((item) => item.id)).toEqual(["a", "b", "c"]);
    expect(chain.truncated).toBe(false);
    expect(chain.dangling).toEqual([]);
  });

  test("an entry with no cause is a chain of one", () => {
    expect(causalChain(timeline, "a").entries.map((item) => item.id)).toEqual(["a"]);
  });

  test("an unknown start is an empty chain, not a throw", () => {
    expect(causalChain(timeline, "nope")).toEqual({ entries: [], truncated: false, dangling: [] });
  });

  test("a broken link is reported, not hidden", () => {
    // A chain that silently starts mid-way is worse than one that says where it
    // lost the thread: the reader cannot tell a root cause from a truncated export.
    const chain = causalChain([entry({ id: "c", caused_by: "ghost" })], "c");
    expect(chain.entries.map((item) => item.id)).toEqual(["c"]);
    expect(chain.dangling).toEqual(["ghost"]);
    expect(chain.truncated).toBe(false);
  });

  test("a cycle stops instead of hanging the tab", () => {
    const cyclic = [entry({ id: "a", caused_by: "b" }), entry({ id: "b", caused_by: "a" })];
    const chain = causalChain(cyclic, "a");
    expect(chain.truncated).toBe(true);
    expect(chain.entries).toHaveLength(2);
  });

  test("a self-caused entry terminates", () => {
    const chain = causalChain([entry({ id: "a", caused_by: "a" })], "a");
    expect(chain.entries.map((item) => item.id)).toEqual(["a"]);
    expect(chain.truncated).toBe(true);
  });

  test("the depth cap mirrors the Python side", () => {
    const deep = Array.from({ length: MAX_CHAIN_DEPTH + 10 }, (_, index) =>
      entry({ id: `e${index}`, t: index, caused_by: index === 0 ? "" : `e${index - 1}` }),
    );
    const chain = causalChain(deep, `e${MAX_CHAIN_DEPTH + 9}`);
    expect(chain.truncated).toBe(true);
    expect(chain.entries).toHaveLength(MAX_CHAIN_DEPTH);
  });
});

describe("timeline helpers", () => {
  const timeline = [
    entry({ id: "a", t: 2, lane_id: "l1", type: "lane.started" }),
    entry({ id: "b", t: 1, lane_id: "l2", type: "task.submitted" }),
    entry({ id: "c", t: 3, lane_id: "l1", type: "lane.started", caused_by: "b" }),
  ];

  test("effectsOf finds the children in time order", () => {
    expect(effectsOf(timeline, "b").map((item) => item.id)).toEqual(["c"]);
    expect(effectsOf(timeline, "nobody")).toEqual([]);
  });

  test("groupByLane buckets and sorts within each bucket", () => {
    const grouped = groupByLane(timeline);
    expect([...grouped.keys()].sort()).toEqual(["l1", "l2"]);
    expect(grouped.get("l1")?.map((item) => item.id)).toEqual(["a", "c"]);
  });

  test("indexTimeline keys by id", () => {
    expect(indexTimeline(timeline).get("b")?.t).toBe(1);
  });

  test("typeCounts is ordered by frequency, then by name", () => {
    // A stable order matters: a chip row that reshuffles between renders is a
    // row people mis-click.
    expect(typeCounts(timeline)).toEqual([
      ["lane.started", 2],
      ["task.submitted", 1],
    ]);
  });
});

describe("reelDuration", () => {
  test("is derived from the data, not from the session field", () => {
    // A session that never closed has a duration of 0 while its lanes clearly ran.
    const bundle = parseBundle({
      session: { duration_s: 0 },
      lanes: [{ id: "l1", events: [[42, "x"]] }],
      timeline: [{ id: "e", t: 10 }],
    });
    expect(reelDuration(bundle)).toBe(42);
  });

  test("falls back to the session duration when nothing is longer", () => {
    expect(reelDuration(parseBundle({ session: { duration_s: 100 } }))).toBe(100);
  });

  test("the empty bundle is zero", () => {
    expect(reelDuration(EMPTY_BUNDLE)).toBe(0);
  });
});

describe("coverageGaps", () => {
  test("names the lanes with no cast and why", () => {
    const bundle = parseBundle({
      coverage: [
        { lane_id: "l1", has_cast: true },
        { lane_id: "l2", has_cast: false, reason_missing: "the pty closed early" },
        { lane_id: "l3", has_cast: false },
      ],
    });
    expect(coverageGaps(bundle)).toEqual([
      { laneId: "l2", reason: "the pty closed early" },
      { laneId: "l3", reason: "no reason recorded" },
    ]);
  });
});
