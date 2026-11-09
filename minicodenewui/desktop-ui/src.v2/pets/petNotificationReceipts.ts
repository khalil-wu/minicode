import type { CompletionNotification } from "../protocol/notification-types";

const completions = new Map<string, { notification: CompletionNotification; identity: { turnId?: string; messageId?: string } }>();

export function recordPetCompletionNotification(conversationId: string, notification: CompletionNotification | null | undefined, identity: { turnId?: string; messageId?: string }) {
  if (notification) completions.set(conversationId, { notification, identity });
  else completions.delete(conversationId);
  window.dispatchEvent(new Event("pets:activity-changed"));
}

export const petCompletionNotification = (conversationId: string) => completions.get(conversationId);
