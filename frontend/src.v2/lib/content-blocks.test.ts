import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../stores/types";
import {
  getAnswerTextFromBlocks,
  getContentBlocks,
  getThinkingFromMessage,
  getToolCallsFromMessage,
  isCompletedAgentMessageBlock,
  stripLegacyContentFields,
} from "./content-blocks";

const baseMessage = (overrides: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "message-1",
  role: "assistant",
  content: "",
  artifacts: [],
  timestamp: 1,
  ...overrides,
});

describe("content block helpers", () => {
  it("returns explicit blocks without regrouping them", () => {
    const blocks = [
      { type: "thinking" as const, content: "Think" },
      { type: "text" as const, content: "Answer", visibility: "final" },
    ];

    expect(getContentBlocks(baseMessage({ blocks }))).toBe(blocks);
  });

  it("hydrates legacy fields only when blocks are absent", () => {
    const message = baseMessage({ content: "Legacy answer" }) as ChatMessage & {
      thinking: string;
      toolCalls: Array<{ id: string; name: string; args: {}; status: "success"; startedAt: number }>;
    };
    message.thinking = "Legacy thought";
    message.toolCalls = [{ id: "tool-1", name: "tool", args: {}, status: "success", startedAt: 1 }];

    expect(getContentBlocks(message).map((block) => block.type)).toEqual(["thinking", "tool_call", "text"]);
    expect(getThinkingFromMessage(message)).toBe("Legacy thought");
    expect(getToolCallsFromMessage(message)).toHaveLength(1);
  });

  it("strips legacy duplicated fields", () => {
    const message = Object.assign(baseMessage(), { thinking: "old", toolCalls: [] });

    expect(stripLegacyContentFields(message)).not.toHaveProperty("thinking");
    expect(stripLegacyContentFields(message)).not.toHaveProperty("toolCalls");
  });

  it("recognizes completed agent-message items", () => {
    expect(isCompletedAgentMessageBlock({
      type: "text",
      itemId: "agent-message",
      content: "Done",
      status: "completed",
      isStreaming: false,
    })).toBe(true);
    expect(isCompletedAgentMessageBlock({
      type: "text",
      itemId: "agent-message",
      content: "Working",
      status: "partial",
      isStreaming: true,
    })).toBe(false);
    expect(isCompletedAgentMessageBlock({
      type: "text",
      itemId: "agent-message",
      content: "",
      status: "completed",
      isStreaming: false,
    })).toBe(false);
  });

  it("joins completed and partial agent-message items and ignores process text", () => {
    expect(getAnswerTextFromBlocks([
      { type: "text", itemId: "a", content: "A", status: "completed", isStreaming: false },
      { type: "process", id: "p", itemKind: "process_text", content: "working", status: "completed", timestamp: 1 },
      { type: "text", itemId: "b", content: "B", status: "partial", isStreaming: false },
    ])).toBe("AB");
  });
});
