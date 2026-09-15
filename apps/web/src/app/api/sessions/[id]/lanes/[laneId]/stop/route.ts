import { callDaemon } from "@/lib/daemon-bridge";
import { handle, readJsonBody } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * Stop a lane.
 *
 * A POST, not a DELETE, and the distinction is not pedantry: DELETE says "remove
 * this resource", and a stopped lane is not removed. It keeps its branch, its
 * worktree, its cast and its place in the bus log, and the reel will still show
 * it. What changes is that it is no longer running — which is a state
 * transition, which is what POST is for.
 *
 * `reason` is passed through rather than defaulted here. The daemon writes it
 * onto the `lane.stopped` event, and "why did this stop" is the single most
 * asked question of a reel. A UI that invents a reason is a UI that poisons the
 * record.
 */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string; laneId: string }> },
): Promise<Response> {
  const { id, laneId } = await params;
  const body = await readJsonBody(request);
  if (!body.ok) return body.response;

  return handle(() =>
    callDaemon<Record<string, unknown>>("lane.stop", {
      session: id,
      lane: laneId,
      reason: typeof body.value.reason === "string" ? body.value.reason : "",
    }),
  );
}
