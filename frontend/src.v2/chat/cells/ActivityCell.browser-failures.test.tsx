// @vitest-environment jsdom
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../../stores";
import { ActivityCell } from "./ActivityCell";
import type { ActivityCellState } from "./cellTypes";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";

const target = "http://127.0.0.1:55494/preview-test/index.html";
const raw = "Browser navigation to a local, private, or unresolved network target is blocked unless it belongs to the active conversation preview. Network target resolves to a local or private address";
const record: ToolCallRecord = {
  id: "code_browser-fixture", name: "browser_control", args: { action: "navigate", url: target },
  status: "blocked", errorKind: "network_policy", userSummary: "目标未关联到本会话的运行中预览。", developerDetail: raw,
  outputPreview: raw, startedAt: 1000, durationMs: 292,
  callSource: { kind: "code_mode", cell_id: "cell_fixture", parent_call_id: "call_parent_fixture", runtime_call_id: "2" },
};
const cell: ActivityCellState = { kind: "activity", id: "browser-fixture", activityKind: "browser", title: "Browser", status: "failed", collapsed: true, toolCallRecords: [record], startedAt: 1000 };
beforeEach(() => useAppStore.setState({ conversationId: "owner-A", workingDirectory: "C:/projects/demo", previewLaunchProcesses: [], previewVerification: null, conversationWorkbenchStates: {} }));
afterEach(cleanup);

describe("browser failure projection", () => {
  it("keeps one action row and discloses the original diagnostic without generating recovery instructions", () => {
    const view = render(<ActivityCell cell={cell} conversationId="owner-A" workspaceRoot="C:/projects/demo" />);
    const row = view.container.querySelector(".activity-cell-main-button")!;
    expect(row.textContent).toContain("Navigate");
    expect(row.textContent).toContain(target);
    expect(row.textContent).toContain("已阻止");
    expect(view.container.textContent).not.toContain("浏览器操作失败");
    expect(view.container.textContent).not.toContain("下一步");
    expect(view.container.querySelector(".activity-cell-error-detail")).toBeNull();
    expect(view.container.textContent).not.toContain(raw);
    fireEvent.click(row);
    const output = view.getByLabelText("操作结果");
    expect(output.textContent).toContain(raw);
    expect(output.tagName).toBe("DIV");
    expect(view.container.textContent).not.toContain("cell_fixture");
    expect(view.container.textContent).not.toContain("call_parent_fixture");
    expect(record.callSource?.cell_id).toBe("cell_fixture");
  });

  it("does not turn another conversation's preview state into browser failure prose", () => {
    useAppStore.setState({ previewLaunchProcesses: [{ id: "p-A", name: "dev", command: "node", cwd: "C:/projects/demo", port: 55494, url: target, pid: 123, status: "ready" }] });
    const view = render(<ActivityCell cell={cell} conversationId="owner-B" />);
    expect(view.container.textContent).not.toContain("本会话预览");
    expect(view.container.textContent).not.toContain("下一步");
    fireEvent.click(view.getByRole("button", { name: "展开活动详情" }));
    expect(view.getByLabelText("操作结果").textContent).toContain(raw);
  });

  it("keeps a real aborted navigation failed and exposes its precise error on disclosure", () => {
    const error = "ERR_ABORTED (-3) loading 'https://www.google.com.hk/'";
    const failed = { ...record, status: "failed" as const, args: { action: "navigate", url: "https://www.google.com/" },
      outputPreview: "工具执行失败。", userSummary: "工具执行失败。", developerDetail: error };
    const view = render(<ActivityCell cell={{ ...cell, toolCallRecords: [failed] }} />);
    expect(view.container.querySelector(".activity-cell-failed")).toBeTruthy();
    expect(view.container.textContent).not.toContain(error);
    expect(view.container.querySelector(".activity-cell-running")).toBeNull();
    fireEvent.click(view.getByRole("button", { name: "展开活动详情" }));
    expect(view.getByLabelText("错误详情").textContent).toBe(error);
  });

  it("keeps approval waiting static without claiming navigation started", () => {
    const pending: ToolCallRecord = { id: "pending", name: "browser_control", args: { action: "navigate", url: target },
      status: "pending", transition: "waiting_approval", waitingOn: "approval", startedAt: 1000 };
    const view = render(<ActivityCell cell={{ ...cell, status: "pending_approval", toolCallRecords: [pending] }} />);
    expect(view.container.textContent).toContain("等待批准");
    expect(view.container.querySelector(".activity-cell-running")).toBeNull();
    expect(view.container.querySelector('[data-running="true"]')).toBeNull();
    expect(view.container.textContent).not.toContain("下一步");
  });
});
