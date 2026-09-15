import Link from "next/link";

import { StatusBadge } from "@/components/status-badge";
import { SESSION_TONES, formatRelative, shortId } from "@/lib/format";
import type { Session } from "@/types/openburrow";

/**
 * One session, as a row.
 *
 * A row rather than a card, because sessions are the thing you scan rather than
 * inspect — you are looking for the one that is active, or the one you left
 * running. Cards here would trade vertical density for decoration.
 *
 * The row is a link to the session, and the whole row is the target. That is
 * deliberate for a dense list: a small "open" affordance in a table of thirty
 * rows is a mis-click generator.
 */

export interface SessionRowProps {
  session: Session;
  /** Live lane count, when the board has already fetched it. */
  laneCount?: number;
}

export function SessionRow({ session, laneCount }: SessionRowProps) {
  return (
    <Link
      href={`/sessions/${encodeURIComponent(session.id)}`}
      className="flex items-center gap-3 border-b border-border px-3 py-2.5 transition-colors last:border-b-0 hover:bg-secondary/50"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{session.name}</span>
          <StatusBadge tone={SESSION_TONES[session.status]} label={session.status} />
        </div>
        {session.description ? (
          <p className="mt-0.5 truncate text-xs text-muted-foreground">{session.description}</p>
        ) : null}
      </div>

      <div className="hidden shrink-0 items-center gap-4 text-xs text-muted-foreground sm:flex">
        <span className="font-mono" title={session.branch}>
          {session.branch || session.base_branch}
        </span>
        {typeof laneCount === "number" ? (
          <span className="tabular-nums" title="lanes currently running">
            {laneCount} {laneCount === 1 ? "lane" : "lanes"}
          </span>
        ) : null}
        <span className="w-16 text-right tabular-nums" title={session.created_at}>
          {formatRelative(session.closed_at ?? session.created_at)}
        </span>
        <span className="w-24 truncate text-right font-mono" title={session.id}>
          {shortId(session.id)}
        </span>
      </div>
    </Link>
  );
}
