/* @vitest-environment jsdom */
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MarkdownRenderer } from "./messages/MarkdownRenderer";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { useAppStore } from "../stores";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { AgentTimeline } from "../agent-loop/components/AgentTimeline";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function assistant(): ChatMessage {
  return { id: "assistant", role: "assistant", content: "", timestamp: 1, artifacts: [], isStreaming: true, isThinkingStreaming: true,
    blocks: [...Array.from({ length: 300 }, (_, index): ContentBlock => ({ type: "tool_call", record: {
      id: `t${index}`, name: "read_file", args: { file_path: `file${index}.py` }, status: "success", startedAt: index,
    } })), { type: "thinking", content: "Investigating", source: "provider", item_id: "reasoning" }] };
}

it("keeps interleaved progress, output and text equivalent to an uncached projection", () => {
  vi.spyOn(Date, "now").mockReturnValue(1);
  const message = assistant();
  message.isThinkingStreaming = false;
  message.blocks = [...message.blocks!.filter((block) => block.type === "tool_call"),
    { type: "progress", id: "progress", stage: "tool", status: "running", message: "working", timestamp: 1 },
    { type: "tool_call", record: { id: "command", name: "run_command", args: { command: "pytest" }, status: "running", startedAt: 1 } },
    { type: "text", itemId: "answer", source: "model_final", content: "Started", status: "in_progress", isStreaming: true },
  ];
  useAppStore.setState({ conversationId: "conv", messages: [message], conversationMessages: {}, conversationStreaming: {}, isStreaming: true });
  const originalProjection = projectMessagesToTurns([message], true)[0];
  const firstCell = originalProjection.committedCells[0];
  for (let index = 0; index < 12; index++) {
    useAppStore.getState().upsertMessageProgress({ id: "progress", stage: "tool", status: "running", message: `work ${index}` }, "conv", "assistant");
    useAppStore.getState().updateToolCall("command", { stdoutPreview: `out ${index}`, stderrPreview: `err ${index}`, seq: index + 1 }, "conv", undefined, "assistant");
    useAppStore.getState().appendAgentMessageDelta("answer", " more", "conv", "assistant");
    const current = useAppStore.getState().messages[0];
    const projected = projectMessagesToTurns([current], true)[0];
    expect(projected).toEqual(projectMessagesToTurns([{ ...current }], true)[0]);
    expect(projected.committedCells[0]).toBe(firstCell);
    expect(projected.resourceKey).toBe(originalProjection.resourceKey);
  }
  useAppStore.getState().updateToolCall("command", { status: "failed", finishedAt: 10, seq: 20 }, "conv", undefined, "assistant");
  const current = useAppStore.getState().messages[0];
  expect(projectMessagesToTurns([current], true)).toEqual(projectMessagesToTurns([{ ...current }], true));
});

it("rebuilds progress when its visibility or provider classification changes", () => {
  const message = assistant();
  message.blocks = [...message.blocks!.filter((block) => block.type === "tool_call"),
    { type: "progress", id: "provider:operation", stage: "tool", status: "running", message: "working", timestamp: 1 },
  ];
  useAppStore.setState({ conversationId: "conv", messages: [message], conversationMessages: {}, conversationStreaming: {}, isStreaming: true });
  expect(projectMessagesToTurns([message], true)[0].committedCells.some((cell) => cell.id === "provider:operation")).toBe(true);
  useAppStore.getState().upsertMessageProgress({ id: "provider:operation", stage: "tool", status: "running", message: "private", visibility: "debug" }, "conv", "assistant");
  expect(projectMessagesToTurns(useAppStore.getState().messages, true)[0].committedCells.some((cell) => cell.id === "provider:operation")).toBe(false);
  useAppStore.getState().upsertMessageProgress({ id: "provider:operation", stage: "tool", status: "running", message: "retry", visibility: "timeline", retryAttempt: 1 }, "conv", "assistant");
  expect(projectMessagesToTurns(useAppStore.getState().messages, true)[0].committedCells.some((cell) => cell.id === "provider:operation")).toBe(false);
});

it("updates thinking without reconstructing completed tool projections", () => {
  const message = assistant();
  useAppStore.setState({ conversationId: "conv", messages: [message], conversationMessages: {}, conversationStreaming: {}, isStreaming: true });
  const before = projectMessagesToTurns([message], true)[0];
  const first = message.blocks![0] as Extract<ContentBlock, { type: "tool_call" }>;
  Object.defineProperty(first.record, "args", { get() { throw new Error("completed tool was projected again"); } });
  useAppStore.getState().appendThinkingChunk(" more", "conv", { source: "provider", item_id: "reasoning" }, "assistant");
  const after = projectMessagesToTurns(useAppStore.getState().messages, true)[0];
  expect(after.committedCells[0]).toBe(before.committedCells[0]);
  expect(after.committedCells.at(-1)).toMatchObject({ kind: "thinking", content: "Investigating more" });
});

it("updates tool progress without reconstructing completed tool projections", () => {
  const message = assistant();
  message.blocks = [
    ...message.blocks!.filter((block) => block.type === "tool_call"),
    { type: "progress", id: "tool-progress", stage: "tool", status: "running", message: "working", timestamp: 1 },
  ];
  useAppStore.setState({ conversationId: "conv", messages: [message], conversationMessages: {}, conversationStreaming: {}, isStreaming: true });
  const before = projectMessagesToTurns([message], true)[0];
  useAppStore.getState().upsertMessageProgress({ id: "tool-progress", stage: "tool", status: "running", message: "working more", timestamp: 2 }, "conv", "assistant");
  const after = projectMessagesToTurns(useAppStore.getState().messages, true)[0];
  expect(after.committedCells[0]).toBe(before.committedCells[0]);
  expect(after.committedCells.at(-1)).toMatchObject({ kind: "activity", id: "tool-progress", progress: { text: "working more" } });
});

it("updates command output while retaining unrelated tool cells", () => {
  const message = assistant();
  message.blocks = [
    ...message.blocks!.filter((block) => block.type === "tool_call"),
    { type: "tool_call", record: { id: "command", name: "run_command", activityKind: "commandExecution", args: { command: "pytest" }, status: "running", startedAt: 301 } },
  ];
  useAppStore.setState({ conversationId: "conv", messages: [message], conversationMessages: {}, conversationStreaming: {}, isStreaming: true });
  const before = projectMessagesToTurns([message], true)[0];
  useAppStore.getState().updateToolCall("command", { stdoutPreview: "result", outputPreview: "result", seq: 1 }, "conv", undefined, "assistant");
  const after = projectMessagesToTurns(useAppStore.getState().messages, true)[0];
  expect(after.committedCells[0]).toBe(before.committedCells[0]);
  expect(after.committedCells.at(-1)).toMatchObject({ kind: "exec", id: "command", stdoutFull: "result" });
});

it("preserves rendered prose across resource updates and stream settlement", () => {
  const content = "## Heading\n\nFirst **paragraph**.\n\nLast paragraph.";
  const view = render(<MarkdownRenderer content={content} isStreaming knownFilePaths={["a.py"]} />);
  const heading = view.container.querySelector("h2");
  const strong = view.container.querySelector("strong");
  view.rerender(<MarkdownRenderer content={content} isStreaming knownFilePaths={["a.py", "b.py"]} />);
  expect(view.container.querySelector("strong")).toBe(strong);
  view.rerender(<MarkdownRenderer content={content} isStreaming={false} knownFilePaths={["a.py", "b.py"]} />);
  expect(view.container.querySelector("strong")).toBe(strong);
  expect(view.container.querySelector("h2")).toBe(heading);
});

it("summarizes older failures while leaving running work visible", () => {
  const message = assistant();
  message.blocks = message.blocks!.filter((block) => block.type === "tool_call");
  for (const block of message.blocks) if (block.type === "tool_call") block.record.status = "failed";
  const first = message.blocks[0] as Extract<ContentBlock, { type: "tool_call" }>;
  first.record.status = "running";
  const cells = projectMessagesToTurns([message], true)[0].committedCells;
  render(<AgentTimeline cells={cells} isRunning renderCell={({ key, cell }) => <div key={key} data-testid="cell">{cell.id}</div>} />);
  expect(screen.getAllByTestId("cell")).toHaveLength(41);
  expect(screen.getByText("t0")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /显示更早的操作.*失败/ }));
  expect(screen.getAllByTestId("cell")).toHaveLength(81);
});
