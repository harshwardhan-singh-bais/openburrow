"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api";

/**
 * Polling, done the way the daemon wants to be polled.
 *
 * The daemon does not push. `bus.tail` with a `since_seq` is the intended
 * pattern, and it is self-healing: the cursor lives on the client, so a dropped
 * tick is recovered by the next one rather than leaving a permanent hole.
 *
 * Four behaviours here are not optional, and each one exists because its absence
 * is a bug someone has already had:
 *
 * 1. **It stops when the tab is hidden.** A dashboard left open overnight in a
 *    background tab should not make a request a second for eight hours. This is
 *    the single biggest cost difference between a polling UI that is fine and one
 *    that is not.
 * 2. **It resumes immediately on visibility change**, without waiting out the
 *    interval. Coming back to a tab that shows ten-second-old state feels broken
 *    even though it is technically correct.
 * 3. **It backs off when the daemon is down.** A dead daemon plus a 1s poll is
 *    60 connection attempts a minute against a socket that is not there. Backoff
 *    is capped, and any success resets it, so recovery is immediate.
 * 4. **It never overlaps requests.** The next tick is scheduled *after* the
 *    previous one settles, not on a fixed clock. A slow daemon otherwise
 *    accumulates a queue of in-flight requests that all resolve out of order.
 */

export interface PollState<T> {
  data: T | null;
  error: ApiError | null;
  /** True only for the very first load, so the UI can show a skeleton once. */
  loading: boolean;
  /** True while a request is in flight after the first load. */
  refreshing: boolean;
  /** When the last successful response arrived. */
  lastUpdated: number | null;
  /** Manual refresh. Resets the backoff. */
  refresh: () => void;
}

export interface PollOptions {
  /** Milliseconds between polls. */
  intervalMs?: number;
  /** Skip polling entirely — used when a required id is missing. */
  enabled?: boolean;
  /** First backoff step after a failure, doubled up to `maxBackoffMs`. */
  minBackoffMs?: number;
  maxBackoffMs?: number;
}

export function usePoll<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  { intervalMs = 1000, enabled = true, minBackoffMs = 1000, maxBackoffMs = 30_000 }: PollOptions = {},
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  // The fetcher is almost always a fresh closure each render. Holding it in a
  // ref keeps the effect from restarting the whole poll loop every time the
  // parent re-renders, which would reset the backoff and the interval.
  //
  // Written from an effect rather than during render: a render can be
  // discarded — by a concurrent update, by StrictMode, by a suspended
  // sibling — and a ref write during render would survive it, leaving the
  // loop calling a fetcher from a render that never committed. Waiting
  // costs nothing, because the poll reads this only inside a `setTimeout`
  // callback and this effect is declared above the one that schedules the
  // first tick.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const backoffRef = useRef(minBackoffMs);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(() => {
    backoffRef.current = minBackoffMs;
    setNonce((value) => value + 1);
  }, [minBackoffMs]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  useEffect(() => {
    // No `setLoading(false)` here. Whether a disabled poll is loading is
    // derived at the return statement below, which covers the first render,
    // a toggle, and a toggle back with no state to keep in sync. Fixing
    // state up inside an effect is a second render that the render could
    // have computed.
    if (!enabled) return;

    let cancelled = false;

    const schedule = (delayMs: number) => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(tick, delayMs);
    };

    const tick = async () => {
      if (cancelled || !mountedRef.current) return;

      // A hidden tab does not poll. It does not clear the timer either — the
      // visibility listener below fires the next tick when the tab returns.
      if (typeof document !== "undefined" && document.hidden) {
        schedule(intervalMs);
        return;
      }

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setRefreshing(true);
      try {
        const value = await fetcherRef.current(controller.signal);
        if (cancelled || !mountedRef.current) return;
        setData(value);
        setError(null);
        setLastUpdated(Date.now());
        backoffRef.current = minBackoffMs;
        schedule(intervalMs);
      } catch (cause) {
        if (cancelled || !mountedRef.current) return;

        // An abort is this hook's own doing, not a failure to report.
        if (cause instanceof DOMException && cause.name === "AbortError") return;

        const apiError =
          cause instanceof ApiError
            ? cause
            : new ApiError(
                {
                  code: "openburrow.web.poll_failed",
                  message: cause instanceof Error ? cause.message : "the request failed",
                },
                0,
              );

        setError(apiError);

        // A non-retryable error (401, 403, 404) will say the same thing forever.
        // Retrying it on a timer is a client that hammers a door that will not
        // open, so it stops and waits for a manual refresh.
        if (!apiError.retryable) {
          setRefreshing(false);
          setLoading(false);
          return;
        }

        const delay = backoffRef.current;
        backoffRef.current = Math.min(backoffRef.current * 2, maxBackoffMs);
        schedule(delay);
      } finally {
        if (!cancelled && mountedRef.current) {
          setRefreshing(false);
          setLoading(false);
        }
      }
    };

    const onVisibility = () => {
      if (document.hidden) return;
      // Return to a visible tab: refresh now rather than after the remaining
      // interval. Waiting makes a correct UI feel stale.
      backoffRef.current = minBackoffMs;
      schedule(0);
    };

    document.addEventListener("visibilitychange", onVisibility);
    schedule(0);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
      if (timerRef.current) clearTimeout(timerRef.current);
      abortRef.current?.abort();
    };
  }, [enabled, intervalMs, minBackoffMs, maxBackoffMs, nonce]);

  // `enabled &&` rather than a reset inside the effect: a poll that is
  // switched on has no data yet, so it *is* loading.
  return { data, error, loading: enabled && loading, refreshing, lastUpdated, refresh };
}

/**
 * The bus cursor.
 *
 * Kept separate from `usePoll` because the bus is *incremental*: each response
 * is appended to what came before rather than replacing it, and the cursor is
 * the highest `seq` seen. Getting this wrong in either direction is bad — a
 * cursor that does not advance duplicates events forever, and one that advances
 * too far skips them silently.
 *
 * The accumulated feed lives in a ref rather than in React state, because the
 * fetcher needs to read the previous value and a state update would make it a
 * new closure on every poll — restarting the loop it is called from. `usePoll`
 * still owns the render copy; this ref is only what the next merge starts from.
 */
export function useBusFeed<T extends { seq: number }>(
  fetchPage: (sinceSeq: number, signal: AbortSignal) => Promise<T[]>,
  {
    intervalMs = 1000,
    limit = 500,
    enabled = true,
  }: { intervalMs?: number; limit?: number; enabled?: boolean } = {},
): Omit<PollState<T[]>, "data"> & { data: T[]; cursor: number } {
  // The `data` type is restated as non-nullable because that is what the
  // implementation returns — the `?? []` below has always coalesced it. Saying
  // `PollState<T[]>` instead would promise consumers a `null` that never
  // arrives, and every call site would then pay for a check that cannot fire.
  // A type that is looser than the code is not safety; it is noise that trains
  // people to ignore the compiler.
  const feedRef = useRef<T[]>([]);
  const cursorRef = useRef(0);

  const fetcher = useCallback(
    async (signal: AbortSignal) => {
      const page = await fetchPage(cursorRef.current, signal);
      if (page.length === 0) return feedRef.current;

      // Advance by the maximum seq, not by the last element. The daemon returns
      // events ordered by seq, but a relay-delivered batch can interleave origins,
      // and taking `page[page.length - 1].seq` would then rewind the cursor.
      const highest = page.reduce(
        (max, event) => (event.seq > max ? event.seq : max),
        cursorRef.current,
      );
      cursorRef.current = highest;

      // Dedupe by seq on append. A relay reconnect can legitimately replay a
      // window that overlaps what we already hold, and the alternative — trusting
      // the cursor — shows the same event twice.
      const seen = new Set(feedRef.current.map((event) => event.seq));
      const fresh = page.filter((event) => !seen.has(event.seq));
      if (fresh.length === 0) return feedRef.current;

      const merged = [...feedRef.current, ...fresh].sort((a, b) => a.seq - b.seq);
      // The feed is a view, not the record. Bounding it is what stops an
      // overnight tab from becoming a memory leak.
      const bounded = merged.length > limit ? merged.slice(merged.length - limit) : merged;
      feedRef.current = bounded;
      return bounded;
    },
    [fetchPage, limit],
  );

  const state = usePoll(fetcher, { intervalMs, enabled });

  const data = state.data ?? [];
  // Derived from the feed, not read out of `cursorRef` during render. A ref
  // read in render is invisible to React's memoization, and a render that is
  // discarded would leave the cursor and the feed on screen describing
  // different moments. The cursor *is* the highest `seq` the fetcher has
  // seen, and every one of those events is in the feed — the bounded slice
  // keeps the newest — so deriving it cannot disagree with what is shown.
  const cursor = data.reduce((max, event) => (event.seq > max ? event.seq : max), 0);

  return { ...state, data, cursor };
}
