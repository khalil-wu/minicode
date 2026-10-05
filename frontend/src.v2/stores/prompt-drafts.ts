import { safeJsonParse } from "../lib/safe-parse";
import { readLS, writeLS } from "./shared-helpers";
import type { AgentPromptDraft } from "./types";

const STORAGE_KEY = "minicode.agentPromptDrafts";

export const promptDraftKey = (request: { requestId: string; conversationId?: string }) =>
  JSON.stringify([request.conversationId ?? "", request.requestId]);

export const loadPromptDrafts = (): Record<string, AgentPromptDraft> =>
  safeJsonParse<Record<string, AgentPromptDraft>>(readLS(STORAGE_KEY) ?? "{}", {});

export const persistPromptDrafts = (drafts: Record<string, AgentPromptDraft>) => {
  writeLS(STORAGE_KEY, JSON.stringify(drafts));
  return drafts;
};

export const removePromptDrafts = (drafts: Record<string, AgentPromptDraft>, requestIds: string[]) =>
  persistPromptDrafts(Object.fromEntries(
    Object.entries(drafts).filter(([, draft]) => !requestIds.includes(draft.requestId)),
  ));
