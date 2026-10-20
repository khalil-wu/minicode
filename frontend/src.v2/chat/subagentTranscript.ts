import type { ChatMessage, ContentBlock } from "../stores/types";
import type { SubagentProgressEvent, SubagentTranscriptEventsDelta } from "../protocol/streaming-types";
import { getToolCallsFromMessage, isCodeCellToolFact } from "../lib/content-blocks";
import { mergeToolDelegations, type ToolDelegation } from "../lib/tool-call-reducer";
import { hydrateMessages, toToolCallRecord } from "./transcriptHydration";

export function applyToolDelegation(messages: ChatMessage[], messageId: string, toolCallId: string, fact: ToolDelegation): ChatMessage[] {
  const index = messages.findIndex(message => message.id === messageId);
  if (index < 0) return messages;
  const message = messages[index];
  if (!message.blocks?.some(block => block.type === "tool_call" && block.record.id === toolCallId)) return messages;
  const updated = messages.slice();
  updated[index] = { ...message, blocks: message.blocks.map(block => block.type === "tool_call" && block.record.id === toolCallId
    ? { ...block, record: { ...block.record, delegations: mergeToolDelegations(block.record.delegations, [fact]) } } : block) };
  return updated;
}

/** Cell envelopes keep their original execution metadata, even in one mailbox epoch. */
export function isHistoricCodeCellProgress(messages: ChatMessage[], event: SubagentProgressEvent): boolean {
  const source = event.call_source;
  if (source?.kind !== "code_mode") return false;
  const delta = event.transcript_delta;
  return messages.some(message => message.role === "assistant" && !message.isStreaming && !message.isThinkingStreaming
    && (getToolCallsFromMessage(message).some(record => record.id === source.parent_call_id || record.id === event.tool_call_id)
      || (delta?.kind === "events" && delta.events.some(fact => {
        const record = toToolCallRecord(fact.event_type === "tool_use" ? fact.payload.tool_call : fact.payload.record);
        return record?.callSource?.kind === "code_mode" && record.turnId === message.turnId;
      }))));
}

/** Apply published journal facts only. Missing ownership/offset means read the
 * authoritative child transcript; it must never advance the local cursor. */
export function applySubagentTranscriptEvents(messages: ChatMessage[], delta: SubagentTranscriptEventsDelta): ChatMessage[] | null {
  let next = messages.slice();
  let current = -1;
  for (let index = next.length - 1; index >= 0; index--) {
    if (next[index].role === "assistant" && next[index].isStreaming) { current = index; break; }
  }
  const setMessage = (message: ChatMessage) => { next[current] = message; };
  const upsertBlock = (block: ContentBlock, matches: (previous: ContentBlock) => boolean, index = current) => {
    const message = next[index];
    const blocks = [...(message.blocks ?? [])];
    const at = blocks.findIndex(matches);
    if (at >= 0) blocks[at] = block; else blocks.push(block);
    next[index] = { ...message, blocks };
  };
  for (const event of delta.events) {
    const payload = event.payload;
    if (event.event_type === "system" && payload.lifecycle === "subagent_delegation_updated") {
      const parentId = payload.parent_message_id as string;
      const parent = next.find(message => message.id === parentId);
      if (!parent?.blocks?.some(block => block.type === "tool_call" && block.record.id === payload.parent_tool_call_id)) return null;
      next = applyToolDelegation(next, parentId, payload.parent_tool_call_id as string, payload.delegation as ToolDelegation);
      continue;
    }
    if (event.event_type === "system" && payload.lifecycle === "turn_started") {
      const turnId = payload.turn_id as string;
      const id = `subagent-turn-${turnId || event.event_id}`;
      if (current >= 0 && next[current].id !== id) {
        next[current] = { ...next[current], isStreaming: false, isThinkingStreaming: false };
      }
      current = next.findIndex(message => message.id === id);
      if (current < 0) {
        current = next.length;
        next.push({ id, role: "assistant", content: "", blocks: [], artifacts: [], timestamp: event.ts_ms,
          turnId: turnId || id, isStreaming: true });
      }
      continue;
    }
    if (event.event_type === "user_prompt") {
      if (next.some(message => message.id === event.event_id)) continue;
      const user: ChatMessage = { id: event.event_id, role: "user", content: String(payload.content).trim(),
        artifacts: [], timestamp: event.ts_ms };
      if (current >= 0 && next[current].blocks?.length === 0) { next.splice(current, 0, user); current++; }
      else next.push(user);
      continue;
    }
    if (event.event_type === "tool_use" || event.event_type === "tool_result") {
      const incoming = toToolCallRecord(event.event_type === "tool_use" ? payload.tool_call : payload.record);
      const index = incoming?.callSource?.kind === "code_mode"
        ? next.findIndex(message => message.role === "assistant" && message.turnId === incoming.turnId)
        : current;
      if (index < 0) return null;
      const message = next[index];
      if ((index !== current || !message.isStreaming) && (!incoming
        || !isCodeCellToolFact(message, incoming, event.event_type === "tool_use" ? "start" : "update"))) return null;
      if (event.event_type === "tool_use") {
        if (!incoming) return null;
        upsertBlock({ type: "tool_call", record: incoming }, block => block.type === "tool_call" && block.record.id === incoming.id, index);
      } else {
        const old = message.blocks?.find(block => block.type === "tool_call" && block.record.id === payload.tool_call_id);
        if (!old || old.type !== "tool_call" || (incoming && incoming.id !== old.record.id)) return null;
        const record = toToolCallRecord({ ...old.record, ...(payload.record as object), args: old.record.args });
        if (!record) return null;
        record.startedAt = old.record.startedAt ?? record.startedAt;
        record.delegations = mergeToolDelegations(old.record.delegations, record.delegations);
        if (record.durationMs === undefined && record.finishedAt !== undefined && record.startedAt !== undefined) {
          record.durationMs = Math.max(0, record.finishedAt - record.startedAt);
        }
        upsertBlock({ type: "tool_call", record }, block => block.type === "tool_call" && block.record.id === record.id, index);
        if (record.supersededToolCallIds?.length) {
          next[index] = { ...next[index], blocks: next[index].blocks?.map(block => block.type === "tool_call" && record.supersededToolCallIds!.includes(block.record.id)
            ? { ...block, record: { ...block.record, temporaryRemoved: true, diff: undefined } } : block) };
        }
      }
      continue;
    }
    if (current < 0) return null;
    const message = next[current];
    if (event.event_type === "progress" || event.event_type === "assistant") {
      if (event.event_type === "assistant" && payload.message_only === true) {
        setMessage({ ...message, content: String(payload.content ?? "") });
        continue;
      }
      const itemId = String(payload.item_id || `${message.id}-final`);
      const old = message.blocks?.find(block => block.type === "text" && block.itemId === itemId);
      let content = String(payload.content ?? "");
      if (payload.content_delta !== undefined) {
        const previous = old?.type === "text" ? old.content : "";
        if (Array.from(previous).length !== payload.content_offset) return null;
        content = previous + String(payload.content_delta);
      }
      const block = hydrateMessages([{ id: message.id, role: "assistant", content: "", blocks: [{
        type: "text", item_id: itemId, content, source: payload.source || "pending", status: payload.status || "completed",
        is_streaming: payload.status !== "completed" && payload.status !== "partial" && payload.status !== "cancelled",
      }] }])[0].blocks![0];
      upsertBlock(block, previous => previous.type === "text" && previous.itemId === itemId);
      setMessage({ ...next[current], content });
    } else if (payload.lifecycle === "turn_diff_updated") {
      const hydrated = hydrateMessages([{ id: message.id, role: "assistant", content: "", metadata: {
        turn_diff: { ...payload, conversation_id: payload.thread_id, message_id: message.id, turn_id: message.turnId || payload.turn_id },
      } }])[0];
      setMessage({ ...message, turnDiff: hydrated.turnDiff });
    } else if (payload.transcript_only) {
      const itemId = String(payload.item_id || event.event_id);
      if (payload.status === "retracted" || ["hidden", "internal", "debug", "redacted"].includes(String(payload.visibility))) {
        setMessage({ ...message, blocks: message.blocks?.filter(block => block.type !== "process" || block.id !== itemId) });
        continue;
      }
      const block = hydrateMessages([{ id: message.id, role: "assistant", content: "", blocks: [{
        type: "process", id: itemId, item_kind: payload.kind || "process_text", content: payload.content,
        source: payload.source || "model_preamble", status: payload.status || "completed", visibility: payload.visibility || "timeline", timestamp: event.ts_ms,
      }] }])[0].blocks![0];
      upsertBlock(block, previous => previous.type === "process" && previous.id === itemId);
    } else if (payload.lifecycle === "provider_progress") {
      const state = String(payload.provider_state || "failed");
      const sourceId = String(payload.operation_id || event.event_id);
      const id = `${sourceId}:error:${payload.provider_span_id || ""}:${state}:${payload.retry_attempt || 0}`;
      const block: ContentBlock = { type: "progress", id, operationId: sourceId, stage: "status", phase: "recover",
        status: state === "failed" ? "failed" : "info", message: String(payload.message || payload.summary || ""), visibility: "timeline",
        providerState: state as "failed", errorMessage: String(payload.error_message || payload.message || payload.summary || ""),
        detail: payload.detail as string | undefined, retryAttempt: Number(payload.retry_attempt || 0), maxRetries: Number(payload.max_retries || 0),
        retryAfterMs: Number(payload.retry_after_ms || 0), timestamp: event.ts_ms };
      upsertBlock(block, previous => previous.type === "progress" && previous.id === id);
    } else if (payload.lifecycle === "error") {
      setMessage({ ...message, failureMessage: String(payload.message || "") });
    }
  }
  return next;
}
