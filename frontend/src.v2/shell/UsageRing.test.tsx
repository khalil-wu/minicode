/* @vitest-environment jsdom */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { UsageRing } from "./UsageRing";

describe("UsageRing authoritative context projection", () => {
  afterEach(() => cleanup());

  it("shows a known empty context as zero percent", () => {
    render(<UsageRing buckets={[]} contextUsage={{ used: 0, limit: 128_000 }} totalBudgetPercent={0} />);

    const meter = screen.getByRole("meter", { name: "Context usage 0%" });
    expect(meter.getAttribute("aria-valuenow")).toBe("0");
    expect(screen.getByText("0%")).toBeTruthy();
  });

  it("does not replace an authoritative context zero with a nonzero token budget", () => {
    render(
      <UsageRing
        buckets={[{ name: "turn", used: 75, limit: 100 }]}
        contextUsage={{ used: 0, limit: 128_000 }}
        totalBudgetPercent={0.75}
      />,
    );

    expect(screen.getByRole("meter", { name: "Context usage 0%" })).toBeTruthy();
    expect(screen.queryByText("75%")).toBeNull();
  });

  it("shows zero from a known budget bucket when context is unavailable", () => {
    render(
      <UsageRing
        buckets={[{ name: "turn", used: 0, limit: 100 }]}
        contextUsage={null}
        totalBudgetPercent={0}
      />,
    );

    expect(screen.getByRole("meter", { name: "Context usage 0%" }).getAttribute("aria-valuenow")).toBe("0");
  });

  it("uses the unknown marker only when neither context nor budget is known", () => {
    render(<UsageRing buckets={[]} contextUsage={null} totalBudgetPercent={0} />);

    const meter = screen.getByRole("meter", { name: "Context usage --" });
    expect(meter.hasAttribute("aria-valuenow")).toBe(false);
    expect(screen.getByText("--")).toBeTruthy();
    expect(meter.parentElement?.getAttribute("title")).toContain("unknown");
  });
});
