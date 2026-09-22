import type { LaneStatus, SessionStatus, StepStatus, TaskState } from "@/types/openburrow";

/**
 * Presentation of domain state.
 *
 * The colour mapping here is the same one the Python CLI uses in
 * `openburrow/cli/output.py`. Keeping them in one place per language — rather
 * than sprinkling `text-amber-500` through components — is what makes "blocked is
 * amber everywhere" a fact rather than a convention someone has to remember.
 */

export type StateTone =
  | "idle"
  | "working"
  | "blocked"
  | "done"
  | "failed"
  | "cancelled"
  | "governance";

export const TONE_CLASSES: Record<StateTone, string> = {
  idle: "text-state-idle border-state-idle/30 bg-state-idle/10",
  working: "text-state-working border-state-working/30 bg-state-working/10",
  blocked: "text-state-blocked border-state-blocked/30 bg-state-blocked/10",
  done: "text-state-done border-state-done/30 bg-state-done/10",
  failed: "text-state-failed border-state-failed/30 bg-state-failed/10",
  cancelled: "text-state-cancelled border-state-cancelled/30 bg-state-cancelled/10",
  governance: "text-governance border-governance/30 bg-governance/10",
};

/** The solid dot next to a status badge. */
export const TONE_DOT: Record<StateTone, string> = {
  idle: "bg-state-idle",
  working: "bg-state-working",
  blocked: "bg-state-blocked",
  done: "bg-state-done",
  failed: "bg-state-failed",
  cancelled: "bg-state-cancelled",
  governance: "bg-governance",
};

export const LANE_TONES: Record<LaneStatus, StateTone> = {
  starting: "idle",
  idle: "idle",
  working: "working",
  waiting_input: "blocked",
  waiting_auth: "blocked",
  watching: "working",
  blocked: "blocked",
  negotiating: "working",
  crashed: "failed",
  stopped: "cancelled",
};

export const LANE_LABELS: Record<LaneStatus, string> = {
  starting: "starting",
  idle: "idle",
  working: "working",
  waiting_input: "needs input",
  waiting_auth: "needs auth",
  watching: "watching",
  blocked: "blocked",
  negotiating: "negotiating",
  crashed: "crashed",
  stopped: "stopped",
};

export const SESSION_TONES: Record<SessionStatus, StateTone> = {
  created: "idle",
  active: "working",
  paused: "blocked",
  completed: "done",
  abandoned: "cancelled",
  failed: "failed",
};

export const TASK_TONES: Record<TaskState, StateTone> = {
  submitted: "idle",
  working: "working",
  input_required: "blocked",
  auth_required: "blocked",
  completed: "done",
  failed: "failed",
  canceled: "cancelled",
  rejected: "governance",
};

export const STEP_TONES: Record<StepStatus, StateTone> = {
  pending: "idle",
  claimed: "idle",
  in_progress: "working",
  blocked: "blocked",
  review: "working",
  done: "done",
  failed: "failed",
  skipped: "cancelled",
};

/**
 * Which lane states mean "this lane needs a human".
 *
 * Exported as a predicate rather than used inline because the definition is a
 * policy decision — `blocked` is included, `negotiating` is not — and a policy
 * decision buried in a component's conditional is a policy decision nobody can
 * find when it needs changing.
 */
export function needsAttention(status: LaneStatus): boolean {
  return status === "waiting_input" || status === "waiting_auth" || status === "blocked";
}

/** Human-readable duration. Deliberately coarse: "3h 12m", never "3h 12m 4s". */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.floor(seconds % 60)}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** Relative time. "just now", "4m ago", "3d ago". */
export function formatRelative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const deltaSeconds = (now - then) / 1000;
  if (deltaSeconds < 5) return "just now";
  if (deltaSeconds < 60) return `${Math.floor(deltaSeconds)}s ago`;
  if (deltaSeconds < 3600) return `${Math.floor(deltaSeconds / 60)}m ago`;
  if (deltaSeconds < 86400) return `${Math.floor(deltaSeconds / 3600)}h ago`;
  return `${Math.floor(deltaSeconds / 86400)}d ago`;
}

/** Absolute local time, to the second. For tooltips where "4m ago" is not enough. */
export function formatAbsolute(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * `00:01:23.4` — the reel scrubber and the cast timeline both want this.
 *
 * Rounds to the nearest tenth, and the rounding is load-bearing rather than
 * cosmetic. Truncating cannot be done exactly here: `59.9` is stored as
 * `59.8999999999999985789…`, so `Math.floor((seconds % 1) * 10)` renders it as
 * `00:00:59.8` — a number wrong in a plausible direction, which is the failure
 * `AGENTS.md` rule 6 names. Every value the old expression got wrong was wrong by
 * exactly one tenth, and the ones it happened to get right (`83.4`, `3599.9`) are
 * why the bug survived: it looked intermittent.
 *
 * Rounding the total to tenths *first* is what makes it exact, and it also keeps
 * the two halves consistent — deriving the seconds and the tenth separately is how
 * `59.96` becomes `00:00:59.0`, a timecode that has gone backwards. The cost is
 * that `59.96` now reads `00:01:00.0` rather than `00:00:59.9`; for a display that
 * shows one decimal, the nearest tenth is the honest answer to give.
 */
export function formatTimecode(seconds: number, withTenths = true): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const tenths = Math.round(seconds * 10);
  const hours = Math.floor(tenths / 36000);
  const minutes = Math.floor((tenths % 36000) / 600);
  const whole = Math.floor((tenths % 600) / 10);
  const pad = (value: number) => String(value).padStart(2, "0");
  const base = `${pad(hours)}:${pad(minutes)}:${pad(whole)}`;
  if (!withTenths) return base;
  return `${base}.${tenths % 10}`;
}

/**
 * Shorten a ULID-ish id for display, keeping the readable prefix.
 *
 * `sess_01HQ8Z4K2M9N7P1Q3R5T7V9X1B` becomes `sess_…7V9X1B`. The prefix matters
 * (it says what kind of thing it is) and the tail matters (it disambiguates
 * within a screen). The middle is noise.
 */
export function shortId(id: string | null | undefined, tail = 6): string {
  if (!id) return "—";
  const underscore = id.indexOf("_");
  if (underscore < 0) {
    return id.length <= tail + 2 ? id : `…${id.slice(-tail)}`;
  }
  const prefix = id.slice(0, underscore + 1);
  const body = id.slice(underscore + 1);
  if (body.length <= tail) return id;
  return `${prefix}…${body.slice(-tail)}`;
}

/**
 * Compact byte counts. `1.2 MB`, `940 kB`.
 *
 * Binary units with decimal labels, which is technically wrong and is what
 * everyone means. Using MiB would be correct and would confuse every reader.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["kB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unitIndex]}`;
}

/**
 * Colour a bus event by its type.
 *
 * Event types are `namespace.action` by convention, so the namespace is enough
 * to classify almost everything without a lookup table that has to be kept in
 * sync with the daemon's emitters.
 */
export function eventTone(eventType: string): StateTone {
  const namespace = eventType.split(".")[0] ?? "";
  switch (namespace) {
    case "lane":
      return "working";
    case "task":
      return "working";
    case "session":
      return "idle";
    case "governance":
    case "delegation":
    case "approval":
    case "policy":
      return "governance";
    case "bus":
    case "daemon":
      return "idle";
    case "radar":
      return "blocked";
    case "brain":
      return "done";
    default:
      return "idle";
  }
}

/** Is this event type a refusal rather than a failure? */
export function isGovernanceEvent(eventType: string): boolean {
  return eventTone(eventType) === "governance";
}
