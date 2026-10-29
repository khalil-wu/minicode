// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { createStreamBuffer } from "../lib/stream-buffer";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import { projectTurn } from "../lib/turn-projection";
import type { ServerEvent } from "../protocol/events";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleControlEvent } from "./controlEvents";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { hydrateMessages, toToolCallRecord } from "./transcriptHydration";
import { ExecCell } from "./cells/ExecCell";
import { ActivityCell } from "./cells/ActivityCell";
import type { ExecCellState } from "./cells/cellTypes";
import type { ContentBlock } from "../stores/types";
import { ChatTurn } from "./components/ChatTurn";

const initial = useAppStore.getState();
const owner = "native-activity-owner";
const messageId = "native-activity-answer";
const callId = "native-activity-command";
const command = 'Write-Output "ACTUAL_NATIVE_RESULT"';
const presentation = { command, cwd: "C:/owned/frozen-workspace", started_at_ms: 3000 };
const buffer = createStreamBuffer(() => {});
const emit = (value: Record<string, unknown>) => {
  const normalized = normalizeInboundServerEvent({ conversation_id: owner, message_id: messageId, turn_id: "native-turn", ...value });
  expect(normalized).toBeTruthy();
  const event = normalized as ServerEvent;
  return handleChatStreamEvent(event, owner, { textStreamBuffer: buffer }) || handleRuntimeEvent(event, owner) || handleControlEvent(event);
};
const records = () => getToolCallsFromMessage(useAppStore.getState().messages[0]);
const cells = () => projectMessagesToTurns(useAppStore.getState().messages, true)[0].committedCells;
const execution = () => cells().find(cell => cell.kind === "exec") as ExecCellState;
const provider = () => emit({ type: "tool_call", id: callId, name: "run_command", args: {}, status: "pending",
  result_kind: "command", streaming_announcement: true, started_at: 1000, seq: 1 });
const planned = (seq = 2) => emit({ type: "tool_call", id: callId, name: "run_command", args: { command, login: false },
  status: "pending", result_kind: "command", streaming_announcement: false, command_presentation: presentation, started_at: 1000, seq });

beforeEach(() => {
  vi.spyOn(Date, "now").mockReturnValue(10000);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  useAppStore.setState({ ...initial, reducedMotion: true, conversationId: owner, isStreaming: true,
    conversationStreaming: { [owner]: true }, conversationMessages: {}, sideChats: {}, pendingApproval: null,
    messages: [{ id: messageId, turnId: "native-turn", role: "assistant", content: "", timestamp: 1000, artifacts: [], blocks: [], isStreaming: true }],
  });
});
afterEach(() => { cleanup(); buffer.flush(); useAppStore.setState(initial); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("keeps provider arguments as raw evidence and projects only the real frozen same-id planned command", () => {
  provider();
  expect(cells()).toEqual([]);
  expect(records()).toHaveLength(1);
  expect(records()[0]).toMatchObject({ id: callId, args: {}, streamingAnnouncement: true, status: "pending" });
  planned();
  expect(cells()).toHaveLength(1);
  expect(execution()).toMatchObject({ id: callId, command, cwd: presentation.cwd, commandPresentation: presentation, durationMs: undefined });
  const view = render(<ExecCell cell={execution()} />);
  expect(screen.getByText("正在运行命令")).toBeTruthy();
  expect(view.container.querySelector('[data-source-icon="terminal-light-16"]')).toBeTruthy();
  expect(view.container.querySelector(".exec-cell-command-preview")).toBeNull();
  expect(view.container.textContent).not.toMatch(/等待批准运行命令|准备命令|准备运行命令|7s|9s/);
  fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
  expect(screen.getByText("$ " + command)).toBeTruthy();
  expect(view.container.querySelector(".exec-cell-output-pre")?.textContent).toBe("");
  expect(view.container.querySelector(".exec-cell-output-status")).toBeNull();
  expect(records()[0].executionStartedAt).toBeUndefined();
});

it("keeps native approval separate from the planned activity and never turns its internal argv into displayed command text", () => {
  provider(); planned();
  emit({ type: "control_request", request_id: callId, replayed: true, request: {
    subtype: "can_use_tool", tool_name: "run_command", tool_use_id: callId,
    input: { command, login: false }, command: ["powershell.exe", "-Command", "__INTERNAL_WRAPPER__"],
    display_command: command, cwd: presentation.cwd, available_decisions: ["approved", "abort"], request_digest: "a".repeat(64),
  } });
  expect(useAppStore.getState().pendingApproval).toMatchObject({ requestId: callId, displayCommand: command });
  expect(cells()).toHaveLength(1);
  const view = render(<ExecCell cell={execution()} />);
  expect(view.container.textContent).toContain("正在运行命令");
  expect(view.container.textContent).not.toMatch(/__INTERNAL_WRAPPER__|等待批准运行命令|准备命令/);
  expect(records()).toHaveLength(1);
});

it("uses the Source active group label and actual terminal glyph through the full ChatTurn without a preparing or waiting group", () => {
  provider(); planned();
  expect(records()[0].status).toBe("pending");
  expect(execution().status).toBe("running");
  const view = render(<ChatTurn turn={projectMessagesToTurns(useAppStore.getState().messages, true)[0]} conversationId={owner} />);
  const group = screen.getByRole("button", { name: `正在运行 ${command}` });
  expect(group.querySelector('[data-source-icon="terminal-light-16"]')).toBeTruthy();
  expect(view.container.textContent).not.toMatch(/等待批准运行命令|准备运行命令|准备命令/);
  fireEvent.click(group);
  expect(screen.getByText("正在运行命令")).toBeTruthy();
  expect(records()[0].executionStartedAt).toBeUndefined();
});

it("starts execution timing only at the real runtime start, then preserves the original record and output through terminal/cold hydration", () => {
  provider(); planned();
  emit({ type: "tool_call", id: callId, name: "run_command", args: { command }, status: "running", result_kind: "command",
    command_presentation: presentation, streaming_announcement: false, started_at: 1000, seq: 3 });
  expect(execution().executionStartedAt).toBeUndefined();
  emit({ type: "runtime.span", event: "tool.started", span_id: `tool:${callId}`, tool_call_id: callId, tool_name: "run_command",
    phase: "tool", status: "running", started_at: 9000, ui_visible: true, debug_only: false, iteration_id: "iter:1" });
  expect(execution().executionStartedAt).toBe(9000);
  const view = render(<ExecCell cell={execution()} />);
  expect(screen.getByText("命令已运行 1s")).toBeTruthy();
  emit({ type: "tool_output_delta", id: callId, output: "ACTUAL_NATIVE_RESULT\n", stream: "stdout", seq: 4 });
  emit({ type: "tool_result", id: callId, status: "success", is_error: false, summary: "Exit code: 0\nACTUAL_NATIVE_RESULT",
    stdout_preview: "ACTUAL_NATIVE_RESULT\n", exit_code: 0, duration_ms: 1088, completed_at_ms: 10088,
    command_presentation: presentation, execution_started_at: 9000, streaming_announcement: false, result_kind: "command", seq: 5 });
  expect(records()).toHaveLength(1);
  expect(records()[0]).toMatchObject({ id: callId, status: "success", commandPresentation: presentation, executionStartedAt: 9000, durationMs: 1088, exitCode: 0 });
  view.rerender(<ExecCell cell={execution()} />);
  expect(screen.getByText("已在 1s 内运行")).toBeTruthy();
  expect(screen.getByText(command)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
  expect(view.container.querySelector(".exec-cell-output-stdout")?.textContent).toBe("ACTUAL_NATIVE_RESULT\n");
  const cold = hydrateMessages([{ id: messageId, role: "assistant", content: "", timestamp: 1000,
    blocks: useAppStore.getState().messages[0].blocks, terminal_status: "completed" }]);
  expect(getToolCallsFromMessage(cold[0])[0]).toMatchObject({ commandPresentation: presentation, executionStartedAt: 9000, durationMs: 1088, exitCode: 0 });
  expect(projectMessagesToTurns(cold, false)[0].committedCells[0]).toMatchObject({ kind: "exec", id: callId, command, durationMs: 1088 });
});

it.each(["blocked", "cancelled"] as const)("keeps a native %s terminal without exit/output from becoming a pending approval or an invented execution duration", status => {
  provider(); planned();
  emit({ type: "tool_result", id: callId, status, is_error: true, summary: "The original command was not executed.",
    error_kind: status === "blocked" ? "approval_rejected" : "approval_aborted", duration_ms: 31000, result_kind: "command", seq: 3 });
  expect(cells()).toHaveLength(1);
  expect(execution()).toMatchObject({ status: status === "blocked" ? "failed" : "cancelled", durationMs: undefined });
  const view = render(<ExecCell cell={execution()} />);
  expect(screen.getByText("运行命令")).toBeTruthy();
  expect(view.container.querySelector('[data-source-icon="terminal-light-16"]')).toBeTruthy();
  expect(view.container.querySelector(".exec-cell-header-button")?.getAttribute("data-processing")).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
  expect(screen.queryByText("The original command was not executed.")).toBeNull();
  expect(view.container.querySelector(".exec-cell-output-pre")?.textContent).toBe("");
  expect(view.container.querySelector(".exec-cell-no-output")).toBeNull();
  expect(view.container.querySelector(".exec-cell-output-status")).toBeNull();
  expect(view.container.textContent).not.toMatch(/等待批准|准备命令|已停止|失败/);
  expect(records()[0].summary).toBe("The original command was not executed.");
});

it("retains a real pre-execution approval timeout as diagnostic in live and restored detail", () => {
  const diagnostic = "Operation rejected: The turn's time budget was exhausted before this approval was answered. The action was not performed.";
  provider(); planned();
  emit({ type: "tool_result", id: callId, status: "blocked", is_error: true, summary: diagnostic,
    developer_detail: diagnostic, error_kind: "approval_rejected", result_kind: "command", seq: 3 });
  const cold = hydrateMessages([{ id: messageId, role: "assistant", content: "", timestamp: 1000,
    blocks: useAppStore.getState().messages[0].blocks, terminal_status: "completed" }]);
  for (const cell of [execution(), projectMessagesToTurns(cold, false)[0].committedCells[0]]) {
    expect(cell).toMatchObject({ kind: "exec", diagnostic });
    if (cell.kind !== "exec") throw new Error("Expected command evidence");
    const view = render(<ExecCell cell={cell} />);
    expect(screen.getByText("运行命令")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
    expect(view.container.textContent).toContain(diagnostic);
    expect(view.container.querySelector(".exec-cell-output-stdout")).toBeNull();
    expect(view.container.querySelector(".exec-cell-output-status")).toBeNull();
    expect(view.container.textContent).not.toContain("已运行");
    view.unmount();
  }
});

it("keeps actual typed stdout and stderr instead of a model receipt even when execution start metadata is absent", () => {
  provider(); planned();
  emit({ type: "tool_result", id: callId, status: "blocked", is_error: true, summary: "The model-facing approval receipt",
    error_kind: "approval_rejected", stdout_preview: "Actual stdout evidence\n", stderr_preview: "Actual stderr evidence\n", result_kind: "command", seq: 3 });
  const view = render(<ExecCell cell={execution()} />);
  fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
  expect(view.container.querySelector(".exec-cell-output-stdout")?.textContent).toBe("Actual stdout evidence\n");
  expect(view.container.querySelector(".exec-cell-output-stderr")?.textContent).toBe("Actual stderr evidence\n");
  expect(view.container.textContent).not.toContain("The model-facing approval receipt");
  expect(records()[0].summary).toBe("The model-facing approval receipt");
});

it("retains genuine execution errors after a real start when no typed command stream was returned", () => {
  provider(); planned();
  emit({ type: "tool_call", id: callId, name: "run_command", args: { command }, status: "running", command_presentation: presentation,
    execution_started_at: 9000, streaming_announcement: false, result_kind: "command", seq: 3 });
  emit({ type: "tool_result", id: callId, status: "failed", is_error: true, summary: "Actual execution failed while opening the output stream",
    error_kind: "tool_execution_error", result_kind: "command", seq: 4 });
  const view = render(<ExecCell cell={execution()} />);
  fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
  expect(view.container.querySelector(".exec-cell-output-stdout")?.textContent).toBe("Actual execution failed while opening the output stream");
  expect(records()[0].summary).toBe("Actual execution failed while opening the output stream");
  expect(view.container.textContent).not.toContain("工具执行失败");
});

it("preserves actual prior execution/output through same-id escalation and duplicate staged notifications", () => {
  provider(); planned();
  emit({ type: "tool_call", id: callId, name: "run_command", args: { command }, status: "running", command_presentation: presentation,
    execution_started_at: 9000, streaming_announcement: false, result_kind: "command", seq: 3 });
  emit({ type: "tool_output_delta", id: callId, output: "retained earlier output\n", stream: "stdout", seq: 4 });
  planned(5);
  planned(6);
  expect(records()).toHaveLength(1);
  expect(records()[0]).toMatchObject({ executionStartedAt: 9000, stdoutPreview: "retained earlier output\n", commandPresentation: presentation });
  expect(execution().stdoutFull).toBe("retained earlier output\n");
  emit({ type: "tool_result", id: callId, status: "cancelled", is_error: true, summary: "Command interrupted after output",
    stdout_preview: "retained earlier output\n", duration_ms: 1800, result_kind: "command", seq: 7 });
  const view = render(<ExecCell cell={execution()} />);
  expect(screen.getByText("运行 1s 后停止")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
  expect(screen.getByText("retained earlier output")).toBeTruthy();
  expect(screen.getByText("已停止")).toBeTruthy();
  expect(view.container.textContent).not.toMatch(/退出码 未知|失败/);
});

it("retains native presentation and actual start on resume without manufacturing a new command item", () => {
  const restored: ToolCallRecord = { id: callId, name: "run_command", args: { command }, status: "running", resultKind: "command",
    commandPresentation: presentation, executionStartedAt: 9000, streamingAnnouncement: false, startedAt: 1000,
    stdoutPreview: "before reconnect\n", transition: "streaming_output" };
  emit({ type: "stream_resume", event_seq: 40, stream_status: "running", last_event_type: "tool_output_delta",
    content_blocks: [{ type: "tool_call", record: restored }], tool_states: [restored], tool_calls_pending: [restored] });
  expect(records()).toHaveLength(1);
  expect(execution()).toMatchObject({ id: callId, commandPresentation: presentation, executionStartedAt: 9000, stdoutFull: "before reconnect\n" });
  expect(toToolCallRecord({ id: callId, name: "run_command", args: { command }, status: "pending", started_at: 1000,
    command_presentation: presentation, streaming_announcement: false })?.commandPresentation).toEqual(presentation);
});

it("attaches Guardian only after its actual target is projected and retains an interrupted provider announcement as readable evidence", () => {
  const placeholder: ContentBlock = { type: "tool_call", record: { id: callId, name: "run_command", args: {}, status: "pending",
    resultKind: "command", streamingAnnouncement: true, startedAt: 1000 } };
  const review: ContentBlock = { type: "guardian_assessment", review: { id: "native-review", turn_id: "native-turn", target_item_id: callId,
    status: "in_progress", started_at_ms: 2000, action: { type: "command", source: "unified_exec", command, cwd: presentation.cwd } } };
  expect(projectTurn([placeholder, review]).activityItems.map(item => item.kind)).toEqual(["guardianReview"]);
  const actual: ContentBlock = { ...placeholder, record: { ...placeholder.record, args: { command }, streamingAnnouncement: false, commandPresentation: presentation } };
  expect(projectTurn([actual, review]).activityItems).toMatchObject([{ id: callId, kind: "commandExecution", automaticApprovalReviews: [review.review] }]);
  useAppStore.setState({ messages: [{ ...useAppStore.getState().messages[0], blocks: [{ ...placeholder,
    record: { ...placeholder.record, status: "cancelled", summary: "Provider stopped before the arguments were complete; this tool was never executed." } }] }] });
  const cell = cells()[0];
  expect(cell.kind).toBe("activity");
  if (cell.kind !== "activity") throw new Error("The terminal provider receipt must stay readable");
  render(<ActivityCell cell={cell} />);
  fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
  expect(screen.getByText("Provider stopped before the arguments were complete; this tool was never executed.")).toBeTruthy();
  expect(records()).toHaveLength(1);
});
