/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { LanguageProvider } from "../i18n";
import { ExecCell } from "./cells/ExecCell";
import { ActivityCell } from "./cells/ActivityCell";
import { AgentTimeline } from "../agent-loop/components/AgentTimeline";
import type { ActivityCellState, ExecCellState } from "./cells/cellTypes";
afterEach(cleanup);

it("changes command UI language without changing the real log or the reader's open detail", () => {
  const cell: ExecCellState = { kind: "exec", id: "original-command", command: "print(真实文件名)", status: "failed", exitCode: 7,
    stdoutPreview: ["真实程序输出 output_bytes: actual bytes"], stderrPreview: ["实际原始报错"], collapsed: true, createdAt: 1 };
  const view = render(<LanguageProvider locale="zh-CN"><ExecCell cell={cell} /></LanguageProvider>);
  fireEvent.click(screen.getByRole("button", { name: /^展开命令详情/ }));
  view.rerender(<LanguageProvider locale="en-US"><ExecCell cell={cell} /></LanguageProvider>);
  expect(screen.getByRole("region", { name: "Command output" })).toBeTruthy();
  expect(screen.getByRole("button", { name: /^Collapse command details/ }).getAttribute("aria-expanded")).toBe("true");
  expect(view.container.querySelector(".exec-cell-output-stdout")?.textContent).toBe("真实程序输出 output_bytes: actual bytes");
  expect(view.container.querySelector(".exec-cell-output-stderr")?.textContent).toBe("实际原始报错");
  expect(view.container.textContent).not.toContain("Failed");
  expect(screen.getByText("Exit code 7")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Copy command" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Copy command output" })).toBeTruthy();
});

it("localizes a real VM operation while retaining the original failure text and neutral appearance", () => {
  const cell: ActivityCellState = { kind: "activity", id: "vm-failure", activityKind: "genericTool", title: "运行", status: "failed", collapsed: true,
    toolCallRecords: [{ id: "vm-failure", name: "tool_exec", args: { code: "throw new Error('原始内容');" }, status: "failed",
      outputPreview: "Error: 原始内容", startedAt: 1, finishedAt: 2 }] };
  const view = render(<LanguageProvider locale="en-US"><ActivityCell cell={cell} /></LanguageProvider>);
  expect(screen.getByText("Run")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Expand activity details" }));
  expect(view.container.textContent).toContain("Error: 原始内容");
  expect(view.container.textContent).not.toContain("Failed");
});

it("updates the combined activity caption in place and retains its manually expanded records", () => {
  const cells = [{ kind: "activity" as const, id: "read", activityKind: "fileRead" as const, title: "backend title", status: "done" as const, collapsed: true, startedAt: 1, segment: 1 },
    { kind: "exec" as const, id: "exec", command: "真实命令", status: "success" as const, stdoutPreview: [], stderrPreview: [], collapsed: true, createdAt: 2, segment: 1 }];
  const timeline = <AgentTimeline cells={cells} renderCell={({ cell, key }) => <div key={key}>{cell.id}</div>} />;
  const view = render(<LanguageProvider locale="zh-CN">{timeline}</LanguageProvider>);
  fireEvent.click(screen.getByRole("button", { name: "已读取文件运行了命令" }));
  view.rerender(<LanguageProvider locale="en-US">{timeline}</LanguageProvider>);
  expect(screen.getByRole("button", { name: "Read files, ran a command" }).getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText("read")).toBeTruthy();
  expect(screen.getByText("exec")).toBeTruthy();
});
