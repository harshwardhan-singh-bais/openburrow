import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * The empty, loading and error states, in one place.
 *
 * These three are grouped deliberately. The failure this prevents is a UI where
 * "no lanes yet" and "the daemon is unreachable" render identically — a blank
 * panel — because the component only ever learned about the success case. The
 * honesty rule from the rest of the codebase applies to the UI too: if we do not
 * know, say so.
 */

export function EmptyState({
  title,
  description,
  action,
  className,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-6 py-10 text-center",
        className,
      )}
    >
      <p className="text-sm font-medium">{title}</p>
      {description ? (
        <p className="max-w-md text-xs text-muted-foreground">{description}</p>
      ) : null}
      {action}
    </div>
  );
}

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center justify-center gap-2 px-6 py-10 text-xs text-muted-foreground"
    >
      <span
        aria-hidden
        className="size-3 animate-spin rounded-full border-2 border-current border-t-transparent"
      />
      {label}
    </div>
  );
}

/**
 * The error panel.
 *
 * Renders the OpenBurrow error envelope properly: the code as a machine-readable
 * chip, the message as the heading, the hint as the thing to actually read, and
 * the context as collapsed detail. Collapsing the message and hint into one
 * paragraph is how a UI ends up telling someone "an error occurred" when the
 * daemon took the trouble to say exactly what was wrong and how to fix it.
 *
 * `title` is optional and sits above the message. It exists for the case where
 * the *caller* knows more than the error does — "Could not load this session"
 * tells someone which panel failed, where the raw message may only say
 * `session_not_found`.
 */
export function ErrorState({
  error,
  onRetry,
  title,
  className,
}: {
  error: { code?: string; message: string; hint?: string; context?: Record<string, unknown> };
  onRetry?: () => void;
  title?: string;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3",
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5 size-1.5 shrink-0 rounded-full bg-destructive" aria-hidden />
        <div className="min-w-0 flex-1">
          {title ? <p className="text-sm font-medium">{title}</p> : null}
          {error.code ? (
            <p className="font-mono text-[11px] text-destructive">{error.code}</p>
          ) : null}
          <p className={cn("text-sm", title && "text-muted-foreground")}>{error.message}</p>
          {error.hint ? (
            <p className="mt-1 text-xs text-muted-foreground">{error.hint}</p>
          ) : null}
          {error.context && Object.keys(error.context).length > 0 ? (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-muted-foreground">
                context
              </summary>
              <pre className="scrollbar-slim transcript mt-1 max-h-48 overflow-auto rounded border bg-muted/40 p-2 text-[11px]">
                {JSON.stringify(error.context, null, 2)}
              </pre>
            </details>
          ) : null}
        </div>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="shrink-0 rounded-sm border px-2 py-0.5 text-xs hover:bg-accent"
          >
            Retry
          </button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * A key/value row for detail panels.
 *
 * `value` is the common case and takes anything renderable. `wrap` is off by
 * default because most values are short identifiers where breaking mid-token
 * hurts readability — a path or a socket endpoint is the exception and opts in.
 *
 * A ULID rendered with an ellipsis is useless without a way to see the whole
 * thing, so callers that shorten an id should put the full value in a `title`.
 */
export function DetailRow({
  label,
  value,
  mono = false,
  wrap = false,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  wrap?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-3 py-1">
      <dt className="w-32 shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          "min-w-0 flex-1 text-xs",
          wrap ? "break-all" : "truncate",
          mono && "font-mono",
        )}
        title={typeof value === "string" ? value : undefined}
      >
        {value ?? "—"}
      </dd>
    </div>
  );
}
