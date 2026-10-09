/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentProcessSummary } from "./AgentProcessSummary";

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("AgentProcessSummary", () => {
  it("updates real elapsed time while running and freezes it at completion", () => {
    vi.useFakeTimers();
    vi.setSystemTime(100_000);
    const props = { processExpanded: true, hasTimelineItems: true, startedAt: 53_000, onToggle: () => undefined };
    const view = render(<AgentProcessSummary {...props} status="running" durationMs={null} />);
    expect(screen.getByText("已处理 47秒")).toBeTruthy();
    expect(view.container.querySelector('[data-running="true"]')).toBeNull();
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText("已处理 48秒")).toBeTruthy();
    view.rerender(<AgentProcessSummary {...props} status="completed" durationMs={48_000} />);
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByText("已处理 48秒")).toBeTruthy();
  });
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

    expect(screen.getByText("已处理 26秒")).toBeTruthy();
    expect(screen.queryByText(/个工具|个失败|输入|输出|推理/)).toBeNull();
  });

  it("waits for a full elapsed second and freezes whole seconds at completion", () => {
    vi.useFakeTimers();
    vi.setSystemTime(100_000);
    const props = { processExpanded: true, hasTimelineItems: true, startedAt: 99_050, onToggle: () => undefined };
    const view = render(<AgentProcessSummary {...props} status="running" durationMs={null} />);
    expect(screen.getByText("处理中")).toBeTruthy();
    expect(screen.queryByText(/不到|0秒|0\.9/)).toBeNull();
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText("已处理 1秒")).toBeTruthy();
    view.rerender(<AgentProcessSummary {...props} status="completed" durationMs={1_950} />);
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByText("已处理 1秒")).toBeTruthy();
    view.rerender(<AgentProcessSummary {...props} status="completed" durationMs={950} />);
    expect(screen.getByText("已处理")).toBeTruthy();
    expect(screen.queryByText(/不到|0秒/)).toBeNull();
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
    expect(screen.getByRole("status", { name: "处理中" })).toBeTruthy();
    const processingStatus = container.querySelector(".agent-loop-process-summary-status");
    expect(processingStatus?.getAttribute("data-running")).toBe("true");
    expect(processingStatus?.querySelector(".agent-loop-process-summary-status-label")?.textContent)
      .toBe("处理中");
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
      .toBe("处理中");
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

    expect(screen.getByRole("status", { name: "处理失败 · 1秒" })).toBeTruthy();
    expect(screen.getByText("处理失败 · 1秒")).toBeTruthy();
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
