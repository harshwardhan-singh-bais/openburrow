"use client";

import Link from "next/link";

import { DaemonOfflinePanel } from "@/components/daemon-offline-panel";
import { SessionRow } from "@/components/session-row";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { adapters, daemon, sessions } from "@/lib/api";
import { formatDuration } from "@/lib/format";
import { usePoll } from "@/lib/use-poll";

/**
 * The board: what is running, and what needs a human.
 *
 * Deliberately an overview rather than a working surface. The two questions a
 * board answers are "is the daemon up" and "which session should I be looking
 * at", and both are cheap. Lanes, the bus feed and the task queue live on the
 * session page, because fetching all of that for every session to render a
 * summary nobody reads is how a landing page becomes the slowest page in the app.
 *
 * The poll interval is 1s for the daemon and 2s for the session list. That
 * asymmetry is intentional: the daemon's liveness is the thing that can change
 * and invalidate everything on screen, whereas the set of sessions changes on a
 * human timescale.
 */

const POLL_MS = Number(process.env.NEXT_PUBLIC_OPENBURROW_POLL_MS ?? 1000);

export default function BoardPage() {
  const daemonState = usePoll((signal) => daemon.status(signal), {
    intervalMs: POLL_MS,
  });

  const sessionState = usePoll((signal) => sessions.list(true, signal), {
    intervalMs: Math.max(POLL_MS * 2, 2000),
    // Nothing to fetch if the daemon is down; the offline panel covers it, and
    // polling a socket that is not there just fills the log with failures.
    enabled: daemonState.data !== null,
  });

  const adapterState = usePoll((signal) => adapters.list(signal), {
    intervalMs: 30_000,
    enabled: daemonState.data !== null,
  });

  // The daemon being unreachable is a first-class state, not an error toast.
  if (daemonState.error && !daemonState.data) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold tracking-tight">Board</h1>
        <DaemonOfflinePanel
          error={daemonState.error}
          onRetry={daemonState.refresh}
          retrying={daemonState.refreshing}
        />
      </div>
    );
  }

  if (daemonState.loading && !daemonState.data) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold tracking-tight">Board</h1>
        <LoadingState label="Asking the daemon what is running…" />
      </div>
    );
  }

  const status = daemonState.data;
  const openSessions = sessionState.data ?? [];
  const available = (adapterState.data ?? []).filter((adapter) => adapter.available);
  const unavailable = (adapterState.data ?? []).filter((adapter) => !adapter.available);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Board</h1>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Open sessions, and the harnesses this machine can actually run.
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          {daemonState.lastUpdated ? (
            <span>updated {new Date(daemonState.lastUpdated).toLocaleTimeString()}</span>
          ) : null}
          <Button variant="outline" size="sm" onClick={daemonState.refresh}>
            Refresh
          </Button>
        </div>
      </div>

      {/* A failed refresh on top of good data is not an offline state. The
          board keeps rendering what it has and says it is stale — hiding live
          data because one poll failed is worse than showing it with a caveat. */}
      {daemonState.error && status ? (
        <div className="rounded-lg border border-state-blocked/40 bg-state-blocked/10 px-3 py-2 text-xs text-state-blocked">
          The last poll failed ({daemonState.error.code}). What is below may be out of date.
        </div>
      ) : null}

      <Card>
        <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 pt-5 text-xs">
          <div className="flex items-center gap-2">
            <span aria-hidden="true" className="size-2 rounded-full bg-state-done" />
            <span className="font-medium">daemon {status?.version ?? "unknown"}</span>
          </div>
          <div className="text-muted-foreground">
            pid <span className="font-mono tabular-nums">{status?.pid ?? "—"}</span>
          </div>
          <div className="text-muted-foreground">
            up <span className="tabular-nums">{status ? formatDuration(status.uptime_s) : "—"}</span>
          </div>
          <div className="text-muted-foreground">
            repo <span className="font-mono">{status?.repo_root ?? "—"}</span>
          </div>
          <div className="min-w-0 text-muted-foreground">
            socket{" "}
            <span className="font-mono break-all" title={status?.endpoint}>
              {status?.endpoint ?? "—"}
            </span>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
        <section className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Open sessions</h2>
            <span className="text-xs text-muted-foreground tabular-nums">
              {openSessions.length} open
            </span>
          </div>

          {sessionState.error && !sessionState.data ? (
            <ErrorState error={sessionState.error} onRetry={sessionState.refresh} />
          ) : openSessions.length === 0 ? (
            <EmptyState
              title="No open sessions"
              description="A session is the unit of collaboration — one goal, a set of lanes, one bus log, one reel."
              action={
                <div className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-left">
                  <p className="text-xs text-muted-foreground">Start one from the CLI:</p>
                  <code className="mt-1 block font-mono text-xs">
                    burrow session create --name &quot;my goal&quot;
                  </code>
                </div>
              }
            />
          ) : (
            <div className="overflow-hidden rounded-lg border border-border bg-card">
              {openSessions.map((session) => (
                <SessionRow key={session.id} session={session} />
              ))}
            </div>
          )}

          {openSessions.length > 0 ? (
            <p className="text-xs text-muted-foreground">
              Open a session to see its lanes, its bus feed and the tasks waiting on a human.
            </p>
          ) : null}
        </section>

        <aside className="space-y-3">
          <h2 className="text-sm font-semibold">Harnesses</h2>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">
                Available on this machine
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 pt-0">
              {adapterState.loading && !adapterState.data ? (
                <p className="text-xs text-muted-foreground">Probing…</p>
              ) : available.length === 0 ? (
                <p className="text-xs text-state-blocked">
                  None found. A lane needs a harness binary on PATH.
                </p>
              ) : (
                available.map((adapter) => (
                  <div key={adapter.name} className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="font-mono">{adapter.name}</span>
                    <span className="text-muted-foreground">{adapter.version ?? "unknown version"}</span>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          {/* Unavailable harnesses are shown rather than filtered out. Someone
              looking for the harness they know they installed needs to see it
              listed with a reason, not missing with no explanation. */}
          {unavailable.length > 0 ? (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-xs font-medium text-muted-foreground">
                  Present but unusable
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 pt-0">
                {unavailable.map((adapter) => (
                  <div key={adapter.name} className="text-xs">
                    <span className="font-mono">{adapter.name}</span>
                    <p className="text-muted-foreground">
                      {adapter.executable ? `found ${adapter.executable}` : "not on PATH"}
                    </p>
                  </div>
                ))}
              </CardContent>
            </Card>
          ) : null}

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">Where to go next</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5 pt-0 text-xs">
              <Link href="/reels" className="block text-muted-foreground hover:text-foreground">
                Reels — replay a finished session
              </Link>
              <Link href="/relay" className="block text-muted-foreground hover:text-foreground">
                Relay — the cross-machine bus
              </Link>
              <Link href="/system" className="block text-muted-foreground hover:text-foreground">
                System — database, bus, governance
              </Link>
            </CardContent>
          </Card>
        </aside>
      </div>
    </div>
  );
}
