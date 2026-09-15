import { callDaemon } from "@/lib/daemon-bridge";
import { handle, intParam } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * Tail the bus.
 *
 * This is the endpoint the board polls, and it is a poll rather than a stream on
 * purpose. The daemon *can* stream (`bus.stream`), but a route handler holding a
 * connection open for the life of a page does not survive a serverless
 * deployment and leaks a socket in every other one. Polling with `since_seq` is
 * self-healing instead: a dropped tick is recovered by the next one, because the
 * cursor is the client's, not the server's.
 *
 * `since_seq` defaults to 0 and `limit` is bounded at 1000. The bound matters —
 * `limit` reaches a SQL `LIMIT`, so an unbounded value is a request that asks
 * the daemon to serialise a whole session's history into one response.
 */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  const search = new URL(request.url).searchParams;

  const since = intParam(search, "since_seq", 0, { min: 0, max: Number.MAX_SAFE_INTEGER });
  if (!since.ok) return since.response;

  const limit = intParam(search, "limit", 200, { min: 1, max: 1000 });
  if (!limit.ok) return limit.response;

  return handle(() =>
    callDaemon<unknown[]>("bus.tail", {
      session: id,
      since_seq: since.value,
      limit: limit.value,
    }),
  );
}
