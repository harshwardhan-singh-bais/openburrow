import { describe, expect, test } from "vitest";

import {
  eventTone,
  formatBytes,
  formatDuration,
  formatRelative,
  formatTimecode,
  isGovernanceEvent,
  LANE_LABELS,
  LANE_TONES,
  needsAttention,
  SESSION_TONES,
  shortId,
  STEP_TONES,
  TASK_TONES,
  TONE_CLASSES,
  TONE_DOT,
  type StateTone,
} from "@/lib/format";
import type { LaneStatus } from "@/types/openburrow";

/**
 * The presentation layer's own tests.
 *
 * These are the functions every screen reads a status through, so a wrong answer
 * here is wrong in every badge, tooltip and timeline at once. The values below
 * were measured against the implementation rather than reasoned about; where a
 * measurement disagreed with what the function claims to do, the expectation
 * records the *correct* answer and the function was fixed, which is the only
 * order in which this is worth doing.
 */

describe("formatDuration", () => {
  test("picks the coarsest unit that still says something", () => {
    expect(formatDuration(0)).toBe("0ms");
    expect(formatDuration(0.25)).toBe("250ms");
    expect(formatDuration(5.4)).toBe("5s");
    expect(formatDuration(90)).toBe("1m 30s");
    expect(formatDuration(3661)).toBe("1h 1m");
    expect(formatDuration(90000)).toBe("1d 1h");
  });

  test("never shows a unit boundary it has not reached", () => {
    expect(formatDuration(60)).toBe("1m 0s");
    expect(formatDuration(3600)).toBe("1h 0m");
    expect(formatDuration(86400)).toBe("1d 0h");
    expect(formatDuration(86399)).toBe("23h 59m");
  });

  test("an impossible input is a dash, not a zero", () => {
    // Zero would read as "it took no time", which is a different claim from
    // "we do not have a number".
    expect(formatDuration(-1)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("formatTimecode", () => {
  test("formats to the tenth", () => {
    expect(formatTimecode(0)).toBe("00:00:00.0");
    expect(formatTimecode(0.5)).toBe("00:00:00.5");
    expect(formatTimecode(60)).toBe("00:01:00.0");
    expect(formatTimecode(83.4)).toBe("00:01:23.4");
    expect(formatTimecode(3600)).toBe("01:00:00.0");
    expect(formatTimecode(3661.5)).toBe("01:01:01.5");
  });

  test("a tenth is a tenth, not the float that represents it", () => {
    // Regression. `Math.floor((seconds % 1) * 10)` reads 59.9 as
    // 59.8999999999999985789, so it rendered `00:00:59.8` — a number wrong in a
    // plausible direction, which is the failure AGENTS.md rule 6 names. Every
    // value here is one the old expression got wrong; 83.4 and 3599.9 happened
    // to survive it, which is why the bug looked like it was not there.
    expect(formatTimecode(59.9)).toBe("00:00:59.9");
    expect(formatTimecode(0.9)).toBe("00:00:00.9");
    expect(formatTimecode(1.1)).toBe("00:00:01.1");
    expect(formatTimecode(1.3)).toBe("00:00:01.3");
    expect(formatTimecode(7.7)).toBe("00:00:07.7");
    expect(formatTimecode(119.9)).toBe("00:01:59.9");
  });

  test("rounding a tenth overflows into the second it belongs to", () => {
    // The whole-second part and the tenth must come from the same rounded
    // quantity, or 59.96 renders as `00:00:59.0` — a timecode that has gone
    // backwards.
    expect(formatTimecode(59.96)).toBe("00:01:00.0");
    expect(formatTimecode(3599.99)).toBe("01:00:00.0");
  });

  test("withTenths=false drops the fraction without changing the second", () => {
    expect(formatTimecode(83.4, false)).toBe("00:01:23");
    expect(formatTimecode(3661.5, false)).toBe("01:01:01");
  });

  test("clamps rather than rendering a negative clock", () => {
    expect(formatTimecode(-5)).toBe("00:00:00.0");
    expect(formatTimecode(Number.NaN)).toBe("00:00:00.0");
  });
});

describe("formatBytes", () => {
  test("binary units with the labels people actually say", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1024)).toBe("1.0 kB");
    expect(formatBytes(1536)).toBe("1.5 kB");
    expect(formatBytes(1048576)).toBe("1.0 MB");
    expect(formatBytes(1073741824)).toBe("1.0 GB");
    expect(formatBytes(1099511627776)).toBe("1.0 TB");
  });

  test("one decimal below ten, none above", () => {
    // Two significant figures is the most a reader can use; `10.0 kB` would
    // claim precision the size does not have.
    expect(formatBytes(10240)).toBe("10 kB");
    expect(formatBytes(10485760)).toBe("10 MB");
  });

  test("an impossible input is a dash", () => {
    expect(formatBytes(-1)).toBe("—");
    expect(formatBytes(Number.NaN)).toBe("—");
  });
});

describe("shortId", () => {
  test("keeps the prefix that names the kind and the tail that disambiguates", () => {
    expect(shortId("sess_01HQ8Z4K2M9N7P1Q3R5T7V9X1B")).toBe("sess_…7V9X1B");
  });

  test("leaves an id that already fits alone", () => {
    expect(shortId("abc")).toBe("abc");
    expect(shortId("sess_short")).toBe("sess_short");
  });

  test("truncates an unprefixed id from the left", () => {
    expect(shortId("abcdefghij")).toBe("…efghij");
    expect(shortId("no_underscore_at_all_long")).toBe("no_…l_long");
  });

  test("a missing id is a dash", () => {
    expect(shortId("")).toBe("—");
    expect(shortId(null)).toBe("—");
    expect(shortId(undefined)).toBe("—");
  });
});

describe("formatRelative", () => {
  const now = Date.parse("2026-01-01T00:00:00Z");
  const ago = (seconds: number) => new Date(now - seconds * 1000).toISOString();

  test("steps through the units", () => {
    expect(formatRelative(ago(3), now)).toBe("just now");
    expect(formatRelative(ago(40), now)).toBe("40s ago");
    expect(formatRelative(ago(240), now)).toBe("4m ago");
    expect(formatRelative(ago(3 * 3600), now)).toBe("3h ago");
    expect(formatRelative(ago(3 * 86400), now)).toBe("3d ago");
  });

  test("a clock ahead of us is not a negative age", () => {
    // A daemon and a browser disagreeing by a few seconds is ordinary, and
    // "-30s ago" is a bug report from a user who did nothing wrong.
    expect(formatRelative(ago(-30), now)).toBe("just now");
  });

  test("an unparseable or absent timestamp is a dash", () => {
    expect(formatRelative(null, now)).toBe("—");
    expect(formatRelative(undefined, now)).toBe("—");
    expect(formatRelative("", now)).toBe("—");
    expect(formatRelative("not-a-date", now)).toBe("—");
  });
});

describe("eventTone", () => {
  test("classifies by namespace, so a new event type needs no table entry", () => {
    expect(eventTone("lane.started")).toBe("working");
    expect(eventTone("task.submitted")).toBe("working");
    expect(eventTone("session.created")).toBe("idle");
    expect(eventTone("bus.appended")).toBe("idle");
    expect(eventTone("radar.conflict")).toBe("blocked");
    expect(eventTone("brain.anchored")).toBe("done");
  });

  test("every governance namespace lands on the governance tone", () => {
    for (const namespace of ["governance", "delegation", "approval", "policy"]) {
      expect(eventTone(`${namespace}.something`)).toBe("governance");
      expect(isGovernanceEvent(`${namespace}.something`)).toBe(true);
    }
    expect(isGovernanceEvent("lane.started")).toBe(false);
  });

  test("an unknown or empty namespace is idle rather than a crash", () => {
    expect(eventTone("who.knows")).toBe("idle");
    expect(eventTone("")).toBe("idle");
  });
});

describe("the tone tables", () => {
  // These are hand-written mirrors of unions declared in `types/openburrow.ts`,
  // and nothing else compares them. A status added to one map and not another
  // renders as `undefined` in a className, which the browser drops silently — a
  // badge with no colour and no error. `LANE_LABELS` is deliberately not in the
  // tone sweep: its values are display strings, not tones.
  const toneTables = { LANE_TONES, SESSION_TONES, TASK_TONES, STEP_TONES };

  test("every tone has a class and a dot", () => {
    const tones = new Set<StateTone>();
    for (const table of Object.values(toneTables)) {
      for (const tone of Object.values(table)) tones.add(tone);
    }
    expect(tones.size).toBeGreaterThan(0);
    for (const tone of tones) {
      expect(TONE_CLASSES[tone], `TONE_CLASSES is missing ${tone}`).toBeTruthy();
      expect(TONE_DOT[tone], `TONE_DOT is missing ${tone}`).toBeTruthy();
    }
  });

  test("LANE_TONES and LANE_LABELS cover exactly the same statuses", () => {
    expect(Object.keys(LANE_TONES).sort()).toEqual(Object.keys(LANE_LABELS).sort());
  });
});

describe("needsAttention", () => {
  test("is the three states a human has to clear", () => {
    expect(needsAttention("waiting_input")).toBe(true);
    expect(needsAttention("waiting_auth")).toBe(true);
    expect(needsAttention("blocked")).toBe(true);
  });

  test("excludes the states that look similar and are not", () => {
    // `negotiating` is the lanes sorting it out themselves, and `crashed` is
    // over — flagging either as "needs a human" trains people to ignore the flag.
    const quiet: LaneStatus[] = ["starting", "idle", "working", "watching", "negotiating", "crashed", "stopped"];
    for (const status of quiet) {
      expect(needsAttention(status), `${status} should not need attention`).toBe(false);
    }
  });
});
