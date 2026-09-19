import { beforeEach, expect, it, vi } from "vitest";
import type { ServerEvent } from "../protocol/events";
import { toolCallLocations } from "../lib/content-blocks";
import { streamingMessageUpdate } from "../lib/message-changes";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import { useAppStore } from "../stores";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { projectMessagesToTurns } from "./chatSurfaceState";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("./sendChatMessage", () => ({ resetSendDeduplication: vi.fn() }));

const buffer = { push() {}, flush() {}, destroy() {} };
const handlers = { textStreamBuffer: buffer, thinkingStreamBuffer: buffer };
const tool = (id: string, patch: Partial<ToolCallRecord> = {}): Extract<ContentBlock, { type: "tool_call" }> => ({
  type: "tool_call", record: { id, name: "run_command", args: { command: `echo ${id}` }, status: "running", startedAt: 1, turnId: "turn", iterationId: "iteration", ...patch },
});
const message = (blocks: ContentBlock[]): ChatMessage => ({ id: "assistant", role: "assistant", turnId: "turn", content: "", blocks, artifacts: [], timestamp: 1, isStreaming: true });
const current = () => useAppStore.getState().messages;
const project = () => projectMessagesToTurns(current(), true)[0];
const handle = (event: Record<string, unknown>) => handleChatStreamEvent({ message_id: "assistant", turn_id: "turn", iteration_id: "iteration", ...event } as ServerEvent, "conv", handlers);
const currentRecord = (id: string) => toolCallLocations(current(), id, "assistant")[0].record;
const seed = (blocks: ContentBlock[]) => useAppStore.setState({ messages: [message(blocks)] });

beforeEach(() => {
  useAppStore.setState({ conversationId: "conv", messages: [], conversationMessages: {}, conversationStreaming: { conv: true }, sideChats: {}, inspectorEntries: [], isStreaming: true });
});

it("does not rescan unrelated tools and retains immutable old snapshots", () => {
  const old = tool("old", { status: "success" });
  seed([old, ...Array.from({ length: 3000 }, (_, i) => tool(`t${i}`, { status: "success" })), tool("live")]);
  handle({ type: "tool_output_delta", id: "live", output: "one", seq: 1 });
  const before = project();
  const snapshot = current();
  const access = vi.fn(() => "old");
  Object.defineProperty(old.record, "id", { get: access });
  handle({ type: "tool_result", id: "live", summary: "Exit code: 0", status: "success", seq: 2 });
  const after = project();
  expect(access).not.toHaveBeenCalled();
  expect(after.committedCells[0]).toBe(before.committedCells[0]);
  expect(after.committedCells.at(-1)).toMatchObject({ kind: "exec", status: "success" });
  expect(toolCallLocations(snapshot, "live", "assistant")[0].record.status).toBe("running");
  expect(currentRecord("live").status).toBe("success");
});

it("acknowledges projected changes, memoizes repeat consumers, and preserves skipped updates", () => {
  seed([tool("first"), tool("second")]);
  project();
  useAppStore.getState().updateToolCall("first", { status: "success", summary: "done" }, "conv", undefined, "assistant");
  useAppStore.getState().updateToolCall("first", { stdoutPreview: "late output" }, "conv", undefined, "assistant");
  expect(streamingMessageUpdate(current()[0])?.changes.get(0)).toBe("tool");
  const first = project();
  expect(project()).toBe(first);
  expect(streamingMessageUpdate(current()[0])).toBeUndefined();
  useAppStore.getState().updateToolCall("second", { status: "failed" }, "conv", undefined, "assistant");
  expect(streamingMessageUpdate(current()[0])?.changes.size).toBe(1);
  const second = project();
  expect(second.committedCells[0]).toBe(first.committedCells[0]);
  expect(second.committedCells[0]).toMatchObject({ status: "success", stdoutFull: "late output" });
});

it.each(["run_command", "read_file", "grep_files", "web_fetch", "mcp__lookup"])("matches full projection through %s lifecycle transitions", (name) => {
  seed([tool("old", { status: "success" }), tool("target", { name, status: "pending" })]);
  project();
  const events = [
    { type: "tool_call", id: "target", name, args: { command: "npm test", path: "src/a.ts" }, status: "running" },
    { type: "tool_output_delta", id: "target", output: "output\n", stream: "stdout" },
    { type: "tool_result", id: "target", status: "partial", summary: "Part complete", display_summary: "已处理部分内容" },
    { type: "tool_result", id: "target", status: "failed", summary: "Failed", display_summary: "失败" },
    { type: "tool_result", id: "target", status: "success", summary: "Exit code: 0", display_summary: "完成" },
  ];
  events.forEach((event, index) => {
    const previous = project();
    handle({ ...event, seq: index + 1 });
    const projected = project();
    const full = projectMessagesToTurns([{ ...current()[0] }], true)[0];
    expect(projected).toEqual(full);
    expect(projected.committedCells[0]).toBe(previous.committedCells[0]);
  });
});

it("rebuilds visibility, classification and aggregate diffs and retains the answer", () => {
  seed([tool("target"), { type: "text", itemId: "answer", content: "Final answer", source: "model_final", isStreaming: false, status: "completed" }]);
  project();
  const patches: Partial<ToolCallRecord>[] = [
    { visibility: "debug" }, { visibility: "timeline" },
    { activityKind: "fileChange", status: "success", diff: { plus: 1, minus: 0, files: [{ path: "a.py", plus: 1, minus: 0, patch: "+fixed" }] } },
    { temporaryRemoved: true, diff: undefined },
  ];
  patches.forEach((patch) => {
    useAppStore.getState().updateToolCall("target", patch, "conv", undefined, "assistant");
    const projected = project();
    expect(projected).toEqual(projectMessagesToTurns([{ ...current()[0] }], true)[0]);
    expect(projected.finalAnswerCell?.markdownSource).toBe("Final answer");
  });
  expect(project().committedCells).toHaveLength(0);
});

it("invalidates file-link evidence when a read commits, while command status keeps it stable", () => {
  seed([tool("command"), tool("read", { name: "read_file", args: { file_path: "src/fixed.py" } })]);
  const before = project();
  handle({ type: "tool_result", id: "command", status: "success", summary: "Exit code: 0", seq: 1 });
  const commandDone = project();
  expect(commandDone.resourceKey).toBe(before.resourceKey);
  handle({ type: "tool_result", id: "read", status: "success", summary: "Read file", seq: 2 });
  expect(project().resourceKey).not.toBe(commandDone.resourceKey);
});

it("keeps duplicate IDs scoped and updates the correct row", () => {
  seed([tool("same", { stepId: "a" }), tool("same", { stepId: "b" })]);
  project();
  handle({ type: "tool_output_delta", id: "same", step_id: "b", output: "second", seq: 1 });
  const records = toolCallLocations(current(), "same", "assistant").map(({ record }) => record);
  expect(records[0].stdoutPreview).toBeUndefined();
  expect(records[1].stdoutPreview).toBe("second");
  expect(project().committedCells[0]).toMatchObject({ stdoutFull: "" });
  expect(project().committedCells[1]).toMatchObject({ stdoutFull: "second" });
  const snapshot = current();
  handle({ type: "tool_output_delta", id: "same", output: "ambiguous", seq: 2 });
  expect(current()).toBe(snapshot);
});

it("reindexes new message snapshots after history prepend and authoritative replacement", () => {
  seed([tool("live")]);
  handle({ type: "tool_output_delta", id: "live", output: "first", seq: 1 });
  const original = current()[0];
  const older: ChatMessage = { ...message([tool("live")]), id: "older", isStreaming: false };
  useAppStore.setState({ messages: [older, { ...original, blocks: [tool("earlier", { status: "success" }), ...original.blocks!] }] });
  handle({ type: "tool_output_delta", id: "live", output: " second", seq: 2 });
  expect(current()[0]).toBe(older);
  expect(currentRecord("live").stdoutPreview).toBe("first second");
  useAppStore.setState({ messages: [older, message([tool("live", { seq: 10, stdoutPreview: "restored" })])] });
  handle({ type: "tool_output_delta", id: "live", output: "obsolete", seq: 3 });
  expect(currentRecord("live").stdoutPreview).toBe("restored");
});

it.each([true, false])("deduplicates sequenced command output with explicit ID=%s", (explicitId) => {
  seed([tool("live")]);
  const event = { type: "command_output_chunk", ...(explicitId ? { tool_call_id: "live" } : {}), content: "one\n", stream: "stdout", seq: 4 };
  handle(event);
  handle(event);
  handle({ ...event, content: "older\n", seq: 3 });
  expect(currentRecord("live")).toMatchObject({ stdoutPreview: "one\n", outputPreview: "one\n", seq: 4 });
});

it("records a command-output scope migration once and rejects a second identity change", () => {
  seed([tool("live", { turnId: "old-turn" })]);
  handle({ type: "command_output_chunk", tool_call_id: "live", content: "one", seq: 2 });
  expect(currentRecord("live")).toMatchObject({ turnId: "turn", scopeMigrationCount: 1, seq: 2, stdoutPreview: "one" });
  handle({ type: "command_output_chunk", tool_call_id: "live", turn_id: "other-turn", content: "wrong", seq: 3 });
  expect(currentRecord("live").stdoutPreview).toBe("one");
});
