"use client";

import { useSyncExternalStore } from "react";

/**
 * The current time, as a value React can render.
 *
 * A component that says "session age: 3h 12m" has to read a clock, and
 * `Date.now()` during render is not allowed. That is not a style rule: React may
 * render a component more than once for a single commit, or discard a render
 * entirely, and a clock read makes each attempt produce different output — a
 * component whose displayed value can disagree with itself for reasons nobody can
 * reproduce.
 *
 * So the clock becomes an external store, which is what it is. One interval for
 * the module rather than one per component: ten components asking for the time
 * should not start ten timers, and the timer stops when the last subscriber
 * unmounts, so a page that stops showing a clock stops ticking.
 *
 * **The server snapshot is `0`, and consumers must treat it as "not yet known".**
 * It has to be a value the server and the first client render agree on, and any
 * real timestamp would be a hydration mismatch — the server's clock and the
 * browser's differ by however much the response took to arrive. `formatDuration`
 * renders a negative as `—`, so the obvious arithmetic degrades to a dash for one
 * frame instead of showing a number that is wrong in a plausible direction.
 */

/** `formatDuration` shows whole seconds below a minute, so a coarser tick would
 *  make a just-created session's age visibly wrong. */
const TICK_MS = 1000;

const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let current = 0;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    // Set on subscribe rather than left at 0: React checks the snapshot once
    // after subscribing, so the first real value arrives immediately instead of
    // a tick later.
    current = Date.now();
    timer = setInterval(() => {
      current = Date.now();
      for (const notify of listeners) notify();
    }, TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const getSnapshot = (): number => current;
const getServerSnapshot = (): number => 0;

export function useNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
