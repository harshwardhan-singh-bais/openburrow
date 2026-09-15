"use client";

import { DaemonOfflinePanel } from "@/components/daemon-offline-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DetailRow, LoadingState } from "@/components/ui/states";
import { daemon } from "@/lib/api";
import { formatDuration } from "@/lib/format";
import { usePoll } from "@/lib/use-poll";

/**
 * The system page: the daemon's own report.
 *
 * This is the page you open when something is wrong. It deliberately shows the
 * daemon's raw self-report rather than a prettified summary, because the whole
 * value of the page is that it can contradict a reassuring green dot elsewhere.
 *
 * Three things are surfaced in a specific way:
 *
 * - **A failed database is a red row inside a healthy page**, not a page-level
 *   error. A daemon with an unreachable database is running, and saying
 *   otherwise sends someone to restart a process that is fine.
 * - **Governance shows its limits, not just "enabled"**. `max_delegation_depth`
 *   is the number that decides whether a delegation chain is legal, and hiding
 *   it behind a boolean is how a policy surprise reaches production.
 * - **The bus subscriber count is shown**, because a bus with zero subscribers
 *   during an active session means nobody is listening to the agents — which is
 *   a real and confusing failure.
 */

const POLL_MS = 5000;

export default function SystemPage() {
  const status = usePoll((signal) => daemon.status(signal), { intervalMs: POLL_MS });
  const health = usePoll((signal) => daemon.health(signal), {
    intervalMs: POLL_MS,
    enabled: status.data !== null,
  });

  if (status.error && !status.data) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold tracking-tight">System</h1>
        <DaemonOfflinePanel
          error={status.error}
          onRetry={status.refresh}
          retrying={status.refreshing}
        />
      </div>
    );
  }

  if (status.loading && !status.data) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold tracking-tight">System</h1>
        <LoadingState label="Asking the daemon to report on itself…" />
      </div>
    );
  }

  const info = status.data;
  const report = health.data;
  const database = report?.database;
  const governance = report?.governance;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">System</h1>
          <p className="mt-0.5 text-xs text-muted-foreground">
            The daemon&apos;s own account of itself. If this disagrees with anything else on screen,
            this is the one to trust.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={status.refresh}>
          Refresh
        </Button>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Daemon</CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <dl>
              <DetailRow label="Version" value={info?.version} mono />
              <DetailRow label="PID" value={info?.pid} mono />
              <DetailRow label="Uptime" value={info ? formatDuration(info.uptime_s) : undefined} />
              <DetailRow label="Requests served" value={info?.requests_served} mono />
              <DetailRow label="Repo root" value={info?.repo_root} mono />
              <DetailRow label="Control socket" value={info?.endpoint} mono wrap />
            </dl>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              Database
              {database ? (
                <StatusBadge
                  tone={database.ok ? "done" : "failed"}
                  label={database.ok ? "reachable" : "unreachable"}
                />
              ) : null}
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {!report ? (
              <p className="text-xs text-muted-foreground">Waiting for the daemon&apos;s report…</p>
            ) : database?.ok ? (
              <p className="text-xs text-muted-foreground">
                The daemon can read and write its SQLite database. The bus log and the session
                record are on disk and durable.
              </p>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-state-failed">
                  {database?.error ?? "The daemon could not open its database."}
                </p>
                <p className="text-xs text-muted-foreground">
                  The daemon is still running, but nothing it does can be recorded — which means
                  nothing on the board is live and the bus is not accumulating.
                </p>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Bus</CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <dl>
              <DetailRow label="Subscribers" value={report?.bus.subscribers} mono />
            </dl>
            {report && report.bus.subscribers === 0 ? (
              <p className="mt-2 text-xs text-state-blocked">
                Nothing is listening. An active session with no subscribers means no lane is
                receiving anything another lane sends.
              </p>
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">
                The bus is append-only and is the canonical record. Subscribers are live listeners
                — a CLI watching, or a lane waiting on a message.
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              Governance
              {governance ? (
                <StatusBadge
                  tone={governance.enabled ? "governance" : "cancelled"}
                  label={governance.enabled ? "enforcing" : "disabled"}
                />
              ) : null}
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {!report ? (
              <p className="text-xs text-muted-foreground">Waiting for the daemon&apos;s report…</p>
            ) : governance?.enabled ? (
              <>
                <dl>
                  <DetailRow
                    label="Max delegation depth"
                    value={governance.max_delegation_depth}
                    mono
                  />
                  <DetailRow label="Authority inheritance" value={governance.authority_inheritance} mono />
                </dl>
                <p className="mt-2 text-xs text-muted-foreground">
                  Authority originates with a human and can only ever narrow. Depth {governance.max_delegation_depth}{" "}
                  means a chain longer than that is refused rather than trimmed.
                </p>
              </>
            ) : (
              <p className="text-xs text-state-blocked">
                Governance is off. Delegations are not being checked and refusals are not being
                recorded — a lane can act with whatever authority it was started with.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Harness adapters</CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          {!report ? (
            <p className="text-xs text-muted-foreground">Waiting for the daemon&apos;s report…</p>
          ) : Object.keys(report.adapters).length === 0 ? (
            <p className="text-xs text-state-blocked">No adapters registered.</p>
          ) : (
            <dl className="grid gap-x-6 sm:grid-cols-2">
              {Object.entries(report.adapters).map(([name, ok]) => (
                <div
                  key={name}
                  className="flex items-center justify-between gap-3 border-b border-border py-2 last:border-b-0"
                >
                  <dt className="font-mono text-xs">{name}</dt>
                  <dd>
                    <StatusBadge tone={ok ? "done" : "cancelled"} label={ok ? "available" : "missing"} />
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
