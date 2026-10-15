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

export const removePromptDrafts = (drafts: Record<string, AgentPromptDraft>, requestIds: string[], conversationId?: string,
  inputOwner?: AgentPromptDraft["inputOwner"]) =>
  persistPromptDrafts(Object.fromEntries(
    Object.entries(drafts).filter(([, draft]) => !requestIds.includes(draft.requestId)
      || (conversationId !== undefined && draft.conversationId !== conversationId)
      || (inputOwner !== undefined && (draft.inputOwner?.turnId !== inputOwner.turnId
        || draft.inputOwner?.messageId !== inputOwner.messageId || draft.inputOwner?.taskId !== inputOwner.taskId))),
  ));
