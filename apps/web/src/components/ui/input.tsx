import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Input.
 *
 * `font-mono` is opt-in rather than default: most inputs here take a human name,
 * and one takes a ULID. The ULID one passes `className="font-mono"`.
 */
export function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      className={cn(
        "flex h-8 w-full rounded-md border bg-transparent px-2.5 py-1 text-sm",
        "placeholder:text-muted-foreground",
        "focus-visible:border-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "file:border-0 file:bg-transparent file:text-sm file:font-medium",
        className,
      )}
      {...props}
    />
  );
}

export function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      className={cn(
        "flex min-h-16 w-full rounded-md border bg-transparent px-2.5 py-1.5 text-sm",
        "placeholder:text-muted-foreground focus-visible:border-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

export function Label({ className, ...props }: React.ComponentProps<"label">) {
  return (
    <label
      className={cn(
        "text-xs font-medium leading-none text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}
