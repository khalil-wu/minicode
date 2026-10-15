/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type React from "react";
import type { AgentLoopProcessCell } from "../projection/project-turn";
import { AgentTimeline } from "./AgentTimeline";
import { StatusNoticeCell } from "../../chat/cells/StatusNoticeCell";
import { LanguageProvider } from "../../i18n";

afterEach(() => { cleanup(); vi.useRealTimers(); });
beforeEach(() => vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("prefers-reduced-motion"), addEventListener() {}, removeEventListener() {} })));

const renderCell = ({ cell, key, className }: { cell: AgentLoopProcessCell; key?: React.Key; className?: string }) => (
  <div key={key} className={className}>
    {cell.kind === "exec" ? cell.command : cell.kind === "thinking" ? cell.content : cell.kind === "status_notice" ? cell.message : cell.kind}
  </div>
);

const exec = (id: string, command: string): AgentLoopProcessCell => ({
  kind: "exec",
  id,
  command,
  status: "success",
  stdoutPreview: [],
  stderrPreview: [],
  collapsed: true,
  createdAt: 1,
  segment: 2,
  segmentClosed: true,
});

const fileChange = (id: string): AgentLoopProcessCell => ({
  kind: "activity",
  id,
  activityKind: "fileChange",
  title: "Edit",
  status: "done",
  collapsed: true,
  startedAt: 1,
  segment: 2,
  segmentClosed: true,
});

const activity = (
  id: string,
  activityKind: "fileRead" | "workspaceList" | "workspaceSearch",
): AgentLoopProcessCell => ({
  kind: "activity",
  id,
  activityKind,
  title: id,
  status: "done",
  collapsed: true,
  startedAt: 1,
  segment: 2,
  segmentClosed: false,
});

const openExec = (id: string, command: string): AgentLoopProcessCell => ({
  ...exec(id, command),
  status: id === "latest" ? "running" : "success",
  segmentClosed: false,
});

describe("AgentTimeline", () => {
  it("keeps a completed screenshot group collapsed when narration moves to the next step", () => {
    const screenshot: AgentLoopProcessCell = { kind: "activity", id: "closed-capture", activityKind: "browser", title: "截图",
      status: "done", collapsed: true, startedAt: 1, segment: 2, segmentClosed: true,
      toolCallRecords: [{ id: "capture", name: "browser_control", args: { action: "screenshot" }, status: "success", startedAt: 1,
        artifactId: "screen", artifactKind: "image", artifactMediaType: "image/png" }],
    };
    const { container } = render(<AgentTimeline cells={[{ ...exec("browse-check", "check"), segment: 2 }, screenshot]} renderCell={renderCell} isRunning />);
    expect(container.querySelector('[data-group-kind="work"]')?.getAttribute("data-group-expanded")).toBe("false");
    expect(screen.queryByText("activity")).toBeNull();
  });

  it("keeps an agent lifecycle row separate from adjacent tools without invented group chrome", () => {
    const cells = [
      { kind: "collaboration", id: "delegate", action: "delegated", status: "failed", entries: [], collapsed: true, createdAt: 1, segment: 2, segmentClosed: true },
      { kind: "activity", id: "check", activityKind: "genericTool", title: "Check agents", status: "done", collapsed: true, startedAt: 1, segment: 2, segmentClosed: true },
    ] as AgentLoopProcessCell[];
    render(<AgentTimeline cells={cells} renderCell={renderCell} />);
    expect(screen.queryByRole("region", { name: "协作 · 工具调用 · 失败" })).toBeNull();
    expect(screen.getByText("collaboration")).toBeTruthy();
    expect(screen.getByText("activity")).toBeTruthy();
    expect(cells[0]).toMatchObject({ kind: "collaboration", status: "failed" });
  });

  it("localizes mixed search, fetch and read group captions while retaining protocol evidence", () => {
    const fetchCell: AgentLoopProcessCell = { kind: "activity", id: "fetch", activityKind: "webSearch", title: "Fetch", status: "failed",
      collapsed: true, startedAt: 1, segment: 2, segmentClosed: true,
      toolCallRecords: [{ id: "fetch-call", name: "web_fetch", status: "failed", args: { url: "https://example.com/Read/Search" } }],
    };
    render(<AgentTimeline cells={[activity("search", "workspaceSearch"), fetchCell, activity("read", "fileRead")]} renderCell={renderCell} />);
    expect(screen.getByRole("region", { name: "已读取文件读取了网页" })).toBeTruthy();
    expect(fetchCell.toolCallRecords?.[0]).toMatchObject({ name: "web_fetch", status: "failed", args: { url: "https://example.com/Read/Search" } });
  });

  it("uses operation captions without a tool-call category and retains failed records", () => {
    vi.useFakeTimers();
    const pendingOutput: AgentLoopProcessCell = { kind: "activity", id: "output", activityKind: "genericTool", title: "读取命令输出",
      status: "done", collapsed: true, startedAt: 1, segment: 2, segmentClosed: true,
      toolCallRecords: [{ id: "output-call", name: "get_command_output", status: "success" }],
    };
    const failedOutput: AgentLoopProcessCell = { ...pendingOutput, status: "failed",
      toolCallRecords: [{ id: "output-call", name: "get_command_output", status: "failed" }],
    };
    const failedRun = { ...exec("failed-test", "npm test"), status: "failed" } as AgentLoopProcessCell;
    const { rerender } = render(<AgentTimeline cells={[failedOutput, activity("read", "fileRead"), fileChange("edit"), failedRun]} renderCell={renderCell} />);
    const summary = screen.getByRole("button", { name: "编辑了文件读取文件运行了命令" });
    expect(summary.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(summary);
    expect(screen.queryByText(/工具调用/)).toBeNull();
    expect(screen.getByText("npm test")).toBeTruthy();
    expect(failedOutput.toolCallRecords?.[0]).toMatchObject({ id: "output-call", name: "get_command_output", status: "failed" });
    expect(failedRun).toMatchObject({ status: "failed", command: "npm test" });
    rerender(<AgentTimeline cells={[pendingOutput, openExec("latest", "npm test")]} renderCell={renderCell} isRunning />);
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByRole("region", { name: "正在运行 npm test" })).toBeTruthy();
    expect(screen.getByText("activity")).toBeTruthy();
    expect(screen.getByText("npm test")).toBeTruthy();
  });

  it("keeps unclassified failures visible without inventing a read failure or generic group", () => {
    const failedTool: AgentLoopProcessCell = { kind: "activity", id: "mcp", activityKind: "genericTool", title: "MCP calendar",
      status: "failed", collapsed: true, startedAt: 1, segment: 2, segmentClosed: true,
    };
    const { rerender } = render(<AgentTimeline cells={[activity("read", "fileRead"), failedTool]} renderCell={renderCell} />);
    expect(screen.queryByRole("region")).toBeNull();
    expect(screen.getAllByText("activity")).toHaveLength(2);
    expect(screen.queryByText("查看失败")).toBeNull();
    rerender(<AgentTimeline cells={[failedTool, { ...failedTool, id: "mcp-two" }]} renderCell={renderCell} />);
    expect(screen.queryByRole("region")).toBeNull();
    expect(screen.getAllByText("activity")).toHaveLength(2);
  });
  it("keeps an unclassified successful action directly visible between readable groups", () => {
    const unknown: AgentLoopProcessCell = { kind: "activity", id: "custom", activityKind: "genericTool", title: "MCP calendar",
      status: "done", collapsed: true, startedAt: 1, segment: 2, segmentClosed: true };
    const { container } = render(<AgentTimeline cells={[activity("one", "fileRead"), activity("two", "fileRead"), unknown]} renderCell={({ cell, key }) =>
      <div key={key}>{cell.kind === "activity" ? cell.title : cell.id}</div>} />);
    expect(screen.getByText("MCP calendar")).toBeTruthy();
    expect(screen.getByText("MCP calendar").closest(".agent-loop-timeline-group-work")).toBeNull();
    expect(container.querySelectorAll(".agent-loop-timeline-group-work")).toHaveLength(1);
  });
  it("preserves a user's collapse choice when a live group closes", () => {
    const { rerender } = render(<AgentTimeline cells={[openExec("one", "npm test"), openExec("latest", "npm run build")]} renderCell={renderCell} isRunning />);
    fireEvent.click(screen.getByRole("button", { name: "正在运行 npm run build" }));
    fireEvent.click(screen.getByRole("button", { name: "正在运行 npm run build" }));
    rerender(<AgentTimeline cells={[exec("one", "npm test"), exec("latest", "npm run build")]} renderCell={renderCell} />);
    expect(screen.getByRole("button", { name: "运行了命令" }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("npm test")).toBeNull();
  });

  it("preserves an explicitly expanded group when it closes", () => {
    const { rerender } = render(<AgentTimeline cells={[openExec("one", "npm test"), openExec("latest", "npm run build")]} renderCell={renderCell} isRunning />);
    const toggle = screen.getByRole("button", { name: "正在运行 npm run build" });
    fireEvent.click(toggle);
    rerender(<AgentTimeline cells={[exec("one", "npm test"), exec("latest", "npm run build")]} renderCell={renderCell} />);
    expect(screen.getByRole("button", { name: "运行了命令" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("npm test")).toBeTruthy();
  });

  it("expands groups only when the caller explicitly requests full evidence", () => {
    const { rerender } = render(<AgentTimeline cells={[exec("one", "npm test"), exec("two", "npm run build")]} renderCell={renderCell} expandWorkGroups />);
    expect(screen.getByText("npm test")).toBeTruthy();
    rerender(<AgentTimeline cells={[exec("one", "npm test"), { ...exec("two", "npm run build"), status: "failed" } as AgentLoopProcessCell]} renderCell={renderCell} showAllOpenWork />);
    expect(screen.getByText("npm run build")).toBeTruthy();
  });

  it("groups a contiguous edit and command sequence under one ordered work heading", () => {
    const { container } = render(<AgentTimeline cells={[fileChange("edit"), exec("test", "npm test")]} renderCell={renderCell} />);

    expect(screen.getByText("编辑了文件运行了命令")).toBeTruthy();
    expect(screen.getByText("编辑了文件运行了命令").parentElement?.getAttribute("data-group-kind")).toBe("work");
    expect(container.querySelector(".agent-loop-timeline-group-icon svg")).toBeTruthy();
    expect(container.querySelector(".agent-loop-timeline-group-chevron svg")).toBeTruthy();
    expect(screen.queryByText("npm test")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "编辑了文件运行了命令" }));
    expect(screen.getByText("npm test")).toBeTruthy();
    expect(container.querySelector(".agent-loop-timeline-group-disclosure")?.previousElementSibling)
      .toBe(screen.getByRole("button", { name: "编辑了文件运行了命令" }));
  });

  it("summarizes the actual read and run sequence with a book icon and preserves row order", () => {
    const { container } = render(<AgentTimeline cells={[activity("read", "fileRead"), exec("run", "rg -n process frontend")]} renderCell={({ cell, key }) =>
      <div key={key}>{cell.id}</div>} />);
    const summary = screen.getByRole("button", { name: "已读取文件运行了命令" });
    expect(summary.querySelector(".lucide-book-open-text")).toBeTruthy();
    expect(summary.querySelector(".lucide-terminal-square")).toBeNull();
    expect(screen.queryByText("read")).toBeNull();
    fireEvent.click(summary);
    expect([...container.querySelectorAll(".agent-loop-timeline-group-items > div")].map((node) => node.textContent)).toEqual(["read", "run"]);
  });

  it("uses the Source singular file caption for repeated edits to one actual path without inventing Chinese separators", () => {
    const edits: AgentLoopProcessCell[] = ["first", "second"].map(id => ({ ...fileChange(id), kind: "activity",
      toolCallRecords: [{ id, name: "edit_file", args: { file_path: "actual.ts" }, status: "success", startedAt: 1,
        diff: { plus: 1, minus: 1, files: [{ path: "actual.ts", plus: 1, minus: 1 }] } }] }));
    const cells = [...edits, activity("read", "fileRead"), exec("run", "npm test")];
    const view = render(<LanguageProvider locale="zh-CN"><AgentTimeline cells={cells}
      renderCell={({ cell, key }) => <div key={key}>{cell.id}</div>} /></LanguageProvider>);
    const heading = screen.getByRole("button", { name: "编辑了一个文件读取文件运行了命令" });
    fireEvent.click(heading);
    expect([...view.container.querySelectorAll(".agent-loop-timeline-group-items > div")].map(node => node.textContent))
      .toEqual(["first", "second", "read", "run"]);
    view.rerender(<LanguageProvider locale="en-US"><AgentTimeline cells={cells}
      renderCell={({ cell, key }) => <div key={key}>{cell.id}</div>} /></LanguageProvider>);
    expect(screen.getByRole("button", { name: "Edited a file, read files, ran a command" }).getAttribute("aria-expanded")).toBe("true");
  });

  it("uses the Source plural caption for one genuine patch containing two files", () => {
    const patch: AgentLoopProcessCell = { ...fileChange("actual-patch"), kind: "activity",
      toolCallRecords: [{ id: "actual-patch", name: "apply_patch", args: { patch: "source patch" }, status: "success", startedAt: 1,
        diff: { plus: 2, minus: 0, files: [{ path: "one.ts", plus: 1, minus: 0 }, { path: "two.ts", plus: 1, minus: 0 }] } }] };
    const cells = [patch, exec("one-command", "npm test"), exec("second-command", "npm build")];
    const view = render(<LanguageProvider locale="zh-CN"><AgentTimeline cells={cells} renderCell={renderCell} /></LanguageProvider>);
    expect(screen.getByRole("button", { name: "编辑了文件运行了命令" })).toBeTruthy();
    view.rerender(<LanguageProvider locale="en-US"><AgentTimeline cells={cells} renderCell={renderCell} /></LanguageProvider>);
    expect(screen.getByRole("button", { name: "Edited files, ran commands" })).toBeTruthy();
  });

  it("transitions from pending to running to completed without inventing completion or replacing disclosure state", () => {
    const commands = [exec("one", "rg -n process frontend"), exec("two", "npm test")];
    const pending = commands.map((cell) => ({ ...cell, status: "pending", segmentClosed: false })) as AgentLoopProcessCell[];
    const { rerender } = render(<AgentTimeline cells={pending} renderCell={renderCell} isRunning />);
    fireEvent.click(screen.getByRole("button", { name: "准备运行命令" }));
    expect(screen.getByRole("region", { name: "准备运行命令" }).getAttribute("data-processing")).toBe("false");
    const running = commands.map((cell) => ({ ...cell, status: "running", segmentClosed: false })) as AgentLoopProcessCell[];
    rerender(<AgentTimeline cells={running} renderCell={renderCell} isRunning />);
    const active = screen.getByRole("button", { name: "准备运行命令" });
    expect(active.getAttribute("aria-expanded")).toBe("true");
    expect(active.parentElement?.getAttribute("data-processing")).toBe("true");
    fireEvent.click(active);
    expect(active.parentElement?.getAttribute("data-processing")).toBe("true");
    fireEvent.click(active);
    expect(screen.queryByText("运行了命令")).toBeNull();
    rerender(<AgentTimeline cells={commands} renderCell={renderCell} />);
    expect(screen.getByRole("button", { name: "运行了命令" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("region", { name: "运行了命令" }).getAttribute("data-processing")).toBe("false");
    expect(screen.getByText("npm test")).toBeTruthy();
  });

  it("fades only a real scroll overflow and clears that fade when the reader reaches the bottom", () => {
    const height = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(100);
    const scrollHeight = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(200);
    try {
      const { container } = render(<AgentTimeline cells={[exec("one", "rg process"), exec("two", "npm test")]} renderCell={renderCell} expandWorkGroups />);
      const list = container.querySelector<HTMLElement>(".agent-loop-timeline-group-items")!;
      expect(list.getAttribute("data-overflow-bottom")).toBe("true");
      list.scrollTop = 100;
      fireEvent.scroll(list);
      expect(list.getAttribute("data-overflow-bottom")).toBe("false");
    } finally {
      height.mockRestore();
      scrollHeight.mockRestore();
    }
  });

  it("keeps compaction as its own process item", () => {
    const notice: AgentLoopProcessCell = {
      kind: "status_notice",
      id: "compact",
      tone: "info",
      title: "上下文已自动压缩",
      message: "摘要已保存",
      createdAt: 2,
    };
    render(<AgentTimeline cells={[exec("test", "npm test"), notice, fileChange("edit")]} renderCell={(args) => args.cell.kind === "status_notice"
      ? <StatusNoticeCell cell={args.cell} /> : renderCell(args)} />);

    expect(screen.getAllByText("上下文已自动压缩")).toHaveLength(1);
    expect(screen.getByText("上下文已自动压缩").closest(".agent-loop-timeline-group")).toBeNull();
    expect(screen.getByText("摘要已保存")).toBeTruthy();
    expect(screen.getByText("npm test")).toBeTruthy();
    expect(screen.getByText("activity")).toBeTruthy();
  });

  it("renders one completed work item directly instead of wrapping it in a group", () => {
    render(<AgentTimeline cells={[exec("status", "git status --short")]} renderCell={renderCell} />);

    expect(screen.getByText("git status --short")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "运行了命令" })).toBeNull();
  });

  it("keeps the actual command summary stable when another command starts, with ordered work rows", () => {
    const { container } = render(
      <AgentTimeline
        isRunning
        cells={[
          openExec("old-1", "git status --short"),
          openExec("old-2", "npm test"),
          openExec("latest", "npm run build"),
        ]}
        renderCell={renderCell}
      />,
    );

    const latest = container.querySelector(".agent-loop-timeline-group-live-title");
    expect(latest?.textContent).toBe("正在运行 npm run build");
    expect(screen.getByRole("region", { name: "正在运行 npm run build" })).toBeTruthy();
    expect(screen.queryByText("npm test")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "正在运行 npm run build" }));
    expect(screen.getByText("git status --short")).toBeTruthy();
    expect(screen.getByText("npm test")).toBeTruthy();
    expect(container.querySelectorAll(".agent-loop-process-cell")).toHaveLength(3);
  });

  it("keeps completed Read, Search and List rows static while the turn is still running", () => {
    const { container } = render(
      <AgentTimeline
        isRunning
        cells={[
          activity("read", "fileRead"),
          activity("search", "workspaceSearch"),
          activity("list", "workspaceList"),
        ]}
        renderCell={({ cell, key, className }) => (
          <div key={key} className={className}>{cell.id}</div>
        )}
      />,
    );

    expect(screen.getByRole("region", { name: "已读取文件" })).toBeTruthy();
    expect(screen.queryByText("read")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "已读取文件" }));
    expect(screen.getByText("read")).toBeTruthy();
    expect(screen.getByText("search")).toBeTruthy();
    expect(container.querySelector(".agent-loop-timeline-group-live-title")).toBeNull();
  });

  it("keeps the live title separate from the ordered tool rows and lets the group collapse", () => {
    const { container } = render(
      <AgentTimeline
        isRunning
        cells={[
          activity("list", "workspaceList"),
          activity("read", "fileRead"),
          openExec("latest", "grep -n foo 1.txt"),
        ]}
        renderCell={({ cell, key, className }) => <div key={key} className={className}>{cell.id}</div>}
      />,
    );

    const region = screen.getByRole("region", { name: "正在运行 grep -n foo 1.txt" });
    const title = screen.getByRole("button", { name: "正在运行 grep -n foo 1.txt" });
    expect(title.parentElement).toBe(region);
    expect(title.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".agent-loop-timeline-group-items")).toBeNull();

    fireEvent.click(title);
    expect(title.getAttribute("aria-expanded")).toBe("true");
    expect([...container.querySelectorAll(".agent-loop-process-cell")].map((node) => node.textContent)).toEqual(["list", "read", "latest"]);
  });

  it("leaves execution status to each row instead of overriding the last row in an open group", () => {
    const activeIds: string[] = [];
    render(
      <AgentTimeline
        isRunning
        cells={[openExec("read", "read 1.txt"), openExec("latest", "grep -n foo 1.txt")]}
        renderCell={({ cell, key, isActive }) => {
          if (isActive) activeIds.push(cell.id);
          return <div key={key}>{cell.id}</div>;
        }}
      />,
    );

    expect(activeIds).toEqual([]);
  });

  it("folds settled work groups even when persisted segments lack a closing marker", () => {
    const cells = [activity("read-one", "fileRead"), activity("read-two", "fileRead")];
    const { container, rerender } = render(<AgentTimeline cells={cells} renderCell={renderCell} isRunning />);
    expect(container.querySelectorAll(".agent-loop-process-cell")).toHaveLength(0);
    rerender(<AgentTimeline cells={cells} renderCell={renderCell} isRunning={false} />);
    const toggle = screen.getByRole("button", { name: "已读取文件" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelectorAll(".agent-loop-process-cell")).toHaveLength(0);
    fireEvent.click(toggle);
    expect(container.querySelectorAll(".agent-loop-process-cell")).toHaveLength(2);
  });

  it("keeps completed reasoning summaries while hiding completed raw reasoning", () => {
    const raw: AgentLoopProcessCell = {
      kind: "thinking",
      id: "raw-reasoning",
      content: "Temporary detailed reasoning",
      source: "provider",
      providerReasoningType: "reasoning_content",
      isStreaming: false,
      createdAt: 1,
    };
    const summary: AgentLoopProcessCell = {
      kind: "thinking",
      id: "reasoning-summary",
      content: "已核对提交链路并定位重复请求。",
      source: "provider",
      providerReasoningType: "reasoning_summary_text",
      isStreaming: false,
      createdAt: 2,
    };

    render(<AgentTimeline cells={[raw, summary]} renderCell={renderCell} />);

    expect(screen.queryByText("Temporary detailed reasoning")).toBeNull();
    expect(screen.getByText("已核对提交链路并定位重复请求。")).toBeTruthy();
  });

  it("continues showing raw reasoning only while it is streaming", () => {
    const raw: AgentLoopProcessCell = {
      kind: "thinking",
      id: "raw-reasoning-live",
      content: "Live detailed reasoning",
      source: "provider",
      providerReasoningType: "reasoning_content",
      isStreaming: true,
      createdAt: 1,
    };

    render(<AgentTimeline cells={[raw]} renderCell={renderCell} />);

    expect(screen.getByText("Live detailed reasoning")).toBeTruthy();
  });
});


it("groups a long completed command segment with linear cell inspection", () => {
  const count = 1024;
  let inspections = 0;
  const cells = Array.from({ length: count }, (_, index) => new Proxy(exec(String(index), `command ${index}`), {
    get(target, property, receiver) {
      if (property === "kind") inspections++;
      return Reflect.get(target, property, receiver);
    },
  }));
  const { container } = render(<AgentTimeline cells={cells} renderCell={renderCell} isRunning={false} />);
  const groups = container.querySelectorAll('section[data-group-kind="work"]');
  expect(groups).toHaveLength(1);
  expect(inspections).toBeLessThan(count * 100);
  // All loaded operations belong to this one scrollable group.
  const disclosure = groups[0].querySelector('button[data-group-kind="work"]')!;
  expect(disclosure.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText("command 1023")).toBeNull();
  fireEvent.click(disclosure);
  expect(screen.getByText("command 1023")).toBeTruthy();
  expect(screen.getByText("command 984")).toBeTruthy();
  expect(screen.getByText("command 0")).toBeTruthy();
  expect(screen.getByText("command 983")).toBeTruthy();
  expect(container.querySelectorAll(".agent-loop-process-cell")).toHaveLength(count);
  expect(screen.queryByRole("button", { name: /显示更早|加载更多/ })).toBeNull();
});

it("retains every loaded process group without a synthetic earlier-process control", () => {
  const cells = Array.from({ length: 65 }, (_, index) => ({ ...exec(String(index), `command ${index}`), segment: index }));
  const { container } = render(<AgentTimeline cells={cells} renderCell={renderCell} />);
  expect(container.querySelectorAll(".agent-loop-process-cell")).toHaveLength(65);
  expect(screen.getByText("command 0")).toBeTruthy();
  expect(screen.getByText("command 64")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /显示更早|加载更多/ })).toBeNull();
});

it("renders real notices directly without a generic status group", () => {
  const cells: AgentLoopProcessCell[] = [
    { kind: "status_notice", id: "one", title: "Provider实际错误", tone: "danger", message: "HTTP 400", createdAt: 1 },
    { kind: "status_notice", id: "two", title: "真实任务中断", tone: "warning", message: "用户取消", createdAt: 2 },
  ];
  const { container } = render(<AgentTimeline cells={cells} renderCell={renderCell} />);
  expect(screen.getByText("HTTP 400")).toBeTruthy();
  expect(screen.getByText("用户取消")).toBeTruthy();
  expect(screen.queryByText("状态")).toBeNull();
  expect(container.querySelector(".agent-loop-timeline-group-notice")).toBeNull();
});

it("uses the retained reasoning's own first line for its disclosure", () => {
  render(<AgentTimeline cells={[{ kind: "thinking", id: "legacy", source: "reasoning", collapsible: true,
    content: "实际保留的首行\n实际保留的后续内容", createdAt: 1 }]} renderCell={renderCell} />);
  fireEvent.click(screen.getByRole("button", { name: "实际保留的首行" }));
  expect(screen.getByText("实际保留的首行 实际保留的后续内容")).toBeTruthy();
  expect(screen.queryByText("思考过程")).toBeNull();
});

it("preserves the reading position when a real earlier tool page is prepended", () => {
  const height = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(100);
  const scrollHeight = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function () {
    return (this as HTMLElement).querySelectorAll(".agent-loop-process-cell").length * 30;
  });
  try {
    const cells = Array.from({ length: 6 }, (_, index) => exec(String(index), `command ${index}`));
    const view = render(<AgentTimeline cells={cells.slice(2)} renderCell={renderCell} expandWorkGroups />);
    const list = view.container.querySelector<HTMLElement>(".agent-loop-timeline-group-items")!;
    const retained = screen.getByText("command 2");
    list.scrollTop = 20;
    fireEvent.scroll(list);
    view.rerender(<AgentTimeline cells={cells} renderCell={renderCell} expandWorkGroups />);
    expect(list.scrollTop).toBe(80);
    expect(screen.getByText("command 2")).toBe(retained);
    expect(screen.getByText("command 0")).toBeTruthy();
  } finally {
    height.mockRestore();
    scrollHeight.mockRestore();
  }
});
