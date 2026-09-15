"use client";

import { useMemo } from "react";

import { sgrSpans, type LaneTrack } from "@/lib/reel";
import { cn } from "@/lib/utils";

/**
 * One lane's terminal output at a point in time.
 *
 * `sgrSpans` is not a terminal emulator and does not pretend to be — it handles
 * SGR (colour, weight, underline) and drops cursor movement, because the cast
 * already records what the screen looked like line by line. Re-implementing a
 * VT100 to arrive at the same output would be a lot of code for no difference.
 *
 * What matters here is the failure mode: anything the renderer does not
 * understand is drawn as a **visible marker** rather than swallowed. A renderer
 * that silently drops what it cannot parse makes a corrupt transcript look
 * clean, and a clean-looking transcript is the whole product.
 *
 * The text is memoised on the text itself, so scrubbing within one output event
 * does not re-parse the same string on every animation frame.
 */

export interface TranscriptViewProps {
  track: LaneTrack;
  text: string;
  /** True when the track was clipped to `MAX_RENDER_CHARS`. */
  clipped?: boolean;
  /** Rendered when there is no output yet at this time. */
  placeholder?: string;
  className?: string;
}

export function TranscriptView({
  track,
  text,
  clipped = false,
  placeholder = "— no output yet —",
  className,
}: TranscriptViewProps) {
  const spans = useMemo(() => sgrSpans(text), [text]);

  return (
    <pre
      className={cn(
        "scrollbar-slim transcript h-full overflow-auto rounded border border-border bg-background p-3 text-[11px] leading-relaxed",
        className,
      )}
      // The transcript is the record, not an interface. Selecting it is how
      // someone quotes it, so text selection stays enabled.
      aria-label={`Terminal output for ${track.lane.title}`}
    >
      {text.length === 0 ? (
        <span className="text-muted-foreground">{placeholder}</span>
      ) : (
        <>
          {clipped ? (
            <span className="mb-2 block rounded border border-state-blocked/30 bg-state-blocked/10 px-2 py-1 text-state-blocked">
              Output clipped to the last 60 000 characters. The full recording is in the{" "}
              <span className="font-mono">.cast</span> file.
            </span>
          ) : null}
          {spans.map((span, index) => {
            if (span.unsupported) {
              return (
                <span
                  key={index}
                  title={span.unsupported}
                  className="rounded bg-state-blocked/20 text-state-blocked"
                >
                  {span.text}
                </span>
              );
            }
            return (
              <span
                key={index}
                style={{
                  ...(span.color ? { color: span.color } : {}),
                  ...(span.background ? { backgroundColor: span.background } : {}),
                  fontWeight: span.bold ? 600 : undefined,
                  fontStyle: span.italic ? "italic" : undefined,
                  textDecoration: span.underline ? "underline" : undefined,
                  opacity: span.dim ? 0.65 : undefined,
                }}
              >
                {span.text}
              </span>
            );
          })}
        </>
      )}
    </pre>
  );
}
