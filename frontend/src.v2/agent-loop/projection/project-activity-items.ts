import type { ChatMessage } from "../../stores/types";
import { getContentBlocks } from "../../lib/content-blocks";
import { displayScopeOf, requiresAttention } from "../../lib/display-intent";
import { projectTurn, type TurnActivityItem } from "../../lib/turn-projection";
import type { ActivityItem, ActivityItemKind, ActivityItemStatus } from "../activity-item";

export interface ProjectMessageActivityOptions {
  turnId?: string;
  isStreaming?: boolean;
}

export function projectMessageActivityItems(
  message: ChatMessage,
  options: ProjectMessageActivityOptions = {},
): ActivityItem[] {
  if (message.role !== "assistant") return [];
  const projection = projectTurn(getContentBlocks(message), {
    isStreaming: options.isStreaming ?? Boolean(message.isStreaming),
    isThinkingStreaming: Boolean(message.isThinkingStreaming),
    terminalStatus: message.terminalStatus === "failed" ? "failed" : message.terminalStatus === "completed" ? "completed" : undefined,
    sourceCount: message.citations?.length ?? 0,
    artifactCount: message.artifacts?.length ?? 0,
    includeHiddenActivity: true,
  });

  return projection.activityItems.map((legacy) => projectLegacyActivityItem(legacy, message.id, options.turnId));
}

export function projectMessagesToActivityItems(
  messages: ChatMessage[],
  options: { isStreaming?: boolean } = {},
): ActivityItem[] {
  let lastAssistantIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === "assistant") {
      lastAssistantIndex = index;
      break;
    }
  }
  return messages.flatMap((message, index) => projectMessageActivityItems(message, {
    isStreaming: Boolean(options.isStreaming && index === lastAssistantIndex) || Boolean(message.isStreaming),
  }));
}

export function visibleActivityItems(items: ActivityItem[], limit = 6): ActivityItem[] {
  const seenGroups = new Set<string>();
  const visible: ActivityItem[] = [];
  for (let index = items.length - 1; index >= 0 && visible.length < limit; index -= 1) {
    const item = items[index];
    if (!item || item.visibility === "developer" || item.kind === "reasoning") continue;
    const key = item.groupKey || item.id;
    if (seenGroups.has(key)) continue;
    seenGroups.add(key);
    visible.push(item);
  }
  return visible.reverse();
}

export function projectLegacyActivityItem(
  legacy: TurnActivityItem,
  messageId?: string,
  turnId?: string,
): ActivityItem {
  const kind = activityKind(legacy);
  const records = legacy.records ?? [];
  const recordTaskId = records.find((record) => record.taskId)?.taskId;
  const recordTurnId = records.find((record) => record.turnId)?.turnId;
  const recordSeqs = records.map((record) => record.seq).filter((seq): seq is number => typeof seq === "number");
  const recordTitle = legacy.records?.length === 1
    ? legacy.records[0]?.displaySummary || legacy.records[0]?.summary
    : undefined;
  return {
    id: legacy.id,
    taskId: recordTaskId,
    turnId: turnId ?? recordTurnId,
    seq: recordSeqs.length ? Math.min(...recordSeqs) : undefined,
    messageId,
    kind,
    status: activityStatus(legacy.status),
    phase: activityPhase(legacy),
    title: legacy.title || recordTitle || legacy.summary || legacy.content || activityKindLabel(kind),
    summary: legacy.summary && legacy.summary !== legacy.title ? legacy.summary : undefined,
    startedAt: legacy.startedAt,
    finishedAt: legacy.finishedAt,
    durationMs: legacy.durationMs,
    visibility: activityVisibility(legacy),
    groupKey: activityGroupKey(kind, legacy),
    hasFailure: legacy.hasFailure,
    hasPendingUserAction: legacy.hasPendingUserAction,
  };
}

function activityVisibility(item: TurnActivityItem): ActivityItem["visibility"] {
  if (item.kind === "skill") return "developer";
  const routables = item.records?.length
    ? item.records
    : item.progress?.length
      ? item.progress
      : item.blocks;
  if (routables.some(requiresAttention)) return "main";
  const scopes = routables.map(displayScopeOf).filter(Boolean);
  if (scopes.some((scope) => scope === "chat" || scope === "notice")) return "main";
  if (scopes.some((scope) => scope === "activity")) return "activity";
  if (scopes.some((scope) => scope === "silent" || scope === "inspector")) return "developer";
  if (item.blocks.some((block) => "visibility" in block && block.visibility === "debug")) return "developer";
  return "main";
}

function activityKind(item: TurnActivityItem): ActivityItemKind {
  switch (item.kind) {
    case "reasoning":
    case "providerReasoning":
    case "processNote":
      return "reasoning";
    case "planning":
      return "plan";
    case "agentMessage":
      return "agent";
    case "commandExecution":
      return "command";
    case "fileChange":
      return "file_change";
    case "progress":
      if (item.progress?.some((entry) => entry.stage === "approval")) return "approval";
      if (item.progress?.some((entry) => entry.stage === "planning")) return "plan";
      return "system";
    default:
      return "tool";
  }
}

function activityStatus(status: TurnActivityItem["status"]): ActivityItemStatus {
  switch (status) {
    case "pending": return "queued";
    case "partial": return "completed";
    case "timeout": return "failed";
    default: return status;
  }
}

function activityPhase(item: TurnActivityItem): ActivityItem["phase"] {
  if (item.kind === "planning") return "planning";
  if (item.kind === "webSearch" || item.kind === "workspaceSearch" || item.kind === "fileRead") return "researching";
  if (item.kind === "fileChange" || item.kind === "commandExecution") return "implementing";
  if (item.progress?.some((entry) => entry.stage === "verification")) return "verifying";
  if (item.progress?.some((entry) => entry.stage === "final")) return "finalizing";
  return undefined;
}

function activityGroupKey(kind: ActivityItemKind, item: TurnActivityItem): string {
  if (kind === "tool" && item.records?.[0]?.name) return `tool:${item.records[0].name}`;
  return kind;
}

function activityKindLabel(kind: ActivityItemKind): string {
  switch (kind) {
    case "command": return "Command";
    case "file_change": return "File changes";
    case "approval": return "Approval";
    case "agent": return "Agent activity";
    case "plan": return "Plan";
    case "reasoning": return "Working";
    case "system": return "Status";
    default: return "Tool activity";
  }
}
