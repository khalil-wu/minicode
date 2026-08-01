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
  const targetId = normalizeId(targetConversationId);
  // A prompt without an owner is unscoped. Never project it into whichever
  // conversation happens to be active; the backend must attach the owner (or
  // the caller must render it in a generic/unowned surface).
  void activeConversationId;
  return Boolean(promptConversationId && targetId && promptConversationId === targetId);
};

export const hasLocalPendingPromptForConversation = (
  prompts: PendingPrompt[],
  targetConversationId: string | null | undefined,
  activeConversationId: string | null | undefined,
): boolean =>
  prompts.some((prompt) => pendingPromptTargetsConversation(prompt, targetConversationId, activeConversationId));
