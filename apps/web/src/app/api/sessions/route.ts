import { callDaemon } from "@/lib/daemon-bridge";
import { boolParam, handle } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * List sessions.
 *
 * `open_only` defaults to true because that is what the board wants and because
 * it is the cheap query. The full list is one flag away rather than being a
 * second endpoint, since the shape is identical.
 */
export async function GET(request: Request): Promise<Response> {
  const params = new URL(request.url).searchParams;
  const openOnly = boolParam(params, "open_only", true);

  return handle(() => callDaemon<unknown[]>("session.list", { open_only: openOnly }));
}
