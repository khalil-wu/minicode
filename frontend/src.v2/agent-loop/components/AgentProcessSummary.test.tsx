/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentProcessSummary } from "./AgentProcessSummary";

afterEach(cleanup);

describe("AgentProcessSummary", () => {
  it("shows only completed state and elapsed seconds in the settled heading", () => {
    render(
      <AgentProcessSummary
        status="completed"
        processExpanded={false}
        hasTimelineItems
        durationMs={26_000}
        onToggle={() => undefined}
      />,
    );

    expect(screen.getByText("已处理 26 秒")).toBeTruthy();
    expect(screen.queryByText(/个工具|个失败|输入|输出|推理/)).toBeNull();
  });

  it("shows only the animated processing status when no activity has arrived", () => {
    const onToggle = vi.fn();
    const { container } = render(
      <AgentProcessSummary
        status="running"
        processExpanded={false}
        hasTimelineItems={false}
        durationMs={null}
        onToggle={onToggle}
      />,
    );

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("status", { name: "正在处理" })).toBeTruthy();
    const processingStatus = container.querySelector(".agent-loop-process-summary-status");
    expect(processingStatus?.getAttribute("data-running")).toBe("true");
    expect(processingStatus?.querySelector(".agent-loop-process-summary-status-label")?.textContent)
      .toBe("正在处理");
    expect(processingStatus?.querySelector(".agent-loop-process-summary-status-sheen"))
      .toBeNull();
    expect(container.querySelector(".agent-loop-thinking-spinner")).toBeNull();
    expect(container.querySelector(".agent-loop-process-summary-icon")).toBeNull();
    expect(screen.queryByText("正在连接模型")).toBeNull();
    expect(screen.queryByText("模型生成中")).toBeNull();
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("does not offer process collapse until a complete final answer exists", () => {
    const onToggle = vi.fn();
    render(
      <AgentProcessSummary
        status="running"
        processExpanded={false}
        hasTimelineItems
        durationMs={null}
        onToggle={onToggle}
      />,
    );

    expect(screen.queryByRole("button", { name: "展开处理步骤" })).toBeNull();
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("keeps the running heading compact above an expanded authoritative timeline", () => {
    const { container } = render(
      <AgentProcessSummary
        status="running"
        processExpanded
        hasTimelineItems
        durationMs={null}
        onToggle={() => undefined}
      />,
    );

    expect(container.querySelector(".agent-loop-process-summary-status-label")?.textContent)
      .toBe("正在处理");
    expect(screen.queryByText(/个工具|个失败/)).toBeNull();
  });

  it("keeps non-success terminal states visible", () => {
    render(
      <AgentProcessSummary
        status="failed"
        processExpanded={false}
        hasTimelineItems={false}
        durationMs={1_500}
        onToggle={() => undefined}
      />,
    );

    expect(screen.getByRole("status", { name: "出错 1.5 秒" })).toBeTruthy();
    expect(screen.getByText("出错 1.5 秒")).toBeTruthy();
  });

  it("keeps the concrete failure visible even when work details are collapsible", () => {
    render(
      <AgentProcessSummary
        status="failed"
        processExpanded={false}
        hasTimelineItems
        durationMs={null}
        failureMessage="RuntimeError: 子任务未完成"
        onToggle={() => undefined}
      />,
    );

    expect(screen.getByText("RuntimeError: 子任务未完成")).toBeTruthy();
  });
});
