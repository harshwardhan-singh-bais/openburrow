// @vitest-environment jsdom
import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { StatusBadge, StatusDot } from "@/components/status-badge";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import type { StateTone } from "@/lib/format";

/**
 * The two components every screen is built out of.
 *
 * `StatusBadge` is the only way a state reaches the screen, and its tone comes
 * from `lib/format.ts`, so this suite is where that mapping is checked against a
 * real render rather than against another table. `states.tsx` is where the app
 * promises that "nothing here yet" and "the daemon is unreachable" do not render
 * identically — a blank panel — so the difference between the two is asserted.
 */

const ALL_TONES: StateTone[] = [
  "idle",
  "working",
  "blocked",
  "done",
  "failed",
  "cancelled",
  "governance",
];

function must<T extends Element>(value: T | null, what: string): T {
  if (value === null) throw new Error(`expected ${what} to be rendered`);
  return value;
}

describe("StatusBadge", () => {
  test("renders its label", () => {
    const { container } = render(<StatusBadge tone="working" label="working" />);
    expect(container.textContent).toContain("working");
  });

  test("every tone produces a text class and a dot class", () => {
    // `TONE_TEXT` and `TONE_DOT` are `Record<StateTone, string>`, so the compiler
    // already guarantees every tone has an entry. What it cannot guarantee is that
    // the entry reaches the DOM — a tone whose class is dropped renders a badge
    // with no colour and no error, which is the failure this catches.
    for (const tone of ALL_TONES) {
      const { container, unmount } = render(<StatusBadge tone={tone} label={tone} />);
      const badge = must(container.querySelector("span"), `the ${tone} badge`);
      expect(badge.className, `${tone} has no text colour`).toMatch(/text-(state-|governance)/);
      const dot = must(container.querySelector("span > span"), `the ${tone} dot`);
      expect(dot.className, `${tone} has no dot colour`).toMatch(/bg-(state-|governance)/);
      unmount();
    }
  });

  test("the dot is decorative, because the label is right next to it", () => {
    const { container } = render(<StatusBadge tone="done" label="done" />);
    expect(must(container.querySelector("span > span"), "the dot").getAttribute("aria-hidden")).toBe("true");
  });

  test("pulse is opt-in, and only for states that are actually moving", () => {
    // A pulsing "stopped" badge is motion that means nothing, and motion that
    // means nothing is motion people learn to ignore.
    const still = render(<StatusBadge tone="done" label="done" />);
    expect(still.container.innerHTML).not.toContain("animate-lane-pulse");

    const moving = render(<StatusBadge tone="working" label="working" pulse />);
    expect(moving.container.innerHTML).toContain("animate-lane-pulse");
  });

  test("a caller's className is merged, not replaced", () => {
    const { container } = render(<StatusBadge tone="idle" label="idle" className="ml-2" />);
    const badge = must(container.querySelector("span"), "the badge");
    expect(badge.className).toContain("ml-2");
    expect(badge.className).toContain("text-state-idle");
  });
});

describe("StatusDot", () => {
  test("carries a title and stays out of the accessibility tree", () => {
    const { container } = render(<StatusDot tone="failed" title="crashed" />);
    const dot = must(container.querySelector("span"), "the dot");
    expect(dot.getAttribute("title")).toBe("crashed");
    expect(dot.getAttribute("aria-hidden")).toBe("true");
    expect(dot.className).toContain("bg-state-failed");
  });
});

describe("EmptyState", () => {
  test("renders a title and an optional description", () => {
    const { container } = render(<EmptyState title="No lanes yet" description="Start one to see it here." />);
    expect(container.textContent).toContain("No lanes yet");
    expect(container.textContent).toContain("Start one to see it here.");
  });

  test("omits the description element when there is none", () => {
    const { container } = render(<EmptyState title="No reels" />);
    expect(container.querySelectorAll("p")).toHaveLength(1);
  });

  test("renders an action when given one", () => {
    const { container } = render(<EmptyState title="No reels" action={<button type="button">Export</button>} />);
    expect(container.querySelector("button")?.textContent).toBe("Export");
  });
});

describe("LoadingState", () => {
  test("announces itself politely rather than grabbing focus", () => {
    const { container } = render(<LoadingState />);
    const status = must(container.querySelector("[role=status]"), "the status region");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(container.textContent).toContain("Loading…");
  });

  test("accepts a label", () => {
    const { container } = render(<LoadingState label="Fetching reels…" />);
    expect(container.textContent).toContain("Fetching reels…");
  });
});

describe("ErrorState", () => {
  test("renders the envelope: code, message and hint are all separate", () => {
    // Collapsing these into one paragraph is how a UI says "an error occurred"
    // when the daemon took the trouble to say exactly what was wrong.
    const { container } = render(
      <ErrorState
        error={{ code: "openburrow.daemon.unreachable", message: "no socket", hint: "start the daemon" }}
      />,
    );
    expect(container.textContent).toContain("openburrow.daemon.unreachable");
    expect(container.textContent).toContain("no socket");
    expect(container.textContent).toContain("start the daemon");
  });

  test("an optional title says which panel failed", () => {
    const { container } = render(<ErrorState title="Could not load this session" error={{ message: "session_not_found" }} />);
    expect(container.textContent).toContain("Could not load this session");
  });

  test("context is collapsed detail, and absent when there is none", () => {
    const withContext = render(<ErrorState error={{ message: "bad", context: { parameter: "limit" } }} />);
    const details = must(withContext.container.querySelector("details"), "the context block");
    expect(details.textContent).toContain("limit");

    const without = render(<ErrorState error={{ message: "bad" }} />);
    expect(without.container.querySelector("details")).toBeNull();

    // An empty object is not context either — otherwise every error grows a
    // disclosure triangle that opens onto `{}`.
    const empty = render(<ErrorState error={{ message: "bad", context: {} }} />);
    expect(empty.container.querySelector("details")).toBeNull();
  });

  test("the retry button appears only when there is something to retry", () => {
    const without = render(<ErrorState error={{ message: "bad" }} />);
    expect(without.container.querySelector("button")).toBeNull();

    const onRetry = vi.fn();
    const withRetry = render(<ErrorState error={{ message: "bad" }} onRetry={onRetry} />);
    const button = must(withRetry.container.querySelector("button"), "the retry button");
    fireEvent.click(button);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  test("it is an alert, so a screen reader is told without being asked", () => {
    const { container } = render(<ErrorState error={{ message: "bad" }} />);
    expect(must(container.querySelector("[role=alert]"), "the alert").getAttribute("role")).toBe("alert");
  });
});
