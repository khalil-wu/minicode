import type { ChatMessage, ContentBlock } from "../stores/types";
import type { ToolCallRecord } from "./tool-call-reducer";

type LegacyMessageFields = {
  thinking?: string;
  toolCalls?: ToolCallRecord[];
};

const legacyFields = (message: ChatMessage): LegacyMessageFields =>
  message as ChatMessage & LegacyMessageFields;

export function stripLegacyContentFields(message: ChatMessage): ChatMessage {
  const { thinking: _thinking, toolCalls: _toolCalls, ...rest } =
    message as ChatMessage & LegacyMessageFields;
  return rest;
}

export function getContentBlocks(message: ChatMessage): ContentBlock[] {
  if (message.blocks && message.blocks.length > 0) {
    return message.blocks;
  }
  const legacy = legacyFields(message);
  const blocks: ContentBlock[] = [];
  if (legacy.thinking) {
    blocks.push({ type: "thinking", content: legacy.thinking });
  }
  for (const tc of legacy.toolCalls ?? []) {
    blocks.push({ type: "tool_call", record: tc });
  }
  if (message.content) {
    blocks.push({
      type: "text",
      itemId: "agent-message",
      content: message.content,
      source: "reply",
      status: "completed",
      isStreaming: false,
    });
  }
  return blocks;
}

export function getThinkingFromMessage(message: ChatMessage): string {
  if (message.blocks && message.blocks.length > 0) {
    return message.blocks
      .filter((block): block is Extract<ContentBlock, { type: "thinking" }> => block.type === "thinking")
      .map((block) => block.content)
      .join("");
  }
  return legacyFields(message).thinking ?? "";
}

export function getToolCallsFromMessage(message: ChatMessage): ToolCallRecord[] {
  if (message.blocks && message.blocks.length > 0) {
    return message.blocks
      .filter((block): block is Extract<ContentBlock, { type: "tool_call" }> => block.type === "tool_call")
      .map((block) => block.record);
  }
  return legacyFields(message).toolCalls ?? [];
}

export function isCompletedAgentMessageBlock(
  block: ContentBlock,
): boolean {
  return block.type === "text"
    && Boolean(block.itemId)
    && Boolean(block.content.trim())
    && block.isStreaming !== true
    && (block.status === "completed" || block.status === "partial");
}

export function isExplicitFinalAnswerSource(source: string | undefined): boolean {
  return Boolean(source && ["model_final", "reply", "recovery", "partial"].includes(source));
}

export function isFinalAnswerBlock(block: ContentBlock): boolean {
  if (!isCompletedAgentMessageBlock(block) || block.type !== "text") return false;
  // Typed provider commentary is an assistant-message item too, but it is not
  // part of the terminal answer. Legacy/replayed blocks may use reply or omit
  // source, so retain those as final-answer compatible.
  return !block.source || isExplicitFinalAnswerSource(block.source);
}

export function getAnswerTextFromBlocks(blocks: ContentBlock[]): string {
  return blocks
    .filter((block): block is Extract<ContentBlock, { type: "text" }> =>
      block.type === "text" && isFinalAnswerBlock(block),
    )
    .map((block) => block.content)
    .join("");
}
