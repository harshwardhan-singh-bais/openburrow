import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Card.
 *
 * `CardContent` carries `min-w-0`, which looks like noise and is not. A flex or
 * grid child defaults to `min-width: auto`, so a card containing a long
 * unbreakable string — a repo slug, a ULID, a JSON payload — refuses to shrink
 * and pushes the whole layout wider than the viewport. This is the single most
 * common layout bug in a dashboard and the fix belongs here, once.
 */
export function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "rounded-lg border bg-card text-card-foreground shadow-sm",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("flex flex-col gap-1 border-b px-4 py-3", className)}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: React.ComponentProps<"h3">) {
  return (
    <h3
      className={cn("text-sm font-semibold leading-none tracking-tight", className)}
      {...props}
    />
  );
}

export function CardDescription({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("text-xs text-muted-foreground", className)} {...props} />;
}

export function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("min-w-0 p-4", className)} {...props} />;
}

export function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("flex items-center gap-2 border-t px-4 py-3", className)}
      {...props}
    />
  );
}
