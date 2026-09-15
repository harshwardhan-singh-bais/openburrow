import { callDaemon } from "@/lib/daemon-bridge";
import { handle } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * The harness adapters this machine can actually run.
 *
 * This is the endpoint that makes the "start a lane" form honest. The daemon
 * probes each adapter for an executable and a version, and returns
 * `available: false` with a reason when a harness is installed but unusable —
 * a `claude` on PATH that is a shell alias, a binary that exists but crashes on
 * `--version`.
 *
 * The UI uses it to disable harness options rather than to filter them out.
 * Filtering would leave someone staring at a list that does not contain the
 * harness they know they installed, with nothing to explain why; showing it
 * greyed out with the reason attached answers the question they actually have.
 */
export async function GET(): Promise<Response> {
  return handle(() => callDaemon<unknown[]>("adapters.list"));
}
