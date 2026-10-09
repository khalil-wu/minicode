// @vitest-environment jsdom
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { useAppStore } from "../stores";
import { createStreamBuffer } from "../lib/stream-buffer";
import type { ServerEvent } from "../protocol/events";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { ActivityCell } from "./cells/ActivityCell";
import { ExecCell } from "./cells/ExecCell";
import { ToolCallCard } from "./tool-calls/ToolCallCard";
import { getToolCallsFromMessage } from "../lib/content-blocks";

const initialStore = useAppStore.getState();
const owner = "execution-projection";
const buffer = createStreamBuffer(() => {});
const emit = (event: ServerEvent) => handleChatStreamEvent(event, owner, { textStreamBuffer: buffer });

beforeEach(() => {
  useAppStore.setState({ ...initialStore, conversationId: owner, isStreaming: true,
    conversationStreaming: { [owner]: true }, conversationMessages: {}, sideChats: {},
    messages: [{ id: "answer", role: "assistant", content: "", timestamp: 1, artifacts: [], blocks: [], isStreaming: true }],
  });
});
afterEach(() => { cleanup(); buffer.flush(); useAppStore.setState(initialStore); });

const renderCalls = () => {
  const state = useAppStore.getState();
  const cells = projectMessagesToTurns(state.messages, state.isStreaming)[0].committedCells;
  return render(<div>{cells.map((cell) => cell.kind === "activity"
    ? <ActivityCell key={cell.id} cell={cell} conversationId={owner} />
    : cell.kind === "exec" ? <ExecCell key={cell.id} cell={cell} /> : null)}</div>);
};

it("keeps malformed task arguments readable as a failed action instead of crashing its target label", () => {
  useAppStore.setState({ messages: [{ id: "answer", role: "assistant", content: "", timestamp: 1, artifacts: [], blocks: [
    { type: "tool_call", record: { id: "invalid-task", name: "task", args: { parallel_tasks: [null] }, status: "failed", startedAt: 1,
      summary: "parallel_tasks item must be an object" } },
  ] }] });
  const view = renderCalls();
  fireEvent.click(view.getByRole("button", { name: "展开活动详情" }));
  expect(view.container.textContent).toContain("parallel_tasks item must be an object");
});

it("keeps a rejected weather delegation as its actual expandable action without inventing a child or wrapper failure", () => {
  const failure = "Error: Parallel write-capable task(s) declare no write_scope: 'Weather review'.";
  useAppStore.setState({ subagents: [], messages: [{ id: "answer", role: "assistant", content: "", timestamp: 1, artifacts: [], isStreaming: true, blocks: [
    { type: "tool_call", record: { id: "weather-exec", name: "tool_exec", args: {}, status: "success", startedAt: 1,
      summary: JSON.stringify({ cell_id: "weather-cell", status: "completed", output: [failure] }) } },
    { type: "tool_call", record: { id: "weather-task", name: "task", args: { parallel_tasks: [{ description: "Weather review", prompt: "Private instructions to child", agent_type: "general-purpose" }] },
      status: "failed", startedAt: 1, summary: failure, resultKind: "subagent",
      callSource: { kind: "code_mode", cell_id: "weather-cell", parent_call_id: "weather-exec", runtime_call_id: "1" } } },
  ] }] });
  const cells = projectMessagesToTurns(useAppStore.getState().messages, true)[0].committedCells;
  expect(cells).toHaveLength(1);
  expect(cells[0]).toMatchObject({ kind: "activity", id: "weather-task", collapsed: true });
  const view = renderCalls();
  expect(view.container.textContent).toContain("启动子智能体");
  expect(view.container.textContent).toContain("失败");
  expect(view.container.textContent).not.toContain("错误详情");
  expect(view.container.textContent).not.toContain("开始工作");
  expect(view.container.textContent).not.toContain(failure);
  fireEvent.click(view.getByRole("button", { name: "展开活动详情" }));
  expect(view.container.textContent).toContain(failure);
  expect(view.container.textContent).not.toContain("Private instructions to child");
  expect(useAppStore.getState().subagents).toEqual([]);
  expect(getToolCallsFromMessage(useAppStore.getState().messages[0])).toHaveLength(2);
});

it("projects real stream start, approval, execution and terminal results without animating waiting or settled calls", () => {
  emit({ type: "tool_call", id: "web", name: "web_fetch", args: { url: "https://weather.example/" }, status: "pending", result_kind: "web", message_id: "answer", seq: 1 });
  emit({ type: "permission.decision", tool_call_id: "web", tool_name: "web_fetch", decision: "ask", message_id: "answer", seq: 2 });
  emit({ type: "tool_call", id: "cmd", name: "run_command", args: { command: "python report.py" }, status: "pending", result_kind: "command", message_id: "answer", seq: 3 });
  let view = renderCalls();
  expect(view.container.querySelectorAll(".activity-cell-running, .exec-cell-running")).toHaveLength(0);
  expect(view.container.textContent).toContain("等待批准");
  expect(view.container.textContent).toContain("准备运行命令");
  const web = getToolCallsFromMessage(useAppStore.getState().messages[0]).find((record) => record.id === "web")!;
  const card = render(<ToolCallCard record={web} />);
  expect(card.container.querySelector(".animate-spin, .progress-bar")).toBeNull();
  cleanup();

  emit({ type: "permission.decision", tool_call_id: "web", tool_name: "web_fetch", decision: "allow", message_id: "answer", seq: 4 });
  view = renderCalls();
  expect(view.container.querySelectorAll(".activity-cell-running")).toHaveLength(1);
  expect(view.container.querySelectorAll(".exec-cell-running")).toHaveLength(0);
  cleanup();

  emit({ type: "tool_result", id: "web", status: "failed", is_error: true, summary: "Connection refused by upstream", result_kind: "web", message_id: "answer", seq: 5 });
  emit({ type: "tool_call", id: "cmd", name: "run_command", args: { command: "python report.py" }, status: "running", result_kind: "command", message_id: "answer", seq: 6 });
  view = renderCalls();
  expect(view.container.querySelectorAll(".activity-cell-running")).toHaveLength(0);
  expect(view.container.querySelectorAll(".exec-cell-running")).toHaveLength(1);
  expect(view.container.textContent).not.toContain("等待批准");
  fireEvent.click(view.getByRole("button", { name: "展开活动详情" }));
  expect(view.container.textContent).toContain("Connection refused by upstream");
  cleanup();

  emit({ type: "tool_result", id: "cmd", status: "success", is_error: false, summary: "report complete", result_kind: "command", message_id: "answer", seq: 7 });
  view = renderCalls();
  expect(view.container.querySelectorAll(".activity-cell-running, .exec-cell-running")).toHaveLength(0);
  expect(getToolCallsFromMessage(useAppStore.getState().messages[0]).map((record) => record.status)).toEqual(["failed", "success"]);
});

it.each(["success", "failed", "blocked", "timeout", "cancelled", "partial"] as const)("keeps restored %s records static beside a live call", (status) => {
  useAppStore.setState({ messages: [{ id: "answer", role: "assistant", content: "", timestamp: 1, artifacts: [], isStreaming: true, blocks: [
    { type: "tool_call", record: { id: "settled", name: "web_fetch", args: { url: "https://weather.example/history" }, status, resultKind: "web", startedAt: 1, outputPreview: "Retained evidence" } },
    { type: "tool_call", record: { id: "live", name: "web_fetch", args: { url: "https://weather.example/current" }, status: "running", resultKind: "web", startedAt: 2 } },
  ] }] });
  const view = renderCalls();
  const animated = view.container.querySelectorAll(".activity-cell-running");
  expect(animated).toHaveLength(1);
  expect(animated[0].textContent).toContain("/current");
  expect(animated[0].textContent).not.toContain("/history");
});
