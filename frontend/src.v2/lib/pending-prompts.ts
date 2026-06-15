import type { PendingApproval, PendingAskUser, PendingDiffReview } from "../stores/types";

type PendingPrompt = PendingApproval | PendingDiffReview | PendingAskUser | null | undefined;

const normalizeId = (value: string | null | undefined): string | undefined => {
  const trimmed = value?.trim();
  return trimmed || undefined;
};

export const pendingPromptTargetsConversation = (
  prompt: PendingPrompt,
  targetConversationId: string | null | undefined,
  activeConversationId: string | null | undefined,
): boolean => {
  if (!prompt) return false;
  const promptConversationId = normalizeId(prompt.conversationId);
  const targetId = normalizeId(targetConversationId) ?? normalizeId(activeConversationId);
  if (promptConversationId) return promptConversationId === targetId;

  const activeId = normalizeId(activeConversationId);
  if (!targetId) return true;
  return !activeId || targetId === activeId;
};

export const hasLocalPendingPromptForConversation = (
  prompts: PendingPrompt[],
  targetConversationId: string | null | undefined,
  activeConversationId: string | null | undefined,
): boolean =>
  prompts.some((prompt) => pendingPromptTargetsConversation(prompt, targetConversationId, activeConversationId));
