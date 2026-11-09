import { safeJsonParse } from "../lib/safe-parse";
import { readLS, writeLS } from "./shared-helpers";
import type { AgentPromptDraft } from "./types";

const STORAGE_KEY = "minicode.agentPromptDrafts";

export const promptDraftKey = (request: Pick<AgentPromptDraft, "requestId" | "conversationId" | "inputOwner">) =>
  JSON.stringify(request.inputOwner
    ? [request.conversationId ?? "", request.requestId, request.inputOwner.turnId, request.inputOwner.messageId, request.inputOwner.taskId]
    : [request.conversationId ?? "", request.requestId]);

export const loadPromptDrafts = (): Record<string, AgentPromptDraft> =>
  safeJsonParse<Record<string, AgentPromptDraft>>(readLS(STORAGE_KEY) ?? "{}", {});

export const persistPromptDrafts = (drafts: Record<string, AgentPromptDraft>) => {
  writeLS(STORAGE_KEY, JSON.stringify(drafts));
  return drafts;
};

export const removePromptDrafts = (requestIds: string[], conversationId?: string,
  inputOwner?: AgentPromptDraft["inputOwner"]) =>
  // Scope the removal to the shared storage's latest contents so entries
  // another window drafted for a different request are never replaced by a
  // stale local map.
  persistPromptDrafts(Object.fromEntries(
    Object.entries(loadPromptDrafts()).filter(([, draft]) => !requestIds.includes(draft.requestId)
      || (conversationId !== undefined && draft.conversationId !== conversationId)
      || (inputOwner !== undefined && (draft.inputOwner === undefined || draft.inputOwner.turnId !== inputOwner.turnId
        || draft.inputOwner?.messageId !== inputOwner.messageId || draft.inputOwner?.taskId !== inputOwner.taskId))),
  ));
