"use client";

import { useState } from "react";

import { SessionRow } from "@/components/session-row";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { sessions } from "@/lib/api";
import { usePoll } from "@/lib/use-poll";

/**
 * Every session, open or closed.
 *
 * The board shows open ones because that is what you act on. This page exists
 * for the other question — "what happened last week" — which is a search over
 * history rather than a view of live state. Same rows, different query.
 */

export default function SessionsPage() {
  const [openOnly, setOpenOnly] = useState(false);

  const state = usePoll((signal) => sessions.list(openOnly, signal), {
    intervalMs: 5000,
  });

  const rows = state.data ?? [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Sessions</h1>
          <p className="mt-0.5 text-xs text-muted-foreground">
            One session is one goal: a set of lanes, one bus log, one reel.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div role="radiogroup" aria-label="Filter" className="flex rounded-md border border-border p-0.5">
            <button
              type="button"
              role="radio"
              aria-checked={!openOnly}
              onClick={() => setOpenOnly(false)}
              className={
                !openOnly
                  ? "rounded bg-secondary px-2.5 py-1 text-xs font-medium text-secondary-foreground"
                  : "rounded px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground"
              }
            >
              All
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={openOnly}
              onClick={() => setOpenOnly(true)}
              className={
                openOnly
                  ? "rounded bg-secondary px-2.5 py-1 text-xs font-medium text-secondary-foreground"
                  : "rounded px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground"
              }
            >
              Open only
            </button>
          </div>
          <Button variant="outline" size="sm" onClick={state.refresh}>
            Refresh
          </Button>
        </div>
      </div>

      {state.loading && !state.data ? (
        <LoadingState label="Loading sessions…" />
      ) : state.error && !state.data ? (
        <ErrorState error={state.error} onRetry={state.refresh} title="Could not list sessions" />
      ) : rows.length === 0 ? (
        <EmptyState
          title={openOnly ? "No open sessions" : "No sessions yet"}
          description={
            openOnly
              ? "Every session has been closed. Switch to All to see the history."
              : "Nothing has been created in this repo yet."
          }
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          {rows.map((session) => (
            <SessionRow key={session.id} session={session} />
          ))}
        </div>
      )}

      {state.error && state.data ? (
        <p className="text-xs text-state-blocked">
          The last poll failed ({state.error.code}); this list may be out of date.
        </p>
      ) : null}
    </div>
  );
}
