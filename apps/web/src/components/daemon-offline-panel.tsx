"use client";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ApiError } from "@/lib/api";

/**
 * The "the daemon is not there" panel.
 *
 * This is the most important empty state in the app, because it is the state
 * every new user hits first. A blank board with a spinner and a red toast
 * teaches nothing; what the operator needs is:
 *
 * 1. **What was attempted** — the method and the endpoint, from the error's own
 *    `context`. "Could not reach the daemon" without saying *where* is a
 *    support ticket.
 * 2. **Why it probably failed** — the `hint`, which distinguishes "no socket at
 *    that path" from "a socket exists but nobody is listening", because those
 *    have different fixes.
 * 3. **What to do next** — the actual commands, copyable.
 *
 * The commands are shown as literal text rather than a "run this" button,
 * because the web app has no business executing a daemon start on the
 * operator's machine. That would be a remote code execution surface wearing a
 * convenience costume.
 */

export interface DaemonOfflinePanelProps {
  error: ApiError;
  onRetry: () => void;
  retrying?: boolean;
}

export function DaemonOfflinePanel({ error, onRetry, retrying = false }: DaemonOfflinePanelProps) {
  const context = error.context as Record<string, unknown>;
  const endpoint = typeof context.endpoint === "string" ? context.endpoint : null;
  const method = typeof context.method === "string" ? context.method : null;
  const errno = typeof context.errno === "string" ? context.errno : null;

  return (
    <Card className="border-state-failed/40">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <span aria-hidden="true" className="size-2 rounded-full bg-state-failed" />
          The burrow daemon is not answering
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          {error.hint ??
            "The web app could not reach the daemon's control socket. Nothing on this page is live."}
        </p>

        <dl className="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2">
          <div className="flex flex-col gap-0.5">
            <dt className="text-muted-foreground">error code</dt>
            <dd className="font-mono">{error.code}</dd>
          </div>
          {method ? (
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">method</dt>
              <dd className="font-mono">{method}</dd>
            </div>
          ) : null}
          {endpoint ? (
            <div className="flex flex-col gap-0.5 sm:col-span-2">
              <dt className="text-muted-foreground">endpoint</dt>
              <dd className="font-mono break-all">{endpoint}</dd>
            </div>
          ) : null}
          {errno ? (
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">errno</dt>
              <dd className="font-mono">{errno}</dd>
            </div>
          ) : null}
        </dl>

        <div className="rounded-lg border border-border bg-muted/40 p-3">
          <p className="text-xs font-medium">Usually one of these</p>
          <ul className="mt-2 space-y-2 text-xs text-muted-foreground">
            <li className="flex flex-col gap-0.5">
              <span>The daemon is not running. Start one in the repo:</span>
              <code className="font-mono text-foreground">burrow daemon start</code>
            </li>
            <li className="flex flex-col gap-0.5">
              <span>One is running but not where the web app is looking:</span>
              <code className="font-mono text-foreground">burrow daemon status</code>
            </li>
            <li className="flex flex-col gap-0.5">
              <span>
                It is running elsewhere — point the web app at it with{" "}
                <code className="font-mono">OPENBURROW_DAEMON_SOCKET</code>, or tell it which repo
                to inspect with <code className="font-mono">OPENBURROW_REPO_ROOT</code>.
              </span>
            </li>
          </ul>
        </div>

        <div className="flex items-center gap-3">
          <Button onClick={onRetry} disabled={retrying} size="sm">
            {retrying ? "Retrying…" : "Try again"}
          </Button>
          <span className="text-xs text-muted-foreground">
            The board resumes on its own as soon as the daemon answers.
          </span>
        </div>

        {Object.keys(error.context).length > 0 ? (
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">Full error context</summary>
            <pre className="scrollbar-slim transcript mt-2 max-h-48 overflow-auto rounded border border-border bg-background p-2">
              {JSON.stringify(error.context, null, 2)}
            </pre>
          </details>
        ) : null}
      </CardContent>
    </Card>
  );
}
