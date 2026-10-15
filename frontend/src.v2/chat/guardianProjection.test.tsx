/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { GuardianAssessment, GuardianAssessmentEvent } from "../protocol/guardian-types";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { hydrateMessages } from "./transcriptHydration";
import { applyGuardianEvent } from "./guardianEvents";
import { projectTurn } from "../lib/turn-projection";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { GuardianReviewCell, GuardianToolReviews, GuardianWarningCell } from "./cells/GuardianReviewCell";
import { ExecCell } from "./cells/ExecCell";
import { ChatTurn } from "./components/ChatTurn";
import { AgentTimeline } from "../agent-loop/components/AgentTimeline";
import type { GuardianReviewCellState } from "./cells/cellTypes";

const assessment = (patch: Partial<GuardianAssessment> = {}): GuardianAssessment => ({
  id: "review", turn_id: "turn", started_at_ms: 1000, status: "in_progress",
  action: { type: "command", source: "unified_exec", command: "Get-Content source.ts", cwd: "C:/workspace" }, ...patch,
});
const event = (patch: Partial<GuardianAssessment> = {}): GuardianAssessmentEvent => ({
  type: "guardian_assessment", conversation_id: "conversation", message_id: "answer", ...assessment(patch),
});
const message = (blocks: ContentBlock[] = []): ChatMessage => ({ id: "answer", turnId: "turn", role: "assistant", content: "", timestamp: 1000, isStreaming: true, blocks });
const cell = (review: GuardianAssessment): GuardianReviewCellState => ({ kind: "guardian_review", id: review.id, review,
  createdAt: review.started_at_ms, status: review.status === "in_progress" ? "running" : "done", segment: 0 });

afterEach(cleanup);
beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

describe("Guardian Source projection", () => {
  it("projects a real pending patch and scoped Guardian event through ChatTurn to a folded file review without fake approval or diff", () => {
    const tool: ContentBlock = { type: "tool_call", record: { id: "patch-live", name: "apply_patch", args: { patch: "*** Begin Patch\n*** Add File: pending.txt\n+original bytes\n*** End Patch" },
      status: "pending", transition: "waiting_approval", waitingOn: "approval", startedAt: 1000, activityKind: "fileChange" } };
    const incoming = event({ id: "patch-review-live", target_item_id: "patch-live", action: { type: "apply_patch", cwd: "C:/workspace", files: ["C:/workspace/pending.txt"] } });
    const live = applyGuardianEvent([message([tool])], incoming)!;
    const turns = projectMessagesToTurns(live, true);
    expect(turns[0].committedCells).toHaveLength(1);
    expect(turns[0].committedCells[0]).toMatchObject({ kind: "activity", automaticApprovalReviews: [{ id: "patch-review-live" }] });
    const { container, rerender } = render(<ChatTurn turn={turns[0]} conversationId="patch-live-owner" workspaceRoot="C:/workspace" />);
    expect(screen.getByText("pending.txt")).toBeTruthy();
    expect(container.querySelector(".guardian-patch-header .guardian-review-glyph")).toBeTruthy();
    expect(container.querySelector(".activity-cell-file-change-stats")).toBeNull();
    expect(screen.queryByText(/等待批准/)).toBeNull();
    expect(screen.queryByText("+0")).toBeNull();
    expect(screen.queryByText("自动审核中")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开 C:/workspace/pending.txt 的修改" }));
    expect(screen.getByRole("button", { name: "自动审核中" }).getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector('.guardian-review-details[data-high-risk="true"]')).toBeNull();
    expect(container.querySelector(".guardian-review-details .agent-loop-timeline-group-live-title")).toBeTruthy();

    const approved = applyGuardianEvent(live, { ...incoming, status: "approved", completed_at_ms: 2000 })!;
    const completed: ContentBlock = { ...tool, record: { ...tool.record, status: "success", transition: "completed", waitingOn: undefined,
      diff: { plus: 1, minus: 0, files: [{ path: "pending.txt", plus: 1, minus: 0, status: "added", patch: "@@ -0,0 +1 @@\n+original bytes" }] } } };
    const after = { ...approved[0], blocks: [completed, ...approved[0].blocks!.slice(1)] };
    rerender(<ChatTurn turn={projectMessagesToTurns([after], true)[0]} conversationId="patch-live-owner" workspaceRoot="C:/workspace" />);
    expect(container.querySelector(".guardian-patch-cell")).toBeTruthy();
    expect(screen.queryByText("自动审核中")).toBeNull();
    expect(screen.getByRole("button", { name: "收起 C:/workspace/pending.txt 的修改" }).getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector(".activity-cell-file-change-stats")).toBeTruthy();
  });

  it("keeps combined Timeline work on the real automatic-review action and exposes its file disclosure", () => {
    const read: ContentBlock = { type: "tool_call", record: { id: "read-before-review", name: "read_file", args: { path: "source.ts" }, status: "success", startedAt: 900 } };
    const patch: ContentBlock = { type: "tool_call", record: { id: "patch-group", name: "apply_patch", args: { patch: "*** Begin Patch\n*** Add File: grouped.txt\n+bytes\n*** End Patch" },
      status: "pending", transition: "waiting_approval", waitingOn: "approval", startedAt: 1000 } };
    const live = applyGuardianEvent([message([read, patch])], event({ id: "patch-group-review", target_item_id: "patch-group", action: { type: "apply_patch", cwd: "C:/workspace", files: ["C:/workspace/grouped.txt"] } }))!;
    const { container } = render(<ChatTurn turn={projectMessagesToTurns(live, true)[0]} conversationId="patch-group-owner" workspaceRoot="C:/workspace" />);
    expect(screen.queryByText(/等待批准/)).toBeNull();
    const group = screen.getByRole("button", { name: "正在编辑 C:/workspace/grouped.txt" });
    expect(group.querySelector(".guardian-review-glyph")).toBeTruthy();
    fireEvent.click(group);
    expect(screen.getByText("grouped.txt")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开 C:/workspace/grouped.txt 的修改" }));
    expect(container.querySelector(".guardian-review-details")).toBeTruthy();
    expect(screen.getByRole("button", { name: "自动审核中" }).getAttribute("aria-expanded")).toBe("false");
  });

  it("does not accept an assistant message ID as a different canonical turn in the live renderer chain", () => {
    const tool: ContentBlock = { type: "tool_call", record: { id: "patch-canonical", name: "apply_patch", args: {}, status: "pending", startedAt: 1000 } };
    const owner = { ...message([tool]), turnId: "run-canonical" };
    const review = event({ id: "canonical-review", turn_id: "answer", target_item_id: "patch-canonical", action: { type: "apply_patch", cwd: "C:/workspace", files: ["C:/workspace/target.txt"] } });
    expect(applyGuardianEvent([owner], review)).toBeNull();
    const live = applyGuardianEvent([owner], { ...review, turn_id: "run-canonical" })!;
    render(<ChatTurn turn={projectMessagesToTurns(live, true)[0]} conversationId="canonical-owner" workspaceRoot="C:/workspace" />);
    expect(screen.getByText("target.txt")).toBeTruthy();
    expect(screen.queryByText(/等待批准/)).toBeNull();
  });

  it("keeps live, replay and cold transcripts on the same typed review and hides approval", () => {
    const normalized = normalizeInboundServerEvent(event());
    expect(normalized?.type).toBe("guardian_assessment");
    const live = applyGuardianEvent([message()], event())!;
    const cold = hydrateMessages([{ id: "answer", turn_id: "turn", role: "assistant", timestamp: 1000, blocks: live[0].blocks }]);
    expect(projectTurn(live[0].blocks!).activityItems.map(item => item.kind)).toEqual(["guardianReview"]);
    expect(projectTurn(cold[0].blocks!).activityItems[0].review).toEqual(assessment());
    const approved = applyGuardianEvent(live, event({ status: "approved", completed_at_ms: 2000 }))!;
    expect(projectTurn(approved[0].blocks!).activityItems).toEqual([]);
    expect(approved[0].blocks).toHaveLength(1);
    expect(applyGuardianEvent(approved, event())).toBeNull();
    expect(applyGuardianEvent(approved, { ...event(), turn_id: "another-turn" })).toBeNull();
  });

  it("attaches shell-read reviews to their real exec target and retains denied MCP separately", () => {
    const command: ContentBlock = { type: "tool_call", record: { id: "call", name: "run_command", args: { command: "Get-Content source.ts" },
      status: "blocked", errorKind: "approval_rejected", startedAt: 1000, commandActions: [{ type: "read", command: "Get-Content source.ts", path: "source.ts", name: "source.ts" }] } };
    const review: ContentBlock = { type: "guardian_assessment", review: assessment({ target_item_id: "call", status: "denied", risk_level: "high", rationale: "Original restriction" }) };
    const projected = projectTurn([command, review]);
    expect(projected.activityItems).toHaveLength(1);
    expect(projected.activityItems[0].automaticApprovalReviews).toEqual([review.review]);
    const cells = projectMessagesToTurns([message([command, review])], true)[0].committedCells;
    expect(cells[0]).toMatchObject({ kind: "exec", automaticApprovalReviews: [review.review], guardianDeclined: true });
    const multiRead: ContentBlock = { ...command, record: { ...command.record,
      commandActions: [...command.record.commandActions!, ...command.record.commandActions!] } };
    expect(projectMessagesToTurns([message([multiRead, review])], true)[0].committedCells).toHaveLength(1);
    const mcp: ContentBlock = { type: "tool_call", record: { id: "mcp", name: "mcp__docs__query", args: {}, status: "blocked", startedAt: 1000, activityKind: "mcpToolCall" } };
    expect(projectTurn([mcp, { ...review, review: { ...review.review, target_item_id: "mcp", action: { type: "mcp_tool_call", server: "docs", tool_name: "query" } } }]).activityItems.map(item => item.kind))
      .toEqual(["mcpToolCall", "guardianReview"]);
  });

  it("does not project ordinary assessment warnings or private agents", () => {
    const blocks: ContentBlock[] = [
      { type: "guardian_warning", message: "Approved (risk: low)", turnId: "turn" },
      { type: "guardian_warning", message: "Repeated denials", turnId: "turn", interruption: true },
    ];
    expect(projectTurn(blocks).activityItems.map(item => item.kind)).toEqual(["guardianWarning"]);
    expect(projectMessagesToTurns([message(blocks)], true)[0].committedCells.map(item => item.kind)).toEqual(["guardian_warning"]);
  });

  it("keeps Source review details initially folded and scans only the live title", () => {
    const { container } = render(<GuardianReviewCell cell={cell(assessment())} />);
    expect(screen.queryByText("自动审核中")).toBeNull();
    expect(container.querySelectorAll(".agent-loop-timeline-group-live-title")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Get-Content source.ts" }));
    expect(screen.getByRole("button", { name: "自动审核中" }).getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "自动审核中" }));
    expect(screen.getByText("经过精心提示的审查智能体正在审查此请求，随后 ChatGPT 才会运行它")).toBeTruthy();
  });

  it("uses Source explicit-authorization explanation and interruption guidance without new actions", () => {
    render(<><GuardianToolReviews reviews={[assessment({ status: "denied", risk_level: "high" })]} declined />
      <GuardianWarningCell cell={{ kind: "guardian_warning", id: "warning", message: "Original breaker record", createdAt: 1000 }} /></>);
    expect(screen.getByText("此操作被视为高风险，需要明确授权")).toBeTruthy();
    expect(screen.getByText("本轮操作已被自动审查终止")).toBeTruthy();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "任务中断详情" })).toBeTruthy();
  });

  it("uses genuine declined command receipt and never calls refusal instructions shell stdout", () => {
    const review = assessment({ status: "denied", rationale: "Original reason" });
    const blocks: ContentBlock[] = [{ type: "tool_call", record: { id: "call", name: "run_command", args: { command: "pwd" }, status: "blocked",
      startedAt: 1000, errorKind: "approval_rejected", outputPreview: "Model-facing rejection instructions" } },
    { type: "guardian_assessment", review: { ...review, target_item_id: "call" } }];
    const command = projectMessagesToTurns([message(blocks)], true)[0].committedCells[0];
    expect(command.kind).toBe("exec");
    if (command.kind !== "exec") throw new Error("Expected real exec target");
    render(<ExecCell cell={command} />);
    fireEvent.click(screen.getByRole("button", { name: /命令被自动审核拒绝/ }));
    expect(screen.getByText("被拒绝的原因")).toBeTruthy();
    expect(screen.getByText("Original reason")).toBeTruthy();
    expect(screen.queryByText("Model-facing rejection instructions")).toBeNull();
    expect(screen.queryByText(/退出代码/)).toBeNull();
  });

  it("groups in-progress reviews and leaves denied/timed-out/aborted reviews standalone", () => {
    const exec = { kind: "exec" as const, id: "exec", command: "pwd", status: "success" as const, stdoutPreview: [], stderrPreview: [], collapsed: true, createdAt: 1000, segment: 0 };
    const { container, rerender } = render(<AgentTimeline cells={[exec, cell(assessment())]} renderCell={({ cell: item }) => <div key={item.id}>{item.id}</div>} isRunning />);
    expect(container.querySelectorAll('section[data-group-kind="work"]')).toHaveLength(1);
    for (const status of ["denied", "timed_out", "aborted"] as const) {
      rerender(<AgentTimeline cells={[exec, cell(assessment({ status }))]} renderCell={({ cell: item }) => <div key={item.id}>{item.id}</div>} />);
      expect(container.querySelectorAll('section[data-group-kind="work"]')).toHaveLength(0);
      expect(screen.getByText("review")).toBeTruthy();
    }
  });
});
