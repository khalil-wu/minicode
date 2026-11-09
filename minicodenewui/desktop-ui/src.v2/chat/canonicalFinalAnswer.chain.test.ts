// @vitest-environment jsdom
import { beforeEach, expect, it } from "vitest";
import { useAppStore } from "../stores";
import { getAnswerTextFromBlocks } from "../lib/content-blocks";
import type { ContentBlock } from "../stores/types";
const owner = "conv_canonical_answer";
beforeEach(() => useAppStore.setState({ conversationId: owner, workingDirectory: "/canonical", conversationMessages: {}, messages: [{ id: "answer-owner", role: "assistant", content: "", blocks: [], timestamp: 1, isStreaming: true }], isStreaming: true }));
const answer = (itemId: string, content: string): ContentBlock => ({ type: "text", itemId, content, source: "model_final", status: "completed", isStreaming: false });
it("keeps independent final item records but exposes the same latest answer to the live store and resume snapshot", () => {
  const state = useAppStore.getState();
  state.startAgentMessage("first", owner, "answer-owner", "model_final");
  state.completeAgentMessage({ id: "first", text: "First", source: "model_final", status: "completed" }, owner, undefined, "answer-owner");
  state.startAgentMessage("second", owner, "answer-owner", "model_final");
  state.appendAgentMessageDelta("second", "Sec", owner, "answer-owner");
  state.appendAgentMessageDelta("second", "ond", owner, "answer-owner");
  state.completeAgentMessage({ id: "second", text: "Second", source: "model_final", status: "completed" }, owner, undefined, "answer-owner");
  const completed = useAppStore.getState().messages[0];
  expect(completed.content).toBe("Second");
  expect(completed.blocks?.filter(block => block.type === "text").map(block => block.content)).toEqual(["First", "Second"]);
  state.resumeStreaming(owner, [], "answer-owner", "turn", [answer("first", "First"), answer("second", "Second")]);
  expect(useAppStore.getState().messages[0].content).toBe("Second");
});
it("does not erase visible completed text for an empty/hidden-only item, matching Codex last_agent_message facts", () => {
  expect(getAnswerTextFromBlocks([answer("first", "First"), answer("metadata", "")])).toBe("First");
  expect(getAnswerTextFromBlocks([answer("image-only", "")])).toBe("");
});
