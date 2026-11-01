import type { ChatMessage, ContentBlock } from "../stores/types";
import type { ToolCallRecord } from "./tool-call-reducer";
import { safeJsonParse } from "./safe-parse";

type LegacyMessageFields = {
  thinking?: string;
  toolCalls?: ToolCallRecord[];
};

const legacyFields = (message: ChatMessage): LegacyMessageFields =>
  message as ChatMessage & LegacyMessageFields;

// Positions, not record snapshots: scope/status/seq always come from the
// current immutable message. Duplicate IDs retain every candidate.
const messagePositions = new WeakMap<ChatMessage[], Map<string, number[]>>();
const toolPositions = new WeakMap<ChatMessage, Map<string, number[]>>();

export function messageIndices(messages: ChatMessage[], id: string): readonly number[] {
  let positions = messagePositions.get(messages);
  if (!positions) {
    positions = new Map();
    messages.forEach((message, index) => {
      const indices = positions!.get(message.id);
      if (indices) indices.push(index);
      else positions!.set(message.id, [index]);
    });
    messagePositions.set(messages, positions);
  }
  return positions.get(id) ?? [];
}

export function inheritMessagePositions(previous: ChatMessage[], next: ChatMessage[]): void {
  const positions = messagePositions.get(previous);
  if (positions) messagePositions.set(next, positions);
}

export function inheritToolPositions(previous: ChatMessage, next: ChatMessage): void {
  const positions = toolPositions.get(previous);
  if (positions) toolPositions.set(next, positions);
}

export function toolCallLocations(messages: ChatMessage[], id: string, messageId?: string): Array<{
  messageIndex: number; blockIndex: number; record: ToolCallRecord;
}> {
  const candidates: Array<{ messageIndex: number; blockIndex: number; record: ToolCallRecord }> = [];
  const visit = (message: ChatMessage, messageIndex: number) => {
    const blocks = getContentBlocks(message);
    let positions = toolPositions.get(message);
    if (!positions) {
      positions = new Map();
      blocks.forEach((block, index) => {
        if (block.type !== "tool_call") return;
        const indices = positions!.get(block.record.id);
        if (indices) indices.push(index);
        else positions!.set(block.record.id, [index]);
      });
      toolPositions.set(message, positions);
    }
    for (const blockIndex of positions.get(id) ?? []) {
      const block = blocks[blockIndex] as Extract<ContentBlock, { type: "tool_call" }>;
      candidates.push({ messageIndex, blockIndex, record: block.record });
    }
  };
  if (messageId) {
    for (const index of messageIndices(messages, messageId)) visit(messages[index], index);
  } else {
    messages.forEach(visit);
  }
  return candidates;
}

export function stripLegacyContentFields(message: ChatMessage): ChatMessage {
  const { thinking: _thinking, toolCalls: _toolCalls, ...rest } =
    message as ChatMessage & LegacyMessageFields;
  return rest;
}

export function getContentBlocks(message: ChatMessage): ContentBlock[] {
  if (message.blocks) {
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

/** A successful exec receipt is the published ownership of a yielded cell. */
export function hasRunningCodeCellReceipt(message: ChatMessage, source: ToolCallRecord["callSource"]): boolean {
  if (source?.kind !== "code_mode" || !source.cell_id || !source.parent_call_id) return false;
  return getToolCallsFromMessage(message).some(parent => parent.id === source.parent_call_id
    && parent.name === "tool_exec" && parent.status === "success"
    && [parent.summary, parent.outputPreview, parent.contentPreview].some(content => {
      const receipt = content ? safeJsonParse<{ cell_id?: string; status?: string } | null>(content, null) : null;
      return receipt?.cell_id === source.cell_id && receipt.status === "running";
    }));
}

export function isCodeCellToolFact(
  message: ChatMessage,
  incoming: Pick<ToolCallRecord, "id" | "turnId" | "callSource">,
  kind: "start" | "update",
): boolean {
  if (message.role !== "assistant" || !incoming.turnId || (message.turnId && incoming.turnId !== message.turnId)
    || (message.terminalStatus && message.terminalStatus !== "completed")
    || !hasRunningCodeCellReceipt(message, incoming.callSource)) return false;
  const parent = getToolCallsFromMessage(message).find(record => record.id === incoming.callSource!.parent_call_id);
  if (parent?.turnId !== incoming.turnId) return false;
  const calls = getToolCallsFromMessage(message).filter(record => record.id === incoming.id);
  if (calls.length === 0) return kind === "start";
  if (calls.length !== 1) return false;
  const existing = calls[0];
  return existing.turnId === incoming.turnId && existing.callSource?.kind === "code_mode"
    && existing.callSource.cell_id === incoming.callSource!.cell_id
    && existing.callSource.parent_call_id === incoming.callSource!.parent_call_id
    && existing.callSource.runtime_call_id === incoming.callSource!.runtime_call_id;
}

/** An idle ownership snapshot stops presentation; it is not a tool result. */
export function getPresentedContentBlocks(message: ChatMessage, blocks = getContentBlocks(message)): ContentBlock[] {
  if (message.isStreaming !== false || message.isThinkingStreaming) return blocks;
  return blocks.map((block): ContentBlock => {
    if (block.type === "tool_call" && (block.record.status === "running" || block.record.status === "pending")
      && !isCodeCellToolFact(message, block.record, "update")) {
      return { ...block, record: { ...block.record, status: "partial" } };
    }
    if ((block.type === "progress" || block.type === "process") && block.status === "running") {
      return { ...block, status: "partial" };
    }
    if (block.type === "text" && block.isStreaming) {
      return { ...block, isStreaming: false, status: "partial" };
    }
    return block;
  });
}

export function getThinkingFromMessage(message: ChatMessage): string {
  if (message.blocks) {
    return message.blocks
      .filter((block): block is Extract<ContentBlock, { type: "thinking" }> => block.type === "thinking")
      .map((block) => block.content)
      .join("");
  }
  return legacyFields(message).thinking ?? "";
}

export function getToolCallsFromMessage(message: ChatMessage): ToolCallRecord[] {
  if (message.blocks) {
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

/** Typed activity snapshots may carry an answer text block without lifecycle metadata. */
export function isLegacyTextBlock(block: ContentBlock): boolean {
  return block.type === "text"
    && !block.itemId
    && block.status == null
    && block.isStreaming == null
    && Boolean(block.content.trim());
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
  // Each item already merges its own content parts; the latest visible
  // completed item owns the answer.
  for (let index = blocks.length - 1; index >= 0; index--) {
    const block = blocks[index];
    if (block.type === "text" && isFinalAnswerBlock(block)) return block.content;
  }
  return "";
}
