/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { HistoryCellRenderer } from "./components/ChatTurn";
import { ToolCallCard } from "./tool-calls/ToolCallCard";
import { projectTurn } from "../lib/turn-projection";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { inheritMessageTopology, recordStreamingToolUpdate } from "../lib/message-changes";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import type { ChatMessage, ContentBlock } from "../stores/types";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) }));

const receipt = '{"message":"Wait completed.","timed_out":false}';
const record = (status: ToolCallRecord["status"], id = "wait-receipt"): ToolCallRecord => ({
  id, name: "wait_agent", args: { timeout_ms: 30000 }, status, startedAt: 1000,
  activityKind: "commandExecution", displaySummary: "Wait for agents",
  outputPreview: ["failed", "blocked", "timeout", "partial", "cancelled"].includes(status) ? "Actual wait handler error" : receipt,
});
const message = (records: ToolCallRecord[], streaming = true): ChatMessage => ({
  id: "wait-message", role: "assistant", content: "", timestamp: 1000, isStreaming: streaming,
  blocks: records.map(record => ({ type: "tool_call", record })),
});

beforeEach(() => useAppStore.setState({ conversationId: "wait-owner", workingDirectory: "C:/wait", viewMode: "normal" }));
afterEach(cleanup);

it.each(["pending", "running", "success"] as const)(
  "keeps %s wait receipts out of live and restored timelines without changing raw history", status => {
    const live = message([record(status)]);
    const original = JSON.stringify(live);
    const restored = hydrateMessages([{ id: live.id, role: live.role, content: "", timestamp: live.timestamp,
      is_streaming: false, blocks: live.blocks }])[0];
    const legacy = { ...live, blocks: undefined, toolCalls: [record(status)], isStreaming: false };
    for (const candidate of [live, restored, legacy]) {
      const turn = projectMessagesToTurns([candidate], candidate.isStreaming === true)[0];
      expect(turn.committedCells).toEqual([]);
      expect(turn.workItems?.filter(item => item.kind === "process") ?? []).toEqual([]);
      expect(getToolCallsFromMessage(candidate)).toHaveLength(1);
      expect(getToolCallsFromMessage(candidate)[0].outputPreview).toBe(record(status).outputPreview);
    }
    expect(projectTurn(live.blocks!).activityItems).toEqual([]);
    expect(projectTurn(live.blocks!, { includeHiddenActivity: true }).activityItems[0].records).toEqual([getToolCallsFromMessage(live)[0]]);
    expect(JSON.stringify(live)).toBe(original);
  },
);

it("removes five repeated waits while preserving real delegation, read, command and final-answer evidence", () => {
  const records: ToolCallRecord[] = [
    { ...record("success", "spawn"), name: "spawn_agent", summary: "Actual worker started" },
    ...Array.from({ length: 5 }, (_, index) => record("success", `wait-${index}`)),
    { ...record("success", "read"), name: "read_file", args: { file_path: "fact.txt" }, activityKind: "fileRead" },
    { ...record("success", "run"), name: "run_command", args: { command: "git status" }, activityKind: "commandExecution" },
  ];
  const input = message(records, false);
  input.blocks!.push({ type: "text", itemId: "final", content: "Actual final answer", source: "reply", status: "completed" });
  const original = JSON.stringify(input);
  const projection = projectTurn(input.blocks!);
  expect(projection.activityItems.map(item => item.id)).toEqual(["spawn", "read", "run"]);
  expect(projection.finalAnswer).toBe("Actual final answer");
  expect(projectTurn(input.blocks!, { includeHiddenActivity: true }).activityItems).toHaveLength(8);
  expect(getToolCallsFromMessage(input)).toEqual(records);
  expect(JSON.stringify(input)).toBe(original);
});

it.each(["normal", "summary", "verbose"] as const)("renders no successful wait card in %s view", viewMode => {
  const { container } = render(<ToolCallCard record={record("success")} viewMode={viewMode} conversationId="wait-owner" />);
  expect(container.innerHTML).toBe("");
});

it.each(["normal", "summary", "verbose"] as const)("retains a failed wait card in %s view", viewMode => {
  const { container } = render(<ToolCallCard record={record("failed")} viewMode={viewMode} conversationId="wait-owner" />);
  expect(container.textContent).toContain("Actual wait handler error");
});

it.each(["failed", "blocked", "timeout", "partial", "cancelled"] as const)("retains the real %s wait error in live, restored and legacy process detail", status => {
  const input = message([{ ...record(status), visibility: "debug" }], false);
  const restored = hydrateMessages([{ id: input.id, role: input.role, content: "", timestamp: input.timestamp,
    blocks: input.blocks }])[0];
  const legacy = { ...input, blocks: undefined, toolCalls: [record(status)] };
  for (const candidate of [input, restored, legacy]) {
    const turn = projectMessagesToTurns([candidate], false)[0];
    expect(turn.committedCells).toEqual([expect.objectContaining({ kind: "activity",
      toolCallRecords: [expect.objectContaining({ outputPreview: "Actual wait handler error" })] })]);
    const view = render(<HistoryCellRenderer cell={turn.committedCells[0]} conversationId="wait-owner" />);
    fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
    expect(view.container.textContent).toContain("Actual wait handler error");
    expect(getToolCallsFromMessage(candidate)[0].outputPreview).toBe("Actual wait handler error");
    view.unmount();
  }
});

it("omits normal standalone wait progress while retaining its real error", () => {
  const blocks: ContentBlock[] = ["running", "completed", "failed"].map((status, index) => ({
    type: "progress", id: `wait-progress-${index}`, stage: "tool", toolName: "wait_agent",
    status: status as "running" | "completed" | "failed", title: "Wait for agents",
    ...(status === "failed" ? { errorMessage: "Actual wait handler error", visibility: "debug" } : {}),
  }));
  expect(projectTurn(blocks).activityItems.map(item => item.id)).toEqual(["wait-progress-2"]);
  expect(projectTurn(blocks, { includeHiddenActivity: true }).activityItems).toHaveLength(3);
  const input = message([], false);
  input.blocks = blocks;
  const cell = projectMessagesToTurns([input], false)[0].committedCells[0];
  const view = render(<HistoryCellRenderer cell={cell} conversationId="wait-owner" />);
  fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
  expect(view.container.textContent).toContain("Actual wait handler error");
});

it.each([
  ["running", "failed", 1], ["failed", "running", 0], ["failed", "success", 0],
] as const)("reprojects streamed wait transitions from %s to %s", (from, to, count) => {
  const previous = message([record(from)]);
  const before = [previous];
  projectMessagesToTurns(before, true);
  const next = message([record(to)]);
  const after = [next];
  recordStreamingToolUpdate(previous, next, 0);
  inheritMessageTopology(before, after);
  const turn = projectMessagesToTurns(after, true)[0];
  expect(turn.committedCells).toHaveLength(count);
  expect(getToolCallsFromMessage(next)[0].status).toBe(to);
});

it("keeps a successful timed-out wait receipt silent", () => {
  const input = message([{ ...record("success"), outputPreview: '{"message":"Wait timed out.","timed_out":true}' }]);
  expect(projectMessagesToTurns([input], true)[0].committedCells).toEqual([]);
  expect(getToolCallsFromMessage(input)[0].outputPreview).toContain('"timed_out":true');
});
