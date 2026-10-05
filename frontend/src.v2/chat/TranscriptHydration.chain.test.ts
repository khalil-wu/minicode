// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { hydrateMessages, normalizeContentBlocks, type BackendTranscriptMessage } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { getContentBlocks, getThinkingFromMessage, getToolCallsFromMessage } from "../lib/content-blocks";

describe("canonical transcript schema boundaries", () => {
  it.each(["partial", "timeout", "cancelled", "blocked"])("preserves typed %s status across legacy standalone tool results in either order", (status) => {
    const call: BackendTranscriptMessage = { id: "assistant", role: "assistant", content: "", blocks: [
      { type: "tool_call", record: { id: "call", name: "web_fetch", args: {}, status } },
    ] };
    const result = { id: "result", role: "tool", content: "retained output", tool_call_id: "call", name: "web_fetch", is_error: true } as BackendTranscriptMessage;
    for (const transcript of [[call, result], [result, call]]) {
      const messages = hydrateMessages(transcript);
      expect(messages).toHaveLength(1);
      expect(getToolCallsFromMessage(messages[0])[0]).toMatchObject({ status, outputPreview: "retained output" });
    }
  });

  it.each(["partial", "timeout", "cancelled", "blocked", "failed"])("restores an explicit standalone %s outcome in either order", (status) => {
    const call: BackendTranscriptMessage = { id: "assistant", role: "assistant", content: "", blocks: [
      { type: "tool_call", record: { id: "call", name: "web_fetch", args: {}, status: "running" } },
    ] };
    const result = { id: "result", role: "tool", content: "terminal output", tool_call_id: "call", name: "web_fetch", status, is_error: false } as BackendTranscriptMessage;
    for (const transcript of [[call, result], [result, call]]) {
      const messages = hydrateMessages(transcript);
      expect(getToolCallsFromMessage(messages[0])[0]).toMatchObject({ status, outputPreview: "terminal output" });
    }
  });
  it.each(["empty", "hidden-reasoning", "retracted-process"])("keeps an explicit %s block schema from becoming a legacy final answer", (kind) => {
    const blocks = kind === "empty" ? []
      : kind === "hidden-reasoning" ? [{ type: "thinking", content: "hidden", visibility: "hidden", provider_reasoning_type: "encrypted" }]
      : [{ type: "process", id: "removed", itemKind: "process_text", content: "removed", status: "retracted" }];
    const messages = hydrateMessages([
      { id: "user", role: "user", content: "question" },
      { id: "assistant", role: "assistant", content: "compatibility envelope only", blocks, terminal_status: "completed" },
    ]);
    expect(messages[1].blocks).toEqual([]);
    expect(getContentBlocks(messages[1])).toEqual([]);
    expect(projectMessagesToTurns(messages, false)[0].finalAnswerCell).toBeNull();
  });

  it("still upgrades a pre-block legacy assistant schema", () => {
    const [message] = hydrateMessages([{ id: "legacy", role: "assistant", content: "legacy final" }]);
    expect(getContentBlocks(message)[0]).toMatchObject({ type: "text", source: "model_final", content: "legacy final" });
  });

  it.each(["partial", "failed", "interrupted", "completed"])("retains an empty durable %s assistant turn after reload", (status) => {
    const messages = hydrateMessages([
      { id: "user", role: "user", content: "question" },
      { id: "assistant", role: "assistant", content: "", blocks: [], terminal_status: status, completed_at: 8 },
    ]);
    expect(messages.map((message) => message.id)).toEqual(["user", "assistant"]);
    expect(projectMessagesToTurns(messages, false)[0].status).toBe(status);
  });

  it("retains transcript position while merging duplicate snapshots of one tool call", () => {
    const blocks = normalizeContentBlocks([
      { type: "tool_call", transcriptIndex: 3, record: { id: "call", name: "read_file", args: {}, status: "running", startedAt: 1 } },
      { type: "tool_call", transcriptIndex: 4, record: { id: "call", name: "read_file", args: {}, status: "success", startedAt: 2, outputPreview: "raw output" } },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks?.[0]).toMatchObject({ transcriptIndex: 3, record: { status: "success", outputPreview: "raw output", startedAt: 1 } });
  });

  it("keeps raw tool output identical when its legacy result precedes the call", () => {
    const [message] = hydrateMessages([
      { id: "result", role: "tool", content: "raw output", tool_call_id: "call", name: "read_file" } as BackendTranscriptMessage,
      { id: "assistant", role: "assistant", content: "", blocks: [
        { type: "tool_call", record: { id: "call", name: "read_file", args: {}, startedAt: 1 } },
      ] },
    ]);
    expect(message.blocks?.[0]).toMatchObject({ type: "tool_call", record: { outputPreview: "raw output", status: "success" } });
  });

  it("does not restore old legacy thinking or calls from an explicit empty schema", () => {
    const [message] = hydrateMessages([{ id: "assistant", role: "assistant", content: "envelope", blocks: [], terminal_status: "completed" }]);
    const legacy = Object.assign(message, { thinking: "old thought", toolCalls: [{ id: "old", name: "read_file", args: {}, status: "success", startedAt: 1 }] });
    expect(getThinkingFromMessage(legacy)).toBe("");
    expect(getToolCallsFromMessage(legacy)).toEqual([]);
  });
});
