import { callDaemon } from "@/lib/daemon-bridge";
import { fail, handle, readJsonBody } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * Send a prompt to a running lane.
 *
 * An empty prompt is rejected rather than forwarded. The daemon would happily
 * deliver it and emit a `lane.prompted` event with an empty summary, which
 * pollutes the bus feed with a line that says nothing and makes the reel
 * slightly worse every time someone double-clicks. Cheap check, real benefit.
 */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string; laneId: string }> },
): Promise<Response> {
  const { id, laneId } = await params;
  const body = await readJsonBody(request);
  if (!body.ok) return body.response;

  const prompt = typeof body.value.prompt === "string" ? body.value.prompt : "";
  if (!prompt.trim()) {
    return fail(
      {
        code: "openburrow.web.missing_field",
        message: "the prompt is empty",
        context: { field: "prompt" },
      },
      400,
    );
  }

  return handle(() =>
    callDaemon<Record<string, unknown>>("lane.prompt", {
      session: id,
      lane: laneId,
      prompt,
      by: typeof body.value.by === "string" ? body.value.by : "",
    }),
  );
}
