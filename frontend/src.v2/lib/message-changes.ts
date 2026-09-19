import type { ChatMessage } from "../stores/types";
import { isProviderRequestProgress } from "./provider-progress";
import { inheritMessagePositions, inheritToolPositions } from "./content-blocks";
import { activityKindFromToolRecord } from "./turn-projection";

// These receipts are emitted by the store operation that owns the mutation.
// They retain one stable base, never a chain of every streamed message copy.
type ChangeKind = "text" | "thinking" | "progress" | "tool_output" | "tool";
const textUpdates = new WeakMap<ChatMessage, { base: ChatMessage; changes: ReadonlyMap<number, ChangeKind> }>();
const outputKeys = new Set(["outputPreview", "stdoutPreview", "stderrPreview", "seq"]);

function recordChange(previous: ChatMessage, next: ChatMessage, blockIndex: number, kind: ChangeKind): void {
  const existing = textUpdates.get(previous);
  const changes = new Map(existing?.changes);
  // A later stdout delta must not erase an unprojected result/args change.
  changes.set(blockIndex, changes.get(blockIndex) === "tool" ? "tool" : kind);
  textUpdates.set(next, { base: existing?.base ?? previous, changes });
  inheritToolPositions(previous, next);
}
const topologyKeys = new WeakMap<ChatMessage[], object>();

export const streamingMessageUpdate = (message: ChatMessage) => textUpdates.get(message);
export const acknowledgeMessageUpdate = (message: ChatMessage) => textUpdates.delete(message);

export function messageTopologyKey(messages: ChatMessage[]): object {
  let key = topologyKeys.get(messages);
  if (!key) { key = {}; topologyKeys.set(messages, key); }
  return key;
}

export function recordStreamingTextUpdate(previous: ChatMessage, next: ChatMessage, blockIndex: number): void {
  const before = previous.blocks?.[blockIndex];
  const after = next.blocks?.[blockIndex];
  if (!previous.isStreaming || previous.isThinkingStreaming
    || blockIndex !== (previous.blocks?.length ?? 0) - 1
    || before?.type !== "text" || after?.type !== "text"
    || !before.isStreaming || before.source !== after.source
    || !/[\p{L}\p{N}]/u.test(before.content)) return;
  recordChange(previous, next, blockIndex, "text");
}

export function recordStreamingThinkingUpdate(previous: ChatMessage, next: ChatMessage, blockIndex: number): void {
  const before = previous.blocks?.[blockIndex];
  const after = next.blocks?.[blockIndex];
  if (!previous.isThinkingStreaming || blockIndex !== (previous.blocks?.length ?? 0) - 1
    || before?.type !== "thinking" || after?.type !== "thinking" || !before.content.trim()) return;
  const keys = Object.keys(after) as Array<keyof typeof after>;
  if (keys.some((key) => key !== "content" && after[key] !== before[key])) return;
  recordChange(previous, next, blockIndex, "thinking");
}

export function recordStreamingProgressUpdate(previous: ChatMessage, next: ChatMessage, blockIndex: number): void {
  const before = previous.blocks?.[blockIndex];
  const after = next.blocks?.[blockIndex];
  if (!previous.isStreaming || blockIndex < 0 || before?.type !== "progress" || after?.type !== "progress"
    || before.id !== after.id || before.stage !== after.stage || before.toolCallId !== after.toolCallId
    || before.toolName !== after.toolName || before.groupId !== after.groupId || before.stepId !== after.stepId
    || before.iterationId !== after.iterationId || before.operationId !== after.operationId
    || before.providerState !== after.providerState || before.visibility !== after.visibility
    || before.phase !== after.phase || before.stage === "image_generation"
    || isProviderRequestProgress(before) !== isProviderRequestProgress(after)) return;
  recordChange(previous, next, blockIndex, "progress");
}

export function recordStreamingToolUpdate(previous: ChatMessage, next: ChatMessage, blockIndex: number): void {
  const before = previous.blocks?.[blockIndex];
  const after = next.blocks?.[blockIndex];
  if (before?.type !== "tool_call" || after?.type !== "tool_call" || before.record.id !== after.record.id) return;
  inheritToolPositions(previous, next);
  if (!previous.isStreaming) return;
  const keys = Object.keys(after.record) as Array<keyof typeof after.record>;
  const outputOnly = keys.every((key) => outputKeys.has(key) || after.record[key] === before.record[key]);
  if (outputOnly) { recordChange(previous, next, blockIndex, "tool_output"); return; }
  // These fields affect other cells: tool-name narration filtering,
  // visibility, aggregate file diffs, or the kind/number of process rows.
  const kind = activityKindFromToolRecord(after.record);
  if (before.record.name !== after.record.name || before.record.visibility !== after.record.visibility
    || before.record.temporaryRemoved !== after.record.temporaryRemoved
    || before.record.diff !== after.record.diff || kind === "fileChange"
    || kind !== activityKindFromToolRecord(before.record)) return;
  recordChange(previous, next, blockIndex, "tool");
}

export function inheritMessageTopology(previous: ChatMessage[], next: ChatMessage[]): void {
  topologyKeys.set(next, messageTopologyKey(previous));
  inheritMessagePositions(previous, next);
}
