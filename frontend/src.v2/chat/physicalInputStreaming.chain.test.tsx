/* @vitest-environment jsdom */
import { useMemo } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ChatMessage, ContentBlock } from "../stores/types";
import * as turnProjection from "../lib/turn-projection";
import { projectMessagesToTurns } from "./chatSurfaceState";
import type { ChatTurnState } from "./cells/cellTypes";
import { ChatTurn } from "./components/ChatTurn";
import { releaseConversationReadingState } from "./transcriptReadingState";

const owner = "physical-input-stream";
const user = (id: string): ChatMessage => ({ id, role: "user", content: id, timestamp: 1, artifacts: [] });
const read = (id: string): ContentBlock => ({ type: "tool_call", record: {
  id, name: "read_file", args: { file_path: `${id}.ts` }, status: "success", startedAt: 1,
} });
const input = (id = "admitted-user"): ContentBlock => ({ type: "user_input", userMessageId: id });
const text = (source = "model_final"): ContentBlock => ({ type: "text", itemId: "live-text", content: "Started",
  source, status: "in_progress", isStreaming: true });
const assistant = (blocks: ContentBlock[], extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "physical-answer", role: "assistant", content: "", timestamp: 2, artifacts: [], isStreaming: true, blocks, ...extra,
});
const seed = (message: ChatMessage, admitted = true) => useAppStore.setState({
  messages: [user("original-user"), message, ...(admitted ? [user("admitted-user")] : [])],
});
const rows = () => useAppStore.getState().messages;
const project = () => projectMessagesToTurns(rows(), true)[0];
const cold = () => projectMessagesToTurns(rows().map(message => ({ ...message })), true)[0];
const workCells = (turn: ChatTurnState) => turn.workItems!.flatMap(item => item.kind === "process" ? item.cells : []);

beforeEach(() => {
  vi.spyOn(Date, "now").mockReturnValue(10_000);
  releaseConversationReadingState(owner);
  useAppStore.setState({ conversationId: owner, messages: [], conversationMessages: {}, conversationStreaming: {},
    conversationHistoryPages: {}, isStreaming: true, reducedMotion: true, viewMode: "normal" });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); releaseConversationReadingState(owner); });

it("streams real store text into ChatTurn without reprojecting either side of admitted input", () => {
  seed(assistant([
    ...Array.from({ length: 64 }, (_, index) => read(`before-${index}`)), input(),
    ...Array.from({ length: 64 }, (_, index) => read(`after-${index}`)), text(),
  ]));
  const initial = project();
  let latest = initial;
  function Transcript() {
    const messages = useAppStore(state => state.messages);
    latest = useMemo(() => projectMessagesToTurns(messages, true)[0], [messages]);
    return <ChatTurn turn={latest} conversationId={owner} />;
  }
  render(<Transcript />);
  const fullProjection = vi.spyOn(turnProjection, "projectTurn");
  for (let index = 1; index <= 25; index += 1) {
    act(() => useAppStore.getState().appendAgentMessageDelta("live-text", ` word${index}`, owner, "physical-answer"));
    expect(latest.committedCells).toBe(initial.committedCells);
    expect(latest.workItems).toBe(initial.workItems);
    expect(latest.resourceKey).toBe(initial.resourceKey);
  }
  expect(fullProjection).not.toHaveBeenCalled();
  expect(screen.getByText(/Started word1 .*word25/)).toBeTruthy();
  expect(screen.getByText("admitted-user")).toBeTruthy();
  expect(workCells(latest)).toHaveLength(128);
  expect(workCells(latest).map(cell => cell.id)).toEqual(initial.committedCells.map(cell => cell.id));
  for (const button of screen.getAllByRole("button", { name: "已读取文件" })) fireEvent.click(button);
  expect(screen.getByTitle("before-0.ts")).toBeTruthy();
  expect(screen.getByTitle("after-63.ts")).toBeTruthy();
  expect(latest).toEqual(cold());
});

it.each(["pending", "commentary"])("updates %s narration only in its actual process section", source => {
  seed(assistant([read("before"), input(), read("after"), text(source)]));
  const initial = project();
  useAppStore.getState().appendAgentMessageDelta("live-text", " continued", owner, "physical-answer");
  const next = project();
  expect(next.workItems![0]).toBe(initial.workItems![0]);
  expect(next.workItems![1]).toBe(initial.workItems![1]);
  expect(next.workItems![2]).not.toBe(initial.workItems![2]);
  expect(workCells(next).at(-1)).toMatchObject({ kind: "thinking", content: "Started continued" });
  expect(next).toEqual(cold());
});

it.each([true, false])("applies batched progress, tool output and text with input loaded=%s", admitted => {
  seed(assistant([read("before"), input(), read("after"),
    { type: "progress", id: "progress", stage: "tool", status: "running", message: "working", timestamp: 1 },
    { type: "tool_call", record: { id: "command", name: "run_command", args: { command: "inspect" }, status: "running", startedAt: 2 } },
    text(),
  ]), admitted);
  const initial = project();
  const fullProjection = vi.spyOn(turnProjection, "projectTurn");
  useAppStore.getState().upsertMessageProgress({ id: "progress", stage: "tool", status: "running", message: "actual progress" }, owner, "physical-answer");
  useAppStore.getState().updateToolCall("command", { stdoutPreview: "actual stdout", seq: 1 }, owner, undefined, "physical-answer");
  useAppStore.getState().appendAgentMessageDelta("live-text", " continued", owner, "physical-answer");
  const next = project();
  expect(fullProjection).not.toHaveBeenCalled();
  expect(next.workItems![0]).toBe(initial.workItems![0]);
  expect(workCells(next).find(cell => cell.id === "progress")).toMatchObject({ progress: { text: "actual progress" } });
  expect(workCells(next).find(cell => cell.id === "command")).toMatchObject({ kind: "exec", stdoutFull: "actual stdout" });
  expect(next).toEqual(cold());
  useAppStore.getState().updateToolCall("before", { outputPreview: "older operation completed", seq: 1 }, owner, undefined, "physical-answer");
  const earlier = project();
  expect(earlier.workItems!.at(-1)).toBe(next.workItems!.at(-1));
  expect(workCells(earlier)[0]).toMatchObject({ toolCallRecords: [expect.objectContaining({ outputPreview: "older operation completed" })] });
  expect(earlier).toEqual(cold());
});

it("updates provider thinking after an input without replaying earlier tools", () => {
  seed(assistant([read("before"), input(), read("after"),
    { type: "thinking", content: "Investigating", source: "provider", item_id: "reasoning" },
  ], { isThinkingStreaming: true }));
  const initial = project();
  const fullProjection = vi.spyOn(turnProjection, "projectTurn");
  useAppStore.getState().appendThinkingChunk(" more", owner, { source: "provider", item_id: "reasoning" }, "physical-answer");
  const next = project();
  expect(fullProjection).not.toHaveBeenCalled();
  expect(next.workItems![0]).toBe(initial.workItems![0]);
  expect(workCells(next).at(-1)).toMatchObject({ kind: "thinking", content: "Investigating more" });
  expect(next).toEqual(cold());
});

it.each([true, false])("projects newly loaded or edited actual user input during an outstanding text receipt (%s)", admitted => {
  seed(assistant([read("before"), input(), read("after"), text()]), admitted);
  project();
  useAppStore.getState().appendAgentMessageDelta("live-text", " continued", owner, "physical-answer");
  const changedUser = { ...user("admitted-user"), content: "Current actual input", attachmentRefs: [
    { id: "upload", artifactId: "artifact", name: "evidence.txt", mediaType: "text/plain", sizeBytes: 10 },
  ] };
  useAppStore.setState({ messages: admitted ? rows().map(message => message.id === changedUser.id ? changedUser : message) : [...rows(), changedUser] });
  const next = project();
  expect(next.workItems![1]).toMatchObject({ kind: "user", cell: { content: "Current actual input", attachments: [{ name: "evidence.txt" }] } });
  expect(next.activeCell).toMatchObject({ partialMarkdown: "Started continued" });
  expect(next).toEqual(cold());
});

it("rebuilds new boundaries, multi-action tools and final settlement without losing real history", () => {
  seed(assistant([read("before"), input(), text()]));
  project();
  useAppStore.getState().appendAgentMessageDelta("live-text", " continued", owner, "physical-answer");
  project();
  useAppStore.getState().appendToolCallBlock({ id: "command", name: "run_command", args: { command: "cat a.ts && cat b.ts" },
    status: "running", startedAt: 3, commandActions: [{ type: "read", name: "a.ts", path: "a.ts", command: "cat a.ts" },
      { type: "read", name: "b.ts", path: "b.ts", command: "cat b.ts" }] }, owner, "physical-answer");
  project();
  useAppStore.getState().updateToolCall("command", { stdoutPreview: "actual two-file output", seq: 1 }, owner, undefined, "physical-answer");
  expect(project()).toEqual(cold());
  const nextRows = rows().map(message => message.role === "assistant" ? { ...message, blocks: [...message.blocks!, input("second-user"), read("last-tool")] } : message);
  useAppStore.setState({ messages: [...nextRows, user("second-user")] });
  const newBoundary = project();
  expect(newBoundary.workItems!.filter(item => item.kind === "user").map(item => item.id)).toEqual(["admitted-user", "second-user"]);
  useAppStore.getState().completeAgentMessage({ id: "final", text: "Authoritative final", source: "model_final", status: "completed" }, owner, undefined, "physical-answer");
  const settled = project();
  expect(settled.finalAnswerCell?.markdownSource).toBe("Authoritative final");
  expect(workCells(settled).some(cell => cell.id === "last-tool")).toBe(true);
  expect(settled).toEqual(cold());
});
