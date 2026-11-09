// @vitest-environment jsdom
import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { AgentTimeline } from "./AgentTimeline";
import type { ExecCellState, StatusNoticeCellState } from "../../chat/cells/cellTypes";
import { StatusNoticeCell } from "../../chat/cells/StatusNoticeCell";

afterEach(cleanup);

it("shows a terminal tool failure without requiring another disclosure click", () => {
  const cell: ExecCellState = { kind: "exec", id: "failed-command", command: "build", status: "failed", stdoutPreview: [], stderrPreview: ["Build failed"], collapsed: false, createdAt: 1, segment: 1, segmentClosed: true };
  render(<AgentTimeline cells={[cell]} isRunning={false} renderCell={({ key }) => <div key={key}>Build failed</div>} />);
  expect(screen.getByText("Build failed")).toBeTruthy();
});

it("preserves compaction failure wording instead of projecting success", () => {
  const cell: StatusNoticeCellState = { kind: "status_notice", id: "compact-failure", tone: "danger", title: "上下文压缩失败", message: "Provider unavailable", createdAt: 1 };
  render(<AgentTimeline cells={[cell]} renderCell={({ cell: item }) => item.kind === "status_notice"
    ? <StatusNoticeCell cell={item} /> : <div>{item.id}</div>} />);
  expect(screen.getByText("上下文压缩失败")).toBeTruthy();
  expect(screen.getByText("Provider unavailable")).toBeTruthy();
  expect(screen.queryByText("上下文已自动压缩")).toBeNull();
});
