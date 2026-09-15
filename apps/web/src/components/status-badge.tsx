import { TONE_DOT, type StateTone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * A state, rendered consistently.
 *
 * Every status in the app goes through this component. That is the point: the
 * tone comes from `lib/format.ts`, which mirrors the Python CLI's palette, so
 * "blocked is amber" is a fact about the codebase rather than a convention
 * someone has to remember in eleven components.
 *
 * `pulse` is opt-in and only used for states that are actively doing something.
 * A pulsing "stopped" badge would be motion that means nothing, and motion that
 * means nothing is motion people learn to ignore.
 */

export interface StatusBadgeProps {
  tone: StateTone;
  label: string;
  /** Adds a slow pulse. Only for genuinely in-progress states. */
  pulse?: boolean;
  className?: string;
}

const TONE_TEXT: Record<StateTone, string> = {
  idle: "text-state-idle",
  working: "text-state-working",
  blocked: "text-state-blocked",
  done: "text-state-done",
  failed: "text-state-failed",
  cancelled: "text-state-cancelled",
  governance: "text-governance",
};

export function StatusBadge({ tone, label, pulse = false, className }: StatusBadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        TONE_TEXT[tone],
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn("size-1.5 shrink-0 rounded-full", TONE_DOT[tone], pulse && "animate-lane-pulse")}
      />
      {label}
    </span>
  );
}

/**
 * A tiny coloured dot with no label, for dense rows.
 *
 * Carries a `title` rather than an `aria-label`, because the label is always
 * rendered adjacent in the row — a screen reader announcing it twice is worse
 * than not announcing the dot at all.
 */
export function StatusDot({ tone, title }: { tone: StateTone; title: string }) {
  return (
    <span
      aria-hidden="true"
      title={title}
      className={cn("inline-block size-2 shrink-0 rounded-full", TONE_DOT[tone])}
    />
  );
}
