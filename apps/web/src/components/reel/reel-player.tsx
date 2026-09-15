"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { StatusDot } from "@/components/status-badge";
import { TranscriptView } from "@/components/reel/transcript-view";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { eventTone, formatAbsolute, formatTimecode } from "@/lib/format";
import {
  buildLaneTrack,
  causalChain,
  coverageGaps,
  effectsOf,
  groupByLane,
  indexTimeline,
  reelDuration,
  textAt,
  typeCounts,
  type LaneTrack,
} from "@/lib/reel";
import { cn } from "@/lib/utils";
import type { ReelBundle, TimelineEntry } from "@/types/openburrow";

/**
 * The replay viewer.
 *
 * The reel exists to answer one question that a transcript cannot: **why did
 * this happen**. A wall of terminal output tells you what each lane printed; it
 * does not tell you that lane C stopped because lane A refused a proposal. That
 * link lives in `caused_by`, and this viewer's job is to make it walkable.
 *
 * Four decisions shape it.
 *
 * **The playhead advances on `requestAnimationFrame` but commits to state at
 * 100 ms.** A reel is measured in tenths — `formatTimecode` shows one decimal —
 * so committing every frame would re-render the timeline, the transcript and
 * every lane row sixty times a second to display the same tenth. The animation
 * stays smooth because the scrubber position is driven by the same state; it just
 * moves in steps the display can actually show.
 *
 * **Lane tracks are built once.** `buildLaneTrack` concatenates a lane's whole
 * output so that scrubbing is a binary search plus a slice instead of
 * re-concatenating five thousand strings per frame. Rebuilding them on every
 * render would throw that away.
 *
 * **Only the focused lane's text is sliced.** Every lane computes *how far* it
 * has got (a binary search on a precomputed array, which is free), but only one
 * lane's characters are extracted and parsed into spans. A ten-lane session
 * slicing ten transcripts per frame is the difference between a scrubber and a
 * slideshow.
 *
 * **Coverage gaps are stated, not hidden.** A lane with no cast renders an
 * explanation instead of an empty panel. A missing recording and a lane that
 * printed nothing look identical otherwise, and only one of them is a problem.
 */

export interface ReelPlayerProps {
  bundle: ReelBundle;
  /** The exporter's own self-contained viewer, when the bundle has one. */
  staticViewerUrl?: string | null;
}

const COMMIT_INTERVAL_S = 0.1;
const SPEEDS = [0.5, 1, 2, 4, 8] as const;

export function ReelPlayer({ bundle, staticViewerUrl }: ReelPlayerProps) {
  const duration = useMemo(() => reelDuration(bundle), [bundle]);
  const tracks = useMemo(() => bundle.lanes.map((lane) => buildLaneTrack(lane)), [bundle.lanes]);
  const timelineIndex = useMemo(() => indexTimeline(bundle.timeline), [bundle.timeline]);
  const byLane = useMemo(() => groupByLane(bundle.timeline), [bundle.timeline]);
  const counts = useMemo(() => typeCounts(bundle.timeline), [bundle.timeline]);
  const gaps = useMemo(() => coverageGaps(bundle), [bundle]);

  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<number>(1);
  const [focusedLane, setFocusedLane] = useState<string>(bundle.lanes[0]?.id ?? "");
  const [selectedEntry, setSelectedEntry] = useState<string | null>(null);
  const [activeTypes, setActiveTypes] = useState<Set<string>>(() => new Set());

  // The animation loop reads the playhead from a ref so that advancing time does
  // not restart the effect (and therefore the rAF chain) on every commit.
  const tRef = useRef(0);
  const lastCommitRef = useRef(0);
  const frameRef = useRef<number | null>(null);
  const lastFrameRef = useRef<number | null>(null);

  useEffect(() => {
    if (!playing) {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
      lastFrameRef.current = null;
      return;
    }

    const step = (now: number) => {
      const previous = lastFrameRef.current;
      lastFrameRef.current = now;

      if (previous !== null) {
        // Clamp the delta. A background tab that resumes after ten minutes
        // reports one enormous delta, which would jump the playhead to the end
        // and look like a bug rather than a pause.
        const deltaS = Math.min((now - previous) / 1000, 0.25) * speed;
        tRef.current = Math.min(tRef.current + deltaS, duration);
      }

      if (tRef.current - lastCommitRef.current >= COMMIT_INTERVAL_S || tRef.current >= duration) {
        lastCommitRef.current = tRef.current;
        setT(tRef.current);
      }

      if (tRef.current >= duration) {
        setPlaying(false);
        return;
      }
      frameRef.current = requestAnimationFrame(step);
    };

    frameRef.current = requestAnimationFrame(step);
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [playing, speed, duration]);

  const seek = useCallback(
    (next: number) => {
      const clamped = Math.max(0, Math.min(next, duration));
      tRef.current = clamped;
      lastCommitRef.current = clamped;
      setT(clamped);
    },
    [duration],
  );

  // Keyboard transport. Space toggles, arrows step a second, Home/End jump.
  // Worth the code: this is a viewer people use to *find* a moment, and hunting
  // for a 4px scrubber handle is how they stop using it.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      // Never steal keys from a field. Someone typing in the filter box pressing
      // space must get a space.
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;

      switch (event.key) {
        case " ":
          event.preventDefault();
          setPlaying((value) => !value);
          break;
        case "ArrowLeft":
          event.preventDefault();
          setPlaying(false);
          seek(t - (event.shiftKey ? 10 : 1));
          break;
        case "ArrowRight":
          event.preventDefault();
          setPlaying(false);
          seek(t + (event.shiftKey ? 10 : 1));
          break;
        case "Home":
          event.preventDefault();
          seek(0);
          break;
        case "End":
          event.preventDefault();
          seek(duration);
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [t, duration, seek]);

  const visibleEntries = useMemo(() => {
    const upTo = bundle.timeline.filter((entry) => entry.t <= t);
    if (activeTypes.size === 0) return upTo;
    return upTo.filter((entry) => activeTypes.has(entry.type));
  }, [bundle.timeline, t, activeTypes]);

  // The most recent entries, newest first. This is the "what just happened" list
  // next to the transcript, and it is capped because a reel with ten thousand
  // entries would otherwise render all of them.
  const recent = useMemo(() => [...visibleEntries].reverse().slice(0, 60), [visibleEntries]);

  const focused = tracks.find((track) => track.lane.id === focusedLane) ?? tracks[0];
  const focusedText = useMemo(
    () => (focused ? textAt(focused, t) : { text: "", clipped: false }),
    [focused, t],
  );

  const chain = useMemo(
    () => (selectedEntry ? causalChain(bundle.timeline, selectedEntry) : null),
    [bundle.timeline, selectedEntry],
  );
  const downstream = useMemo(
    () => (selectedEntry ? effectsOf(bundle.timeline, selectedEntry) : []),
    [bundle.timeline, selectedEntry],
  );

  const toggleType = (type: string) => {
    setActiveTypes((previous) => {
      const next = new Set(previous);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  if (bundle.lanes.length === 0 && bundle.timeline.length === 0) {
    return (
      <Card>
        <CardContent className="pt-5">
          <p className="text-sm text-muted-foreground">
            This reel is empty — no lanes and no timeline entries were recorded. An empty reel
            usually means the session was exported before anything happened.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-3 pt-5">
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm" onClick={() => setPlaying((value) => !value)} className="w-20">
              {playing ? "Pause" : "Play"}
            </Button>

            <span className="font-mono text-sm tabular-nums">
              {formatTimecode(t)} <span className="text-muted-foreground">/ {formatTimecode(duration)}</span>
            </span>

            <div className="flex items-center gap-1" role="radiogroup" aria-label="Playback speed">
              {SPEEDS.map((value) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={speed === value}
                  onClick={() => setSpeed(value)}
                  className={cn(
                    "rounded px-1.5 py-0.5 font-mono text-[11px]",
                    speed === value
                      ? "bg-secondary font-medium text-secondary-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {value}×
                </button>
              ))}
            </div>

            <div className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
              {staticViewerUrl ? (
                <a
                  href={staticViewerUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="hover:text-foreground"
                >
                  Open the exported viewer ↗
                </a>
              ) : null}
              <span>
                {bundle.lanes.length} lanes · {bundle.timeline.length} entries
              </span>
            </div>
          </div>

          <input
            type="range"
            min={0}
            max={Math.max(duration, 0.1)}
            step={0.1}
            value={t}
            onChange={(event) => {
              setPlaying(false);
              seek(Number(event.target.value));
            }}
            aria-label="Playhead"
            className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-border accent-state-working"
          />

          {/* Lane activity strip. Each lane is a column whose fill shows how far
              through its own output the playhead is — which makes a lane that
              went quiet at t=40 visible at a glance, without reading anything. */}
          {tracks.length > 0 ? (
            <div className="grid gap-1" style={{ gridTemplateColumns: `repeat(${Math.min(tracks.length, 8)}, minmax(0, 1fr))` }}>
              {tracks.map((track) => {
                const progress = progressAt(track, t);
                const active = focused === track;
                return (
                  <button
                    key={track.lane.id}
                    type="button"
                    onClick={() => setFocusedLane(track.lane.id)}
                    title={`${track.lane.title} — ${Math.round(progress * 100)}% of its output seen`}
                    className={cn(
                      "h-8 overflow-hidden rounded border px-1.5 text-left",
                      active ? "border-ring" : "border-border hover:border-ring/60",
                    )}
                  >
                    <span className="block truncate text-[10px] text-muted-foreground">
                      {track.lane.title}
                    </span>
                    <span className="mt-0.5 block h-1 w-full rounded-full bg-border">
                      <span
                        className="block h-1 rounded-full bg-state-working"
                        style={{ width: `${Math.round(progress * 100)}%` }}
                      />
                    </span>
                  </button>
                );
              })}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
        <section className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold">
              {focused ? focused.lane.title : "Transcript"}
            </h2>
            {focused ? (
              <span className="text-xs text-muted-foreground">
                {focused.lane.has_cast
                  ? `${focused.lane.output_events} output events · ${focused.lane.input_events} inputs`
                  : "no recording"}
              </span>
            ) : null}
          </div>

          {focused && !focused.lane.has_cast ? (
            <div className="rounded-lg border border-state-blocked/40 bg-state-blocked/10 px-3 py-2 text-xs text-state-blocked">
              No cast was recorded for this lane.
              {focused.lane.reason_missing ? ` ${focused.lane.reason_missing}.` : ""} The timeline
              below still covers it, so its decisions are visible even though its terminal output is
              not.
            </div>
          ) : focused ? (
            <div className="h-[22rem] lg:h-[32rem]">
              <TranscriptView
                track={focused}
                text={focusedText.text}
                clipped={focusedText.clipped}
                className="h-full"
              />
            </div>
          ) : null}
        </section>

        <aside className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Timeline</h2>
            <span className="text-xs text-muted-foreground tabular-nums">
              {visibleEntries.length}/{bundle.timeline.length}
            </span>
          </div>

          {/* Type filter chips, most frequent first. Clicking one narrows to it;
              clicking several shows the union, which is how someone answers
              "show me the refusals and the stops". */}
          {counts.length > 1 ? (
            <div className="flex flex-wrap gap-1">
              {counts.slice(0, 8).map(([type, count]) => (
                <button
                  key={type}
                  type="button"
                  onClick={() => toggleType(type)}
                  aria-pressed={activeTypes.has(type)}
                  className={cn(
                    "rounded border px-1.5 py-0.5 font-mono text-[10px]",
                    activeTypes.has(type)
                      ? "border-ring bg-secondary text-foreground"
                      : "border-border text-muted-foreground hover:text-foreground",
                  )}
                >
                  {type} <span className="tabular-nums">{count}</span>
                </button>
              ))}
              {activeTypes.size > 0 ? (
                <button
                  type="button"
                  onClick={() => setActiveTypes(new Set())}
                  className="rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-foreground"
                >
                  clear
                </button>
              ) : null}
            </div>
          ) : null}

          <div className="scrollbar-slim h-[18rem] overflow-y-auto rounded-lg border border-border bg-card">
            {recent.length === 0 ? (
              <p className="p-3 text-xs text-muted-foreground">
                Nothing has happened yet at {formatTimecode(t)}.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {recent.map((entry) => (
                  <TimelineRow
                    key={entry.id}
                    entry={entry}
                    selected={selectedEntry === entry.id}
                    onSelect={() => {
                      setSelectedEntry(entry.id);
                      seek(entry.t);
                    }}
                  />
                ))}
              </ul>
            )}
          </div>

          {selectedEntry && chain ? (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-xs font-medium text-muted-foreground">
                  Why this happened
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 pt-0">
                {chain.dangling.length > 0 ? (
                  <p className="rounded border border-state-blocked/30 bg-state-blocked/10 px-2 py-1 text-[11px] text-state-blocked">
                    The chain is broken — {chain.dangling.length}{" "}
                    {chain.dangling.length === 1 ? "entry" : "entries"} referenced but not present.
                    The export was likely truncated.
                  </p>
                ) : null}

                <ol className="space-y-1.5">
                  {chain.entries.map((entry, index) => (
                    <li key={entry.id} className="flex gap-2 text-[11px]">
                      <span className="w-4 shrink-0 text-right text-muted-foreground tabular-nums">
                        {index + 1}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5">
                          <StatusDot tone={eventTone(entry.type)} title={entry.type} />
                          <span className="font-mono text-muted-foreground">{entry.type}</span>
                          <span className="ml-auto font-mono text-muted-foreground tabular-nums">
                            {formatTimecode(entry.t, false)}
                          </span>
                        </span>
                        <span className="mt-0.5 block break-words">{entry.summary}</span>
                      </span>
                    </li>
                  ))}
                </ol>

                {chain.truncated ? (
                  <p className="text-[11px] text-muted-foreground">
                    Stopped at the depth cap of 64. The chain continues further back, or contains a
                    cycle.
                  </p>
                ) : null}

                {downstream.length > 0 ? (
                  <div className="border-t border-border pt-2">
                    <p className="text-[11px] font-medium text-muted-foreground">
                      Led to {downstream.length} {downstream.length === 1 ? "effect" : "effects"}
                    </p>
                    <ul className="mt-1 space-y-1">
                      {downstream.slice(0, 5).map((entry) => (
                        <li key={entry.id} className="flex gap-2 text-[11px]">
                          <span className="font-mono text-muted-foreground tabular-nums">
                            {formatTimecode(entry.t, false)}
                          </span>
                          <span className="min-w-0 flex-1 break-words">{entry.summary}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </CardContent>
            </Card>
          ) : null}

          {gaps.length > 0 ? (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-xs font-medium text-muted-foreground">
                  Recording gaps
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-1.5 pt-0">
                {gaps.map((gap) => (
                  <div key={gap.laneId} className="text-[11px]">
                    <span className="font-mono">{gap.laneId}</span>
                    <p className="text-muted-foreground">{gap.reason}</p>
                  </div>
                ))}
              </CardContent>
            </Card>
          ) : null}
        </aside>
      </div>

      <p className="text-xs text-muted-foreground">
        Started {formatAbsolute(bundle.session.started_at)} · space to play, ←/→ to step, shift for
        10s, Home/End to jump.
      </p>
    </div>
  );
}

/**
 * How far through its own output a lane has got at time `t`.
 *
 * A binary search over the precomputed `times` array rather than a filter, because
 * this runs for every lane on every commit and a `filter` would walk the whole
 * event list each time.
 */
function progressAt(track: LaneTrack, t: number): number {
  const total = track.offsets.length;
  if (total === 0) return 0;
  let low = 0;
  let high = total - 1;
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
  return found < 0 ? 0 : (found + 1) / total;
}

function TimelineRow({
  entry,
  selected,
  onSelect,
}: {
  entry: TimelineEntry;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className={cn(
          "flex w-full items-start gap-2 px-2.5 py-2 text-left transition-colors hover:bg-secondary/50",
          selected && "bg-secondary/70",
        )}
      >
        <span className="mt-1">
          <StatusDot tone={eventTone(entry.type)} title={entry.type} />
        </span>
        <span className="w-12 shrink-0 font-mono text-[10px] text-muted-foreground tabular-nums">
          {formatTimecode(entry.t, false)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block font-mono text-[10px] text-muted-foreground">{entry.type}</span>
          <span className="mt-0.5 block text-xs break-words">{entry.summary}</span>
        </span>
      </button>
    </li>
  );
}
