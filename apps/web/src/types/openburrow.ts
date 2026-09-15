/**
 * The OpenBurrow domain vocabulary, mirroring `openburrow.core.models.enums`.
 *
 * These are hand-written rather than generated, and that is a deliberate choice
 * with a cost. The alternative — generating from the Python enums — was rejected
 * because the generated file would be correct and unreadable, and the parts of
 * this file that matter are the *relationships* (which states are terminal, which
 * transitions are legal), not the string literals.
 *
 * The contract is enforced by `scripts/check_enum_parity.py`, which reads the
 * arrays below and compares them against the Python enums. A drift therefore
 * fails CI rather than producing a UI that renders an unknown state as blank.
 */

// ---------------------------------------------------------------------------
// A2A task lifecycle
// ---------------------------------------------------------------------------

/** The eight states A2A defines. */
export const TASK_STATES = [
  "submitted",
  "working",
  "input_required",
  "auth_required",
  "completed",
  "failed",
  "canceled",
  "rejected",
] as const;

export type TaskState = (typeof TASK_STATES)[number];

/** States a task never leaves. */
export const TERMINAL_TASK_STATES: readonly TaskState[] = [
  "completed",
  "failed",
  "canceled",
  "rejected",
];

/** The two states that block on a human. */
export const BLOCKING_TASK_STATES: readonly TaskState[] = [
  "input_required",
  "auth_required",
];

/**
 * Legal transitions, mirroring `TASK_TRANSITIONS` in Python.
 *
 * The UI uses this to disable buttons rather than to validate after the fact. An
 * action that the protocol will reject should not be clickable — showing someone
 * a button that produces an error is worse than not showing the button, because
 * it implies the action is possible.
 */
export const TASK_TRANSITIONS: Record<TaskState, readonly TaskState[]> = {
  submitted: ["working", "rejected", "canceled", "failed"],
  working: ["input_required", "auth_required", "completed", "failed", "canceled"],
  input_required: ["working", "canceled", "failed"],
  auth_required: ["working", "canceled", "failed", "rejected"],
  completed: [],
  failed: [],
  canceled: [],
  rejected: [],
};

export function isTerminal(state: TaskState): boolean {
  return TERMINAL_TASK_STATES.includes(state);
}

export function canTransition(from: TaskState, to: TaskState): boolean {
  return TASK_TRANSITIONS[from].includes(to);
}

// ---------------------------------------------------------------------------
// ACP negotiation
// ---------------------------------------------------------------------------

export const PERFORMATIVES = [
  "propose",
  "counter",
  "accept",
  "reject",
  "inform",
  "withdraw",
] as const;

export type Performative = (typeof PERFORMATIVES)[number];

/**
 * What may legally follow each performative. Mirrors `PERFORMATIVE_REPLIES` in
 * `openburrow/core/models/enums.py` exactly.
 *
 * Note the asymmetry, because it is not a mistake: `reject`, `inform` and
 * `withdraw` are **not** dead ends. A rejection is an opening for a counter; an
 * `inform` is a statement of fact that may be answered with a new proposal. Only
 * a negotiation that reaches an agreed outcome closes. Treating `reject` as
 * terminal — which an earlier version of this file did — would disable the
 * "counter this" action in exactly the case where it is most useful.
 */
export const PERFORMATIVE_REPLIES: Record<Performative, readonly Performative[]> = {
  propose: ["accept", "reject", "counter", "withdraw"],
  counter: ["accept", "reject", "counter", "withdraw"],
  accept: ["inform"],
  reject: ["propose", "inform"],
  inform: ["propose", "counter", "inform"],
  withdraw: ["propose", "inform"],
};

// ---------------------------------------------------------------------------
// Lane and session state
// ---------------------------------------------------------------------------

export const LANE_STATUSES = [
  "starting",
  "idle",
  "working",
  "waiting_input",
  "waiting_auth",
  "watching",
  "blocked",
  "negotiating",
  "crashed",
  "stopped",
] as const;

export type LaneStatus = (typeof LANE_STATUSES)[number];

export const SESSION_STATUSES = [
  "created",
  "active",
  "paused",
  "completed",
  "abandoned",
  "failed",
] as const;

export type SessionStatus = (typeof SESSION_STATUSES)[number];

export const LANE_ROLES = [
  "implementer",
  "reviewer",
  "observer",
  "coordinator",
  "custom",
] as const;

export type LaneRole = (typeof LANE_ROLES)[number];

export const STEP_STATUSES = [
  "pending",
  "claimed",
  "in_progress",
  "blocked",
  "review",
  "done",
  "failed",
  "skipped",
] as const;

export type StepStatus = (typeof STEP_STATUSES)[number];

// ---------------------------------------------------------------------------
// Entities
// ---------------------------------------------------------------------------

export interface Lane {
  id: string;
  session_id: string;
  name: string;
  /** Which harness backs this lane — "claude-code", "opencode", "crush", … */
  harness: string;
  role: LaneRole;
  status: LaneStatus;
  /** The human or agent this lane acts for. Never empty for a started lane. */
  owner: string;
  /** Advisory, not locks. See ADR 0008. */
  claims: string[];
  branch?: string | null;
  worktree?: string | null;
  created_at: string;
  started_at?: string | null;
  stopped_at?: string | null;
  /** Present when the lane crashed. The reason, not a stack trace. */
  last_error?: string | null;
}

export interface Session {
  id: string;
  name: string;
  description: string;
  status: SessionStatus;
  branch: string;
  base_branch: string;
  owner: string;
  tags: string[];
  created_at: string;
  closed_at?: string | null;
}

export interface BusEvent {
  /**
   * Monotonic within the originating daemon. This is the ordering key; a ULID
   * would only sort at millisecond granularity, which is too coarse for a bus.
   */
  seq: number;
  event_type: string;
  session_id?: string | null;
  lane_id?: string | null;
  task_id?: string | null;
  summary: string;
  payload: Record<string, unknown>;
  /** ULID. Identifies the event; does not order it. */
  id: string;
  created_at: string;
  /**
   * Which daemon this came from, when it arrived via the relay. Absent for
   * local events, where there is only one origin.
   */
  origin_repo?: string;
  origin_seq?: number;
}

export interface A2ATask {
  id: string;
  session_id: string;
  state: TaskState;
  /** The lane that owns the task. */
  lane_id?: string | null;
  /** The lane that asked for it, if any. */
  parent_lane_id?: string | null;
  description: string;
  created_at: string;
  updated_at: string;
  /** Depth in the delegation chain. Governance caps this. */
  delegation_depth?: number;
}

// ---------------------------------------------------------------------------
// Governance
// ---------------------------------------------------------------------------

/** Mirrors `DelegationStatus`. */
export const DELEGATION_STATUSES = [
  "proposed",
  "authorized",
  "active",
  "completed",
  "revoked",
  "denied",
] as const;

export type DelegationStatus = (typeof DELEGATION_STATUSES)[number];

export interface Delegation {
  id: string;
  session_id: string;
  status: DelegationStatus;
  /** Who granted. Always traces back to a human. */
  grantor: string;
  grantee: string;
  /** What the grantee may do. Denials are consulted before grants. */
  scope: string[];
  /** Explicit refusals. A wildcard grant cannot bypass one of these. */
  denials: string[];
  depth: number;
  created_at: string;
  revoked_at?: string | null;
}

/**
 * Mirrors `ApprovalStatus`.
 *
 * The two members that are easy to get wrong and matter most are
 * `approved_edited` and `escalated`. An approval that was *changed* before being
 * granted is not the same as one that was granted as asked — rendering both as
 * "approved" hides the human's edit, which is precisely the thing an audit wants
 * to see. And `escalated` means the decision moved up a level, not that it was
 * refused.
 */
export const APPROVAL_STATUSES = [
  "pending",
  "approved",
  "approved_edited",
  "denied",
  "timed_out",
  "escalated",
] as const;

export type ApprovalStatus = (typeof APPROVAL_STATUSES)[number];

export interface Approval {
  id: string;
  session_id: string;
  lane_id?: string | null;
  /** What is being asked for. */
  action: string;
  status: ApprovalStatus;
  requested_by: string;
  requested_at: string;
  decided_by?: string | null;
  decided_at?: string | null;
  reason?: string | null;
}

// ---------------------------------------------------------------------------
// Relay
// ---------------------------------------------------------------------------

export interface RelayRoom {
  id: string;
  repo_slug: string;
  name: string;
  archived: boolean;
  retention_days: number;
}

export interface RelayEventFrame {
  type: "event";
  origin_repo: string;
  origin_seq: number;
  event_type: string;
  session_id?: string | null;
  lane_id?: string | null;
  summary?: string | null;
  payload: Record<string, unknown>;
  occurred_at?: string | null;
}

/**
 * Emitted when the relay dropped frames for this connection.
 *
 * This is not an error. It is the relay telling the truth about what it did, and
 * the client's only correct response is to stop, re-tail from `resume_from`, and
 * resume — anything else means continuing with a view that is silently missing
 * events.
 */
export interface RelayLagFrame {
  type: "lag";
  dropped: number;
  resume_from: Record<string, number>;
  hint: string;
}

export interface RelayHelloFrame {
  type: "hello";
  connection: string;
  room: string;
  you: { member: string; subject: string; role: "viewer" | "maintainer" | "owner" };
  latest_seqs: Record<string, number>;
  limits: {
    max_frame_bytes: number;
    event_rate_per_s: number;
    event_burst: number;
    max_events_per_publish: number;
  };
}

export interface RelayAckFrame {
  type: "ack";
  accepted: number;
  duplicates: number;
  rejected?: Array<Record<string, unknown>>;
}

export interface RelayErrorFrame {
  type: "error";
  code: string;
  message: string;
  retry_after_s?: number;
  hint?: string;
}

export type RelayFrame =
  | RelayEventFrame
  | RelayLagFrame
  | RelayHelloFrame
  | RelayAckFrame
  | RelayErrorFrame
  | { type: "pong"; at: string };

// ---------------------------------------------------------------------------
// Session reel
//
// These mirror `reel/exporter.py` and `reel/timeline.py` field for field. The
// shapes were read off the exporter rather than guessed, because a viewer that
// renders a field the exporter does not emit is a viewer that shows blank
// columns and nobody notices until a demo.
// ---------------------------------------------------------------------------

/**
 * One entry in a reel timeline. Mirrors `TimelineEntry`.
 *
 * `caused_by` holds an entry **id**, not an index. That is the load-bearing
 * field: it is what turns a transcript into something you can interrogate.
 * Without it, "why did lane C stop" is a search problem; with it, it is a walk —
 * and the walk is the feature the reel exists for.
 */
export interface TimelineEntry {
  /** Seconds since the session started. Shares a clock with the lane casts. */
  t: number;
  /** Monotonic bus sequence, when the entry came from the bus. 0 otherwise. */
  seq: number;
  /** Dotted type, e.g. `claim.created` or `negotiation.move`. */
  type: string;
  lane_id: string;
  summary: string;
  /** The id of the entry that caused this one. Empty when unknown. */
  caused_by: string;
  /** Stable id for this entry, so other entries can point at it. */
  id: string;
  payload: Record<string, unknown>;
}

/**
 * `[seconds, text]` — terminal output for one lane, already stripped of
 * attribution because the whole array belongs to one lane.
 */
export type LaneOutputEvent = [number, string];

export interface ReelLane {
  id: string;
  title: string;
  has_cast: boolean;
  /** Why there is no cast. Never empty when `has_cast` is false. */
  reason_missing?: string | null;
  output_events: number;
  input_events: number;
  events: LaneOutputEvent[];
}

export interface ReelCoverage {
  lane_id: string;
  has_cast: boolean;
  reason_missing?: string | null;
}

export interface ReelSession {
  id: string;
  name: string;
  duration_s: number;
  started_at: string;
}

/** The contents of `reel.json`. */
export interface ReelBundle {
  session: ReelSession;
  lanes: ReelLane[];
  timeline: TimelineEntry[];
  coverage: ReelCoverage[];
}

/** `reel.manifest.json` — what the export contained and how it was signed. */
export interface ReelManifest {
  session_id: string;
  session_name: string;
  /** ISO 8601. Python serialises a `datetime` here. */
  exported_at: string;
  exporter: string;
  cast_files: Record<string, string>;
  a2a_trace_file: string;
  timeline_file: string;
  plan_file: string;
  audit_file: string;
  metrics_file: string;
  lane_count: number;
  event_count: number;
  negotiation_count: number;
  governance_event_count: number;
  duration_seconds: number;
  total_cost_usd?: number | null;

  // --- sharing ------------------------------------------------------------
  // Added to match the Python model, which had all of these and this mirror
  // had none. `signed`, `signature` and `expires_at` were the important
  // omissions: a viewer that cannot see that a link is signed or expiring
  // cannot tell the user, which is the whole point of scoping a share link.
  signed: boolean;
  signature: string;
  expires_at: string | null;
  /** Derived server-side from `expires_at`; never stored. */
  is_expired: boolean;

  allowed_orgs?: string[];
  public: boolean;
  viewer_version: string;
}

/**
 * asciinema v2 cast, as written by `reel/cast.py`.
 *
 * Custom event kinds are namespaced `x-`, because the v2 spec reserves single
 * letters. The five the recorder emits are listed below; the type stays `string`
 * because a future recorder version may add more and a viewer that rejects an
 * unknown `x-` kind would be a viewer that breaks on an upgrade.
 */
export const CAST_OUTPUT = "o";
export const CAST_INPUT = "i";
export const CAST_EVENT_LANE = "x-lane";
export const CAST_EVENT_CLAIM = "x-claim";
export const CAST_EVENT_NEGOTIATION = "x-negotiation";
export const CAST_EVENT_GOVERNANCE = "x-governance";
export const CAST_EVENT_TASK = "x-task";

/** `[time, kind, data]` */
export type CastEvent = [number, string, string];

export interface CastHeader {
  version: number;
  width: number;
  height: number;
  timestamp?: number;
  duration?: number;
  /** Custom header fields are namespaced `x-` too. */
  [custom: `x-${string}`]: unknown;
}

/** Parsed cast. `truncated` is true when the final line was incomplete. */
export interface ParsedCast {
  header: CastHeader;
  events: CastEvent[];
  truncated: boolean;
}

export const EMPTY_BUNDLE: ReelBundle = {
  session: { id: "", name: "", duration_s: 0, started_at: "" },
  lanes: [],
  timeline: [],
  coverage: [],
};
