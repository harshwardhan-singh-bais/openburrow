import { callDaemon } from "@/lib/daemon-bridge";
import { handle } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * One session, with the lanes that are currently running.
 *
 * The daemon returns `{session, running_lanes}` — the *running* lanes, not all
 * of them. That is a real distinction: a lane that stopped is history, and
 * history lives on the bus and in the reel. The session detail page therefore
 * shows live lanes from here and takes its stopped-lane story from the timeline,
 * rather than pretending this endpoint is a complete roster.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  return handle(() => callDaemon<Record<string, unknown>>("session.show", { session: id }));
}
