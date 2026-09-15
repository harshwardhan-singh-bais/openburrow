import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { TONE_CLASSES, TONE_DOT, type StateTone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Badge.
 *
 * The `tone` variant is the reason this exists rather than a plain span: domain
 * state is coloured in exactly one place (`lib/format.ts`), so a lane that is
 * amber in the terminal is amber here. A `tone` that is not a `StateTone` is a
 * type error, which is what stops someone reaching for `text-amber-500`.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-xs font-medium leading-none",
  {
    variants: {
      tone: {
        neutral: "border-border bg-muted text-muted-foreground",
        outline: "border-border text-foreground",
        idle: TONE_CLASSES.idle,
        working: TONE_CLASSES.working,
        blocked: TONE_CLASSES.blocked,
        done: TONE_CLASSES.done,
        failed: TONE_CLASSES.failed,
        cancelled: TONE_CLASSES.cancelled,
        governance: TONE_CLASSES.governance,
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

export interface BadgeProps
  extends React.ComponentProps<"span">,
    VariantProps<typeof badgeVariants> {
  /** Show a coloured dot before the label. */
  dot?: boolean;
}

export function Badge({ className, tone, dot = false, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ tone }), className)} {...props}>
      {dot && tone && tone !== "neutral" && tone !== "outline" ? (
        <span
          aria-hidden
          className={cn("size-1.5 shrink-0 rounded-full", TONE_DOT[tone as StateTone])}
        />
      ) : null}
      {children}
    </span>
  );
}

export { badgeVariants };
