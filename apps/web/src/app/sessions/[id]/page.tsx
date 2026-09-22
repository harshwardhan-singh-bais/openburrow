"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback } from "react";

import { BusFeed } from "@/components/bus-feed";
import { LaneCard } from "@/components/lane-card";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { bus, sessions, tasks } from "@/lib/api";
import { SESSION_TONES, TASK_TONES, formatDuration, formatRelative, shortId } from "@/lib/format";
import { useNow } from "@/lib/use-now";
import { useBusFeed, usePoll } from "@/lib/use-poll";

/**
 * One session: its lanes, its bus, and the tasks waiting on a human.
 *
 * The layout answers three questions in the order they get asked. "What is
 * happening" is the lane grid. "Why" is the bus feed. "What is stuck" is the
 * blocking-task panel, which sits *above* the lanes because a task waiting on
 * input is the only thing on this page that cannot resolve itself — and burying
 * it below a scrolling bus feed is how a session stalls overnight.
 *
 * The bus feed is fed by `useBusFeed`, which carries a `since_seq` cursor. That
 * cursor is the reason this page can be left open: each poll asks only for what
 * is new, so the cost of a ten-hour-old tab is the same as the cost of a fresh
 * one.
 */

const POLL_MS = Number(process.env.NEXT_PUBLIC_OPENBURROW_POLL_MS ?? 1000);
const FEED_LIMIT = Number(process.env.NEXT_PUBLIC_OPENBURROW_FEED_LIMIT ?? 500);

export default function SessionPage() {
  const params = useParams<{ id: string }>();
  const sessionId = typeof params?.id === "string" ? params.id : "";

  // A clock read is not a render-time value, so it comes from the store that
  // owns one. See `useNow` for why `0` means "not yet known".
  const now = useNow();

  const sessionState = usePoll(
    (signal) => sessions.show(sessionId, signal),
    { intervalMs: Math.max(POLL_MS * 2, 2000), enabled: Boolean(sessionId) },
  );

  const taskState = usePoll((signal) => tasks.list(sessionId, true, signal), {
    intervalMs: Math.max(POLL_MS * 2, 2000),
    enabled: Boolean(sessionId),
  });

  // The cursor is stable across renders because the fetcher is memoised inside
  // the hook — a new fetcher identity would restart the poll loop and reset the
  // cursor, which duplicates the whole feed on every render.
  const fetchPage = useCallback(
    (sinceSeq: number, signal: AbortSignal) => bus.tail(sessionId, sinceSeq, 200, signal),
    [sessionId],
  );

  const feed = useBusFeed(fetchPage, {
    intervalMs: POLL_MS,
    limit: FEED_LIMIT,
    enabled: Boolean(sessionId),
  });

  if (!sessionId) {
    return <ErrorState error={{ code: "openburrow.web.missing_session", message: "no session id in the URL" } as never} />;
  }

  if (sessionState.loading && !sessionState.data) {
    return <LoadingState label={`Loading ${sessionId}…`} />;
  }

  if (sessionState.error && !sessionState.data) {
    return (
      <div className="space-y-4">
        <Link href="/" className="text-xs text-muted-foreground hover:text-foreground">
          ← Board
        </Link>
        <ErrorState
          error={sessionState.error}
          onRetry={sessionState.refresh}
          title="Could not load this session"
        />
      </div>
    );
  }

  const session = sessionState.data?.session;
  const liveLanes = sessionState.data?.running_lanes ?? [];
  const blocking = taskState.data ?? [];

  if (!session) {
    return (
      <div className="space-y-4">
        <Link href="/" className="text-xs text-muted-foreground hover:text-foreground">
          ← Board
        </Link>
        <EmptyState
          title="No such session"
          description={`The daemon has no session with id ${sessionId}. It may have been removed, or this may be a different repo.`}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-xs text-muted-foreground hover:text-foreground">
          ← Board
        </Link>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-lg font-semibold tracking-tight">{session.name}</h1>
              <StatusBadge tone={SESSION_TONES[session.status]} label={session.status} />
            </div>
            {session.description ? (
              <p className="mt-1 text-sm text-muted-foreground">{session.description}</p>
            ) : null}
            <p className="mt-1 flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
              <span className="font-mono" title={session.id}>
                {session.id}
              </span>
              <span className="font-mono">{session.branch || session.base_branch}</span>
              <span>owner {session.owner || "unset"}</span>
              <span>created {formatRelative(session.created_at)}</span>
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" asChild>
              <Link href={`/reels/${encodeURIComponent(session.id)}`}>Replay</Link>
            </Button>
            <Button variant="outline" size="sm" onClick={sessionState.refresh}>
              Refresh
            </Button>
          </div>
        </div>
      </div>

      {blocking.length > 0 ? (
        <Card className="border-state-blocked/40">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <span aria-hidden="true" className="size-2 rounded-full bg-state-blocked" />
              Waiting on a human
              <span className="font-normal text-muted-foreground tabular-nums">({blocking.length})</span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            {blocking.map((task) => (
              <div key={task.id} className="flex items-start gap-3 text-sm">
                <StatusBadge tone={TASK_TONES[task.state]} label={task.state.replace("_", " ")} />
                <div className="min-w-0 flex-1">
                  <p className="break-words">{task.description}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {task.lane_id ? (
                      <>
                        lane <span className="font-mono">{shortId(task.lane_id)}</span> ·{" "}
                      </>
                    ) : null}
                    {task.state === "input_required"
                      ? "needs input before it can continue"
                      : "needs an authorisation decision"}
                    {typeof task.delegation_depth === "number" && task.delegation_depth > 0 ? (
                      <> · delegation depth {task.delegation_depth}</>
                    ) : null}
                  </p>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}

      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Lanes</h2>
          <span className="text-xs text-muted-foreground tabular-nums">
            {liveLanes.length} running
          </span>
        </div>

        {liveLanes.length === 0 ? (
          <EmptyState
            title="No lanes running"
            description="A session with no lanes is a plan without anyone working on it. Start one from the CLI, or from the bus feed below if this session already finished."
            action={
              <div className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-left">
                <code className="block font-mono text-xs">
                  burrow lane start --session {shortId(session.id)} --name review --role reviewer
                </code>
              </div>
            }
          />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {liveLanes.map((lane) => (
              <LaneCard key={lane.id} lane={lane} sessionId={session.id} />
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">Bus</h2>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span>
              cursor <span className="font-mono tabular-nums">{feed.cursor}</span>
            </span>
            {feed.lastUpdated ? (
              <span>updated {formatRelative(new Date(feed.lastUpdated).toISOString())}</span>
            ) : null}
          </div>
        </div>

        {feed.error && feed.data.length === 0 ? (
          <ErrorState
            error={feed.error}
            onRetry={feed.refresh}
            title="Could not read the bus"
          />
        ) : (
          <BusFeed
            events={feed.data}
            refreshing={feed.refreshing}
            emptyHint="No events yet. The bus records everything a lane does, so an empty feed means nothing has started."
          />
        )}

        <p className="text-xs text-muted-foreground">
          The bus log is append-only and is the canonical record of this session. Everything on this
          page is derived from it.
        </p>
      </section>

      {session.closed_at ? (
        <p className="text-xs text-muted-foreground">
          This session closed {formatRelative(session.closed_at)}. Lanes shown above are the ones
          still running.
        </p>
      ) : null}

      {/* `now` is 0 until the first client tick, and a negative duration
          renders as an em dash rather than as a wrong number. */}
      <p className="text-xs text-muted-foreground">
        Session age: {formatDuration((now - Date.parse(session.created_at)) / 1000)}
      </p>
    </div>
  );
}
