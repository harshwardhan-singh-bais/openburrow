"use client";

import { useState } from "react";

import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { DetailRow, EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { ApiError, relay } from "@/lib/api";
import { formatDuration, shortId } from "@/lib/format";
import { useLocalStorage } from "@/lib/local-storage";
import { usePoll } from "@/lib/use-poll";
import type { RelayRoom } from "@/types/openburrow";

/**
 * The relay dashboard.
 *
 * The relay is the one component that lives across machines, so this page is
 * about a *connection* rather than about local state. Three states, in order of
 * how often they occur:
 *
 * 1. **No relay configured.** `/api/relay/*` answers 503 with a code saying so,
 *    and the page explains it rather than showing a spinner forever. A 404 here
 *    would be indistinguishable from a typo in the URL.
 * 2. **Relay configured, no token.** The member redeems an invite. The token is
 *    stored in `localStorage` rather than a cookie, deliberately: a cookie is
 *    sent to *every* request to this origin, including the daemon routes, and a
 *    relay credential has no business travelling to the daemon.
 * 3. **Relay configured, token held.** Rooms are listed.
 *
 * The token is per-member and per-room. There is no server-side "service token"
 * anywhere in this app, because a credential that acts as every member at once
 * is exactly the ambient authority the governance layer exists to prevent.
 */

const TOKEN_KEY = "openburrow.relay.token";
const SUBJECT_KEY = "openburrow.relay.subject";

export default function RelayPage() {
  // The credential is an external store, read through the hook that hands the
  // server a null snapshot. See `useLocalStorage`: reading it in the component
  // body is a hydration mismatch, and reading it in a mount effect is a
  // synchronous second render.
  const [token, setToken] = useLocalStorage(TOKEN_KEY);
  const [storedSubject, setStoredSubject] = useLocalStorage(SUBJECT_KEY);

  // The identity field is a form control that *starts* at the stored value.
  // Holding what was typed separately from what is stored is what lets the
  // default be derived in render; `null` means "not typed over yet".
  const [subjectDraft, setSubjectDraft] = useState<string | null>(null);
  const subject = subjectDraft ?? storedSubject ?? "";

  const [invite, setInvite] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [redeeming, setRedeeming] = useState(false);
  const [redeemError, setRedeemError] = useState<ApiError | null>(null);

  const readyz = usePoll((signal) => relay.readyz(signal), { intervalMs: 5000 });

  // Rooms are polled, not fetched once by an effect. The relay's room list
  // changes when somebody else creates a room, so a one-shot fetch produces a
  // list that is stale the moment it is displayed — and `usePoll` already owns
  // the three things a hand-written fetch forgets: it stops while the tab is
  // hidden, it backs off while the relay is unreachable, and it never overlaps
  // requests. It also stops retrying a 401, which is what a stale token
  // produces.
  //
  // `activeToken` is a plain string so the fetcher is typed as one. An empty
  // token never reaches the wire: `enabled` is false, and `usePoll` does not
  // call a disabled fetcher.
  const activeToken = token ?? "";
  const roomsState = usePoll(
    async (signal): Promise<{ token: string; items: RelayRoom[] }> => {
      const result = await relay.rooms(activeToken, signal);
      return { token: activeToken, items: result.rooms };
    },
    { intervalMs: 15_000, enabled: activeToken !== "" },
  );

  // Tagged with the token that fetched it. A list from a previous credential
  // must never be shown as if it belonged to the current one, and tagging
  // makes that impossible without any clearing — clearing is what would
  // otherwise have to happen in an effect, and it is the kind of state two
  // writers can disagree about.
  const fetchedRooms = roomsState.data;
  const visibleRooms =
    fetchedRooms !== null && fetchedRooms.token === activeToken ? fetchedRooms.items : null;

  const redeem = async () => {
    setRedeeming(true);
    setRedeemError(null);
    try {
      const result = await relay.redeem(invite.trim(), subject.trim(), displayName.trim());
      // The relay echoes the subject back normalised, so both the field and the
      // stored default are set from the response rather than from what was typed.
      setToken(result.token);
      setStoredSubject(result.member.subject);
      setSubjectDraft(result.member.subject);
      setInvite("");
      // A new credential makes the old room list the wrong list, and the poll
      // would not run again until its next tick — up to fifteen seconds of an
      // empty panel after a successful redeem.
      roomsState.refresh();
    } catch (error) {
      setRedeemError(
        error instanceof ApiError
          ? error
          : new ApiError({ code: "openburrow.web.redeem_failed", message: "could not redeem" }, 0),
      );
    } finally {
      setRedeeming(false);
    }
  };

  // Dropping the token is the whole action: the room list is keyed by token
  // and hides itself, and the poll is disabled while there is no token.
  const signOut = () => setToken(null);

  const relayMissing =
    readyz.error?.code === "openburrow.web.relay_not_configured" ||
    readyz.error?.status === 503;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Relay</h1>
          <p className="mt-0.5 text-xs text-muted-foreground">
            The cross-machine bus. Rooms are scoped to a repo, not to a person.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {token ? (
            <Button variant="outline" size="sm" onClick={signOut}>
              Forget token
            </Button>
          ) : null}
          <Button variant="outline" size="sm" onClick={readyz.refresh}>
            Refresh
          </Button>
        </div>
      </div>

      {relayMissing ? (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">No relay configured</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            <p className="text-xs text-muted-foreground">
              This deployment has no <code className="font-mono">OPENBURROW_WEB_RELAY_URL</code>, so the
              relay surfaces are disabled. That is the normal state for a single-machine setup — the
              relay exists to carry the bus between machines, and a bus that never leaves one does
              not need it.
            </p>
            <p className="text-xs text-muted-foreground">
              To enable it, run <code className="font-mono">openburrow-relay</code> somewhere both
              machines can reach, then set <code className="font-mono">OPENBURROW_WEB_RELAY_URL</code>
              to its origin and restart the web app.
            </p>
          </CardContent>
        </Card>
      ) : readyz.loading && !readyz.data ? (
        <LoadingState label="Asking the relay whether it is ready…" />
      ) : readyz.error && !readyz.data ? (
        <ErrorState error={readyz.error} onRetry={readyz.refresh} title="Could not reach the relay" />
      ) : readyz.data ? (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              Relay health
              <StatusBadge
                tone={readyz.data.ok ? "done" : "failed"}
                label={readyz.data.ok ? "ready" : "not ready"}
              />
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <dl>
              <DetailRow label="Uptime" value={formatDuration(readyz.data.uptime_s)} />
              <DetailRow
                label="Database"
                value={readyz.data.database.ok ? "reachable" : (readyz.data.database.error ?? "unreachable")}
                mono={!readyz.data.database.ok}
                wrap
              />
              <DetailRow
                label="Open connections"
                value={
                  Object.keys(readyz.data.connections).length === 0
                    ? "none"
                    : Object.entries(readyz.data.connections)
                        .map(([kind, count]) => `${kind}: ${count}`)
                        .join(", ")
                }
                mono
              />
              <DetailRow
                label="Rooms with listeners"
                value={
                  Object.keys(readyz.data.rooms).length === 0
                    ? "none"
                    : Object.entries(readyz.data.rooms)
                        .map(([room, count]) => `${shortId(room)}: ${count}`)
                        .join(", ")
                }
                mono
              />
            </dl>
            {!readyz.data.database.ok ? (
              <p className="mt-2 text-xs text-state-failed">
                The relay is serving but cannot reach its database, so it is not accepting events.
                The bus on each machine is unaffected — the relay is a carrier, not the record.
              </p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {!relayMissing ? (
        token ? (
          <section className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold">Rooms</h2>
              <span className="text-xs text-muted-foreground">
                acting as <span className="font-mono">{subject}</span>
              </span>
            </div>

            {roomsState.error ? (
              <ErrorState error={roomsState.error} onRetry={roomsState.refresh} />
            ) : visibleRooms === null ? (
              <LoadingState label="Loading rooms…" />
            ) : visibleRooms.length === 0 ? (
              <EmptyState
                title="No rooms"
                description="You are a member of no rooms yet. A room is created when a repo is first relayed — ask someone already in it for an invite."
              />
            ) : (
              <div className="overflow-hidden rounded-lg border border-border bg-card">
                {visibleRooms.map((room) => (
                  <div
                    key={room.id}
                    className="flex items-center gap-3 border-b border-border px-3 py-2.5 last:border-b-0"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">{room.name || room.repo_slug}</p>
                      <p className="font-mono text-xs text-muted-foreground">{room.repo_slug}</p>
                    </div>
                    <div className="shrink-0 text-right text-xs text-muted-foreground">
                      <p>retention {room.retention_days}d</p>
                      {room.archived ? <p className="text-state-cancelled">archived</p> : null}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        ) : (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Join a room</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 pt-0">
              <p className="text-xs text-muted-foreground">
                The relay has no passwords. Access is an invite token with 256 bits of entropy,
                stored only as a SHA-256 hash on the relay — so a leaked database does not yield a
                usable invite.
              </p>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1.5 sm:col-span-2">
                  <Label htmlFor="invite">Invite token</Label>
                  <Input
                    id="invite"
                    value={invite}
                    onChange={(event) => setInvite(event.target.value)}
                    placeholder="ob_inv_…"
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono"
                  />
                  <p className="text-[11px] text-muted-foreground">
                    Get one from someone already in the room:{" "}
                    <code className="font-mono">burrow relay invite</code>
                  </p>
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="subject">Your identity</Label>
                  <Input
                    id="subject"
                    value={subject}
                    onChange={(event) => setSubjectDraft(event.target.value)}
                    placeholder="you@example.com"
                    autoComplete="username"
                  />
                  <p className="text-[11px] text-muted-foreground">
                    This is what governance records as the human behind a delegation.
                  </p>
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="display">Display name (optional)</Label>
                  <Input
                    id="display"
                    value={displayName}
                    onChange={(event) => setDisplayName(event.target.value)}
                    placeholder="Your name"
                    autoComplete="name"
                  />
                </div>
              </div>

              {redeemError ? (
                <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs">
                  <p className="font-mono text-destructive">{redeemError.code}</p>
                  <p className="mt-0.5">{redeemError.message}</p>
                  {redeemError.hint ? (
                    <p className="mt-0.5 text-muted-foreground">{redeemError.hint}</p>
                  ) : null}
                </div>
              ) : null}

              <Button
                onClick={() => void redeem()}
                disabled={redeeming || !invite.trim() || !subject.trim()}
                size="sm"
              >
                {redeeming ? "Redeeming…" : "Redeem invite"}
              </Button>
            </CardContent>
          </Card>
        )
      ) : null}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">What the relay does not do</CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <ul className="space-y-1.5 text-xs text-muted-foreground">
            <li>
              <span className="text-foreground">It never invents an event.</span> Every frame carries
              the originating daemon&apos;s sequence number and repo identity. The relay has no
              global counter because it does not assign one.
            </li>
            <li>
              <span className="text-foreground">It is not the record.</span> Each machine&apos;s bus
              log is authoritative. The relay is a carrier with a retention window.
            </li>
            <li>
              <span className="text-foreground">It does not merge documents.</span> Live documents
              are CRDTs; the relay stores snapshots and forwards updates, and merging is the
              clients&apos; job.
            </li>
            <li>
              <span className="text-foreground">It drops the newest frame, not the oldest.</span>{" "}
              Under backpressure it sends an explicit lag notice with a resume point, because
              dropping the oldest lets a client catch up to data that is stale but looks current.
            </li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
