import { callDaemon } from "@/lib/daemon-bridge";
import { boolParam, handle } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * A2A tasks in a session.
 *
 * `blocking_only` is the flag the board actually uses. The states that block on
 * a human are `input_required` and `auth_required`, and those are the tasks a
 * dashboard exists to surface — a lane waiting for input is the one thing that
 * cannot resolve itself. The unfiltered list is for the detail page.
 */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  const search = new URL(request.url).searchParams;

  return handle(() =>
    callDaemon<unknown[]>("task.list", {
      session: id,
      blocking_only: boolParam(search, "blocking_only", false),
    }),
  );
}
