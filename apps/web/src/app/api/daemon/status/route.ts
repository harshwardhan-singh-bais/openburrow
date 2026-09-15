import { callDaemon, describeEndpoint } from "@/lib/daemon-bridge";
import { handle } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * Daemon liveness and identity.
 *
 * Answers 200 even when the daemon is down? No — it answers 503 with the
 * unreachable envelope, because this endpoint's entire job is to say whether the
 * daemon is there. A 200 carrying `{running: false}` would be a lie the browser
 * has to unwrap before it can render anything, and the error path already
 * carries the `hint` that tells the operator what to do about it.
 *
 * `describeEndpoint()` is merged into the success payload so the UI can show
 * *which* socket it is talking to. When two daemons exist on one machine — a
 * repo-local one and a global one — that field is the difference between a
 * confusing bug and a five-second answer.
 */
export async function GET(): Promise<Response> {
  return handle(async () => {
    const status = await callDaemon<Record<string, unknown>>("daemon.status");
    return { ...status, bridge: describeEndpoint() };
  });
}
