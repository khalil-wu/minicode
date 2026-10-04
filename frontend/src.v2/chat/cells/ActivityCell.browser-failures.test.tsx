// @vitest-environment jsdom
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../../stores";
import { ActivityCell } from "./ActivityCell";
import type { ActivityCellState } from "./cellTypes";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import { browserFailureGuidance } from "./activityCellHelpers";

const target = "http://127.0.0.1:55494/preview-test/index.html";
const raw = "Browser navigation to a local, private, or unresolved network target is blocked unless it belongs to the active conversation preview. Network target resolves to a local or private address";
const record: ToolCallRecord = {
  id: "code_browser-fixture", name: "browser_control", args: { action: "navigate", url: target },
  status: "blocked", errorKind: "network_policy", userSummary: "目标未关联到本会话的运行中预览。", developerDetail: raw,
  outputPreview: raw, startedAt: 1000, durationMs: 292,
  callSource: { kind: "code_mode", cell_id: "cell_fixture", parent_call_id: "call_parent_fixture", runtime_call_id: "2" },
};
const cell: ActivityCellState = {kind:"activity",id:"browser-fixture",activityKind:"browser",title:"Browser",status:"failed",collapsed:true,toolCallRecords:[record],startedAt:1000};
beforeEach(() => useAppStore.setState({ conversationId:"owner-A", workingDirectory:"C:/projects/demo",previewLaunchProcesses:[],previewVerification:null,conversationWorkbenchStates:{} }));
afterEach(cleanup);

describe("browser failure projection", () => {
  it("shows the exact action and URL and retains lineage only in details", () => {
    const view=render(<ActivityCell cell={cell} conversationId="owner-A" workspaceRoot="C:/projects/demo" />);
    const row=view.container.querySelector(".activity-cell-main-button")!;
    expect(row.textContent).toContain("Navigate");
    expect(row.textContent).toContain(target);
    expect(row.textContent).not.toContain("Browser");
    expect(row.textContent).not.toContain("code_browser-fixture");
    expect(view.getByText(/目标未关联/)).toBeTruthy();
    fireEvent.click(row);
    const technical = view.container.querySelector(".activity-cell-tool-detail-card")!;
    expect(technical.hasAttribute("open")).toBe(false);
    expect(technical.textContent).not.toContain("cell_fixture");
    expect(technical.textContent).not.toContain("call_parent_fixture");
    expect(view.container.querySelectorAll("pre[aria-label=\"操作结果\"]")).toHaveLength(1);
  });
  it("does not borrow a different conversation's running preview", () => {
    useAppStore.setState({previewLaunchProcesses:[{id:"p-A",name:"dev",command:"node",cwd:"C:/projects/demo",port:55494,url:target,pid:123,status:"ready"}]});
    const view=render(<ActivityCell cell={cell} conversationId="owner-B" workspaceRoot="C:/projects/demo" />);
    expect(view.container.textContent).toContain("回到发起调用的会话");
    expect(view.container.textContent).not.toContain("本会话预览记录：已就绪");
  });
  it("does not infer process failure or success from a legacy policy message", () => {
    const guidance=browserFailureGuidance({...record,status:"failed",errorKind:undefined,userSummary:undefined});
    expect(guidance.reason).toContain("未能确认");
    expect(guidance.reason).not.toContain("崩溃");
    expect(guidance.reason).not.toContain("成功");
  });
  it("surfaces approval waits rather than claiming navigation occurred", () => {
    const guidance=browserFailureGuidance({...record,status:"running",transition:"waiting_approval"});
    expect(guidance.reason).toContain("尚未完成");
    expect(guidance.nextStep).toContain("批准或拒绝");
  });
});
