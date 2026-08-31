/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type React from "react";
import type { AgentLoopProcessCell } from "../projection/project-turn";
import { AgentTimeline } from "./AgentTimeline";

afterEach(cleanup);

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
  title: "已编辑",
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
  it("groups a contiguous edit and command sequence under one ordered work heading", () => {
    const { container } = render(<AgentTimeline cells={[fileChange("edit"), exec("test", "npm test")]} renderCell={renderCell} />);

    expect(screen.getByText("编辑了文件并运行了命令")).toBeTruthy();
    expect(screen.getByText("编辑了文件并运行了命令").parentElement?.getAttribute("data-group-kind")).toBe("work");
    expect(container.querySelector(".agent-loop-timeline-group-icon svg")).toBeTruthy();
    expect(container.querySelector(".agent-loop-timeline-group-chevron svg")).toBeTruthy();
    expect(screen.queryByText("npm test")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "编辑了文件并运行了命令" }));
    expect(screen.getByText("npm test")).toBeTruthy();
    expect(container.querySelector(".agent-loop-timeline-group-items")?.previousElementSibling)
      .toBe(screen.getByRole("button", { name: "编辑了文件并运行了命令" }));
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
    render(<AgentTimeline cells={[exec("test", "npm test"), notice, fileChange("edit")]} renderCell={renderCell} />);

    expect(screen.getByText("上下文已自动压缩")).toBeTruthy();
    expect(screen.getByText("上下文已自动压缩").parentElement?.getAttribute("data-group-kind")).toBe("context");
    expect(screen.getByText("npm test")).toBeTruthy();
    expect(screen.getByText("activity")).toBeTruthy();
  });

  it("renders one completed work item directly instead of wrapping it in a group", () => {
    render(<AgentTimeline cells={[exec("status", "git status --short")]} renderCell={renderCell} />);

    expect(screen.getByText("git status --short")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "运行了命令" })).toBeNull();
  });

  it("uses the latest live command as the title while keeping all work rows ordered", () => {
    const { container } = render(
      <AgentTimeline
        cells={[
          openExec("old-1", "git status --short"),
          openExec("old-2", "npm test"),
          openExec("latest", "npm run build"),
        ]}
        renderCell={renderCell}
      />,
    );

    const latest = container.querySelector(".agent-loop-timeline-group-live-title");
    expect(latest?.textContent).toBe("运行命令 npm run build");
    expect(screen.getByRole("region", { name: "运行命令 npm run build" })).toBeTruthy();
    expect(screen.getByText("git status --short")).toBeTruthy();
    expect(screen.getByText("npm test")).toBeTruthy();
    expect(container.querySelectorAll(".agent-loop-process-cell")).toHaveLength(3);
  });

  it("uses the latest List action as the title while keeping Read and Search rows visible", () => {
    const { container } = render(
      <AgentTimeline
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

    expect(screen.getByRole("region", { name: "list" })).toBeTruthy();
    expect(screen.getByText("read")).toBeTruthy();
    expect(screen.getByText("search")).toBeTruthy();
    expect(container.querySelector(".agent-loop-timeline-group-live-title")?.textContent).toBe("list");
  });

  it("keeps the live title separate from the ordered tool rows and lets the group collapse", () => {
    const { container } = render(
      <AgentTimeline
        cells={[
          activity("list", "workspaceList"),
          activity("read", "fileRead"),
          openExec("latest", "grep -n foo 1.txt"),
        ]}
        renderCell={({ cell, key, className }) => <div key={key} className={className}>{cell.id}</div>}
      />,
    );

    const region = screen.getByRole("region", { name: "运行命令 grep -n foo 1.txt" });
    const title = screen.getByRole("button", { name: "运行命令 grep -n foo 1.txt" });
    expect(title.parentElement).toBe(region);
    expect(title.getAttribute("aria-expanded")).toBe("true");
    expect([...container.querySelectorAll(".agent-loop-process-cell")].map((node) => node.textContent)).toEqual(["list", "read", "latest"]);

    fireEvent.click(title);
    expect(title.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".agent-loop-timeline-group-items")).toBeNull();

    fireEvent.click(title);
    expect(title.getAttribute("aria-expanded")).toBe("true");
    expect([...container.querySelectorAll(".agent-loop-process-cell")].map((node) => node.textContent)).toEqual(["list", "read", "latest"]);
  });

  it("marks only the newest live row active so its details stay open while history remains collapsible", () => {
    const activeIds: string[] = [];
    render(
      <AgentTimeline
        cells={[openExec("read", "read 1.txt"), openExec("latest", "grep -n foo 1.txt")]}
        renderCell={({ cell, key, isActive }) => {
          if (isActive) activeIds.push(cell.id);
          return <div key={key}>{cell.id}</div>;
        }}
      />,
    );

    expect(activeIds).toEqual(["latest"]);
  });
});
