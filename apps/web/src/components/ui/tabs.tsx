"use client";

import * as TabsPrimitive from "@radix-ui/react-tabs";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Tabs.
 *
 * Radix rather than hand-rolled because the keyboard semantics are the whole
 * value: arrow keys move between tabs, `Home`/`End` jump to the ends, and the
 * tab list is a single tab stop. A hand-rolled version gets the mouse right and
 * the keyboard wrong, and the keyboard is what someone reaches for when the
 * board is the only thing on screen.
 */
export const Tabs = TabsPrimitive.Root;

export function TabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn(
        "inline-flex h-8 items-center justify-center rounded-md bg-muted p-0.5 text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-sm px-2.5 py-1 text-xs font-medium transition-colors",
        "data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm",
        "disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

export function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      // Focusable so that a keyboard user lands on the panel after changing tab,
      // rather than having to tab through the whole list again.
      tabIndex={0}
      className={cn("mt-3 focus-visible:outline-none", className)}
      {...props}
    />
  );
}
