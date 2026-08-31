import { describe, expect, it } from "vitest";
import { buildInterruptCommand, hasInterruptFence } from "./interrupt-command";

describe("interrupt command", () => {
  it("binds Stop to the concrete streaming turn and message", () => {
    const command = buildInterruptCommand({
      conversationId: "conv-1",
      messages: [
        { id: "assistant-old", role: "assistant", content: "done", artifacts: [], timestamp: 1 },
        {
          id: "assistant-live",
          turnId: "turn-live",
          role: "assistant",
          content: "",
          artifacts: [],
          timestamp: 2,
          isStreaming: true,
        },
      ],
      conversationMessages: {},
      sideChats: {},
    });

    expect(command).toEqual({
      type: "interrupt",
      conversation_id: "conv-1",
      turn_id: "turn-live",
      message_id: "assistant-live",
    });
    expect(hasInterruptFence(command)).toBe(true);
  });

  it("uses the requested background conversation instead of the active transcript", () => {
    const command = buildInterruptCommand({
      conversationId: "conv-active",
      messages: [],
      conversationMessages: {
        "conv-background": [{
          id: "assistant-background",
          role: "assistant",
          content: "",
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
        }],
      },
      sideChats: {},
    }, "conv-background");

    expect(command).toMatchObject({
      conversation_id: "conv-background",
      message_id: "assistant-background",
    });
  });
});
