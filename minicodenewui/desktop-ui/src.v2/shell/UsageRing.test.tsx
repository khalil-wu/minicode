/* @vitest-environment jsdom */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { UsageRing } from "./UsageRing";

describe("UsageRing authoritative context projection", () => {
  afterEach(() => cleanup());

  it("shows a known empty context as zero percent", () => {
    render(<UsageRing buckets={[]} contextUsage={{ used: 0, limit: 128_000 }} totalBudgetPercent={0} />);

    const meter = screen.getByRole("meter", { name: "会话用量 0%" });
    expect(meter.getAttribute("aria-valuenow")).toBe("0");
    expect(screen.getByText("0%", { selector: ".mc-usage-ring-label" })).toBeTruthy();
  });

  it("does not replace an authoritative context zero with a nonzero token budget", () => {
    render(
      <UsageRing
        buckets={[{ name: "turn", used: 75, limit: 100 }]}
        contextUsage={{ used: 0, limit: 128_000 }}
        totalBudgetPercent={0.75}
      />,
    );

    expect(screen.getByRole("meter", { name: "会话用量 0%" })).toBeTruthy();
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

    expect(screen.getByRole("meter", { name: "会话用量 0%" }).getAttribute("aria-valuenow")).toBe("0");
    expect(screen.getByRole("tooltip", { hidden: true }).textContent).not.toContain("任务预算");
  });

  it("uses the unknown marker only when neither context nor budget is known", () => {
    render(<UsageRing buckets={[]} contextUsage={null} totalBudgetPercent={0} />);

    const meter = screen.getByRole("meter", { name: "用量暂无数据" });
    expect(meter.hasAttribute("aria-valuenow")).toBe(false);
    expect(screen.getByText("暂无数据", { selector: ".mc-usage-ring-label" })).toBeTruthy();
    expect(screen.getByRole("tooltip", { hidden: true }).textContent).toContain("用量暂无数据");
  });

  it("keeps the badge gray and projects only backend ledger categories in the colorful hover detail", () => {
    const { container } = render(<UsageRing buckets={[]} totalBudgetPercent={0} contextUsage={{ used: 500, limit: 1000,
      ledger: { schema_version: 1, estimated_tokens: 300, actual_tokens: 500, compaction_count: 0,
        native_attachment_tokens: 0, native_attachment_count: 0, entries: [
          { category: "system_runtime", label: "系统与运行时", estimated_tokens: 100, item_count: 1, source_count: 1, sources: ["system"] },
          { category: "skills", label: "技能", estimated_tokens: 200, item_count: 1, source_count: 1, sources: ["skill"] },
        ] } }} />);
    expect(screen.getByRole("meter", { name: "会话用量 50%" }).getAttribute("style")).toContain("var(--text-secondary)");
    expect(container.querySelector(".mc-usage-ring-label")?.textContent).toBe("50%");
    expect(screen.queryByText("上下文")).toBeNull();
    const tooltip = screen.getByRole("tooltip", { hidden: true });
    expect(tooltip.textContent).toContain("500 / 1.0k tokens");
    expect(tooltip.textContent).toContain("组成 · 后端估算");
    expect(tooltip.textContent).toContain("系统与运行时100 tokens");
    expect(tooltip.textContent).toContain("技能200 tokens");
    expect(container.querySelectorAll(".mc-usage-details-segment")).toHaveLength(2);
    expect(container.querySelectorAll(".mc-usage-details-bar > span")[0].getAttribute("style")).toContain("var(--agent-identity-violet)");
    expect(container.querySelectorAll(".mc-usage-details-bar > span")[1].getAttribute("style")).toContain("var(--agent-identity-rose)");
  });
});
