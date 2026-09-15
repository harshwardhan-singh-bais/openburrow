import Link from "next/link";

import { StatusBadge } from "@/components/status-badge";
import { LANE_LABELS, LANE_TONES, formatRelative, needsAttention, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Lane } from "@/types/openburrow";

/**
 * One lane, as a card.
 *
 * The card is ordered by what someone needs to know, in order:
 *
 * 1. **Does it need me?** A lane waiting for input is the only thing on this
 *    board that cannot resolve itself, so when it needs attention the card says
 *    so at the top, in the blocked colour, in words. Everything else is
 *    secondary — which is why the attention strip is the one element that is
 *    allowed to be visually loud.
 * 2. **What is it?** Name, role, harness.
 * 3. **What is it doing?** Status, and the error when it crashed.
 * 4. **What does it hold?** Claims, as chips. Advisory, not locks — the ADR says
 *    so, and the UI must not imply otherwise, which is why they are labelled
 *    "claims" and not "locked".
 *
 * Deliberately a server component: nothing here is interactive. The actions
 * (stop, prompt) live on the detail page, because a "stop" button one click away
 * from a scrolling board is a button someone hits by accident.
 */

export interface LaneCardProps {
  lane: Lane;
  sessionId: string;
  /** Most recent bus summary for this lane, when the board has one. */
  latestSummary?: string | null;
}

export function LaneCard({ lane, sessionId, latestSummary }: LaneCardProps) {
  const tone = LANE_TONES[lane.status];
  const attention = needsAttention(lane.status);
  const active = lane.status === "working" || lane.status === "negotiating";

  return (
    <Link
      href={`/sessions/${encodeURIComponent(sessionId)}/lanes/${encodeURIComponent(lane.id)}`}
      className={cn(
        "group block rounded-lg border bg-card p-4 transition-colors",
        attention ? "border-state-blocked/40 hover:border-state-blocked/70" : "border-border hover:border-ring/60",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium">{lane.name}</h3>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
            <span>{lane.role}</span>
            <span aria-hidden="true">·</span>
            <span className="font-mono">{lane.harness}</span>
            {lane.branch ? (
              <>
                <span aria-hidden="true">·</span>
                <span className="truncate font-mono">{lane.branch}</span>
              </>
            ) : null}
          </p>
        </div>
        <StatusBadge tone={tone} label={LANE_LABELS[lane.status]} pulse={active} />
      </div>

      {attention ? (
        <p className="mt-3 rounded border border-state-blocked/30 bg-state-blocked/10 px-2 py-1.5 text-xs text-state-blocked">
          {lane.status === "waiting_input"
            ? "Waiting on a human. It will not continue on its own."
            : lane.status === "waiting_auth"
              ? "Waiting on an authorisation decision. Nothing is running."
              : "Blocked. Check the bus for what it is waiting on."}
        </p>
      ) : null}

      {lane.last_error ? (
        <p className="mt-3 line-clamp-2 text-xs text-state-failed">{lane.last_error}</p>
      ) : latestSummary ? (
        <p className="mt-3 line-clamp-2 text-xs text-muted-foreground">{latestSummary}</p>
      ) : null}

      {lane.claims.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-1">
          {lane.claims.slice(0, 4).map((claim) => (
            <span
              key={claim}
              // "claim", never "lock". Claims are advisory — two lanes may hold
              // the same one and the daemon will not stop them. Labelling these
              // as locks would teach people to trust a guarantee that does not
              // exist.
              title={`Claimed by ${lane.owner || "unknown"} — advisory, not a lock`}
              className="rounded border border-border bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
            >
              {claim}
            </span>
          ))}
          {lane.claims.length > 4 ? (
            <span className="px-1 py-0.5 text-[11px] text-muted-foreground">
              +{lane.claims.length - 4} more
            </span>
          ) : null}
        </div>
      ) : null}

      <div className="mt-3 flex items-center justify-between border-t border-border pt-2 text-[11px] text-muted-foreground">
        <span className="truncate" title={lane.owner}>
          {lane.owner || "unowned"}
        </span>
        <span className="flex items-center gap-2 font-mono">
          <span title={lane.id}>{shortId(lane.id)}</span>
          <span aria-hidden="true">·</span>
          <span title={`created ${lane.created_at}`}>
            {lane.started_at ? formatRelative(lane.started_at) : "not started"}
          </span>
        </span>
      </div>
    </Link>
  );
}
