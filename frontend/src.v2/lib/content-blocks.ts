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
    blocks.push({ type: "text", content: message.content });
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

export type RenderGroup =
  | { type: "thinking"; content: string }
  | { type: "text"; content: string }
  | { type: "todo_list"; records: ToolCallRecord[] }
  | { type: "progress_group"; records: Extract<ContentBlock, { type: "progress" }>[] }
  | { type: "tool_call_group"; records: ToolCallRecord[] };

export function groupBlocksForRender(blocks: ContentBlock[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  const findPreviousToolGroup = (): Extract<RenderGroup, { type: "tool_call_group" }> | null => {
    for (let i = groups.length - 1; i >= 0; i -= 1) {
      const group = groups[i];
      if (group.type === "tool_call_group") return group;
      if (group.type === "text" || group.type === "thinking") return null;
    }
    return null;
  };
  const findPreviousTodoGroup = (): Extract<RenderGroup, { type: "todo_list" }> | null => {
    for (let i = groups.length - 1; i >= 0; i -= 1) {
      const group = groups[i];
      if (group.type === "todo_list") return group;
      if (group.type === "text" || group.type === "thinking") return null;
    }
    return null;
  };

  for (const block of blocks) {
    if (block.type === "tool_call") {
      if (block.record.name === "ask_user") {
        continue;
      }
      if (block.record.name === "todo_write") {
        const last = groups[groups.length - 1];
        if (last && last.type === "todo_list") {
          last.records.push(block.record);
        } else {
          const previousTodoGroup = findPreviousTodoGroup();
          if (previousTodoGroup) {
            previousTodoGroup.records.push(block.record);
          } else {
            groups.push({ type: "todo_list", records: [block.record] });
          }
        }
        continue;
      }
      const last = groups[groups.length - 1];
      if (last && last.type === "tool_call_group") {
        last.records.push(block.record);
      } else {
        const previousToolGroup = findPreviousToolGroup();
        if (previousToolGroup) {
          previousToolGroup.records.push(block.record);
        } else {
          groups.push({ type: "tool_call_group", records: [block.record] });
        }
      }
    } else if (block.type === "thinking") {
      groups.push({ type: "thinking", content: block.content });
    } else if (block.type === "progress") {
      const last = groups[groups.length - 1];
      if (last && last.type === "progress_group") {
        last.records.push(block);
      } else {
        groups.push({ type: "progress_group", records: [block] });
      }
    } else {
      groups.push({ type: "text", content: block.content });
    }
  }
  return groups;
}
