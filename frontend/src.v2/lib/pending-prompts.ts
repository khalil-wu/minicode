import type { PendingApproval, PendingAskUser, PendingDiffReview } from "../stores/types";
import type { PromptOwner } from "../protocol/prompt-responses";

type PendingPrompt = PendingApproval | PendingDiffReview | PendingAskUser | null | undefined;

export const pendingPromptKey = (prompt: NonNullable<PendingPrompt>) => JSON.stringify([
  prompt.conversationId, prompt.requestId, prompt.turnId, prompt.messageId, prompt.taskId,
]);

export const pendingPromptMatchesOwner = (prompt: PromptOwner | null | undefined, owner?: PromptOwner): boolean =>
  Boolean(prompt) && (!owner || (["conversationId", "turnId", "messageId", "taskId"] as const)
    .every(field => !Object.hasOwn(owner, field) || prompt![field] === owner[field]));

const normalizeId = (value: string | null | undefined): string | undefined => {
  const trimmed = value?.trim();
  return trimmed || undefined;
};

export const pendingPromptTargetsConversation = (
  prompt: PendingPrompt,
  targetConversationId: string | null | undefined,
): boolean => {
  if (!prompt) return false;
  const promptConversationId = normalizeId(prompt.conversationId);
  const targetId = normalizeId(targetConversationId);
  // A prompt without an owner is unscoped. Never project it into whichever
  // conversation happens to be active; the backend must attach the owner (or
  // the caller must render it in a generic/unowned surface).
  return Boolean(promptConversationId && targetId && promptConversationId === targetId);
};

export const hasLocalPendingPromptForConversation = (
  prompts: PendingPrompt[],
  targetConversationId: string | null | undefined,
): boolean =>
  prompts.some((prompt) => pendingPromptTargetsConversation(prompt, targetConversationId));
