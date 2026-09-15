"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { StatusDot } from "@/components/status-badge";
import { eventTone, formatAbsolute, formatRelative, isGovernanceEvent, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { BusEvent } from "@/types/openburrow";

/**
 * The bus feed.
 *
 * The bus log is the canonical record of a session, so this is not a log
 * viewer — it is the primary artefact. Three things follow from that.
 *
 * **Governance events are coloured differently from failures.** A refusal is a
 * deliberate act by a human or a policy; a crash is not. Rendering both red
 * would teach people that the governance layer is a source of errors, which is
 * the exact opposite of what it is. `--governance` is its own hue for this
 * reason.
 *
 * **Following the tail is a state, not a behaviour.** A feed that always jumps
 * to the newest event is unusable the moment you scroll up to read something —
 * the next poll yanks you back. So scrolling away from the bottom *unpins* the
 * feed, and a pill appears saying how many events arrived while you were
 * reading. Clicking it re-pins and jumps.
 *
 * **The payload is collapsed by default and shown raw.** Not summarised, not
 * pretty-printed into a fake hierarchy — the exact JSON the daemon stored.
 * Anything else is an interpretation, and an interpretation of the record is
 * what the record exists to avoid.
 */

export interface BusFeedProps {
  events: BusEvent[];
  /** True while a poll is in flight, for the subtle "live" indicator. */
  refreshing?: boolean;
  /** Rendered when the feed is empty and no error is present. */
  emptyHint?: string;
  className?: string;
}

export function BusFeed({ events, refreshing = false, emptyHint, className }: BusFeedProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const [seenCount, setSeenCount] = useState(events.length);

  // How many events arrived while the reader was scrolled up. Computed rather
  // than counted so that a bounded feed (which drops the oldest) cannot report a
  // negative or ever-growing number.
  const pending = pinned ? 0 : Math.max(0, events.length - seenCount);

  useEffect(() => {
    if (pinned && scrollerRef.current) {
      scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
      setSeenCount(events.length);
    }
  }, [events, pinned]);

  const onScroll = () => {
    const node = scrollerRef.current;
    if (!node) return;
    // 24px of slack: "at the bottom" in a browser is never exactly the bottom,
    // and a strict comparison unpins the feed on a sub-pixel rounding error.
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
    setPinned(atBottom);
    if (atBottom) setSeenCount(events.length);
  };

  const jumpToLatest = () => {
    setPinned(true);
    setSeenCount(events.length);
    const node = scrollerRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  };

  const toggle = (seq: number) => {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  };

  // Only the events whose payload is open get a `JSON.stringify`, so a feed of
  // five hundred events does not pay to serialise five hundred payloads on every
  // render.
  const prettyPayloads = useMemo(() => {
    const map = new Map<number, string>();
    for (const seq of expanded) {
      const event = events.find((candidate) => candidate.seq === seq);
      if (!event) continue;
      try {
        map.set(seq, JSON.stringify(event.payload, null, 2));
      } catch {
        map.set(seq, "/* payload is not serialisable */");
      }
    }
    return map;
  }, [expanded, events]);

  if (events.length === 0) {
    return (
      <div
        className={cn(
          "flex h-64 items-center justify-center rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground",
          className,
        )}
      >
        {emptyHint ?? "No events yet. Nothing has happened in this session."}
      </div>
    );
  }

  return (
    <div className={cn("relative", className)}>
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="scrollbar-slim h-64 overflow-y-auto rounded-lg border border-border bg-card lg:h-[28rem]"
      >
        <ul className="divide-y divide-border">
          {events.map((event) => {
            const tone = eventTone(event.event_type);
            const open = expanded.has(event.seq);
            const governance = isGovernanceEvent(event.event_type);

            return (
              <li key={`${event.origin_repo ?? "local"}:${event.seq}:${event.id}`}>
                <button
                  type="button"
                  onClick={() => toggle(event.seq)}
                  aria-expanded={open}
                  className={cn(
                    "flex w-full items-start gap-2.5 px-3 py-2 text-left transition-colors hover:bg-secondary/50",
                    governance && "bg-governance/5",
                  )}
                >
                  <span className="mt-1.5">
                    <StatusDot tone={tone} title={event.event_type} />
                  </span>

                  <span className="w-14 shrink-0 pt-0.5 font-mono text-[11px] tabular-nums text-muted-foreground">
                    {event.seq}
                  </span>

                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-baseline gap-x-2">
                      <span
                        className={cn(
                          "font-mono text-[11px]",
                          governance ? "text-governance" : "text-muted-foreground",
                        )}
                      >
                        {event.event_type}
                      </span>
                      {event.lane_id ? (
                        <span className="font-mono text-[11px] text-muted-foreground" title={event.lane_id}>
                          {shortId(event.lane_id, 5)}
                        </span>
                      ) : null}
                      {event.origin_repo ? (
                        // Which daemon this came from. Absent for local events,
                        // where there is only one origin — so its presence is
                        // itself information.
                        <span className="rounded border border-border px-1 font-mono text-[10px] text-muted-foreground">
                          {event.origin_repo}
                        </span>
                      ) : null}
                    </span>

                    <span className="mt-0.5 block text-sm break-words">{event.summary}</span>
                  </span>

                  <span
                    className="shrink-0 pt-0.5 text-[11px] whitespace-nowrap text-muted-foreground"
                    title={formatAbsolute(event.created_at)}
                  >
                    {formatRelative(event.created_at)}
                  </span>
                </button>

                {open ? (
                  <div className="border-t border-border bg-muted/40 px-3 py-2">
                    <dl className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                      <div className="flex gap-1">
                        <dt>id</dt>
                        <dd className="font-mono">{event.id}</dd>
                      </div>
                      <div className="flex gap-1">
                        <dt>at</dt>
                        <dd className="font-mono">{formatAbsolute(event.created_at)}</dd>
                      </div>
                      {event.task_id ? (
                        <div className="flex gap-1">
                          <dt>task</dt>
                          <dd className="font-mono">{shortId(event.task_id)}</dd>
                        </div>
                      ) : null}
                      {typeof event.origin_seq === "number" ? (
                        <div className="flex gap-1">
                          <dt>origin seq</dt>
                          <dd className="font-mono tabular-nums">{event.origin_seq}</dd>
                        </div>
                      ) : null}
                    </dl>
                    <pre className="scrollbar-slim transcript max-h-72 overflow-auto rounded border border-border bg-background p-2 text-[11px] leading-relaxed">
                      {prettyPayloads.get(event.seq) ?? "{}"}
                    </pre>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      </div>

      {pending > 0 ? (
        <button
          type="button"
          onClick={jumpToLatest}
          className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-border bg-primary px-3 py-1 text-xs font-medium text-primary-foreground shadow-lg"
        >
          {pending} new {pending === 1 ? "event" : "events"} ↓
        </button>
      ) : null}

      <div className="mt-1.5 flex items-center justify-between text-[11px] text-muted-foreground">
        <span>
          {events.length} {events.length === 1 ? "event" : "events"} in view
        </span>
        <span className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className={cn(
              "size-1.5 rounded-full",
              refreshing ? "animate-lane-pulse bg-state-working" : "bg-state-done",
            )}
          />
          {refreshing ? "polling" : "idle"}
        </span>
      </div>
    </div>
  );
}
