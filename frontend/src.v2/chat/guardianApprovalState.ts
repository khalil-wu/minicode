import { useSyncExternalStore } from "react";
import { useAppStore } from "../stores";
import type { GuardianAssessment } from "../protocol/guardian-types";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";

interface ApprovalState { approved: ReadonlySet<string>; pending: string | null; }
const empty: ApprovalState = { approved: new Set(), pending: null };
const states = new Map<string, ApprovalState>();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export const guardianApprovalState = (conversationId: string): ApprovalState => states.get(conversationId) ?? empty;
function publish(conversationId: string, state: ApprovalState): void { states.set(conversationId, state); listeners.forEach(listener => listener()); }

/** Source rJ/iJ: one in-flight operation and a set of recorded review IDs per thread. */
export function useGuardianApprovalState(conversationId: string): ApprovalState {
  return useSyncExternalStore(subscribe, () => guardianApprovalState(conversationId));
}

export async function approveGuardianDeniedAction(conversationId: string, review: GuardianAssessment): Promise<boolean> {
  if (guardianApprovalState(conversationId).pending !== null) return false;
  publish(conversationId, { ...guardianApprovalState(conversationId), pending: review.id });
  try {
    const result = await sendClientCommandAwaitResult({ type: "thread/approveGuardianDeniedAction", threadId: conversationId,
      conversation_id: conversationId, event: review }, "thread/approveGuardianDeniedAction", { silent: true });
    if (!commandResultSucceeded(result) || result.data?.approval_recorded !== true || result.data?.review_id !== review.id
      || result.data?.conversation_id !== conversationId) throw new Error(result.message || "Could not record auto-review approval");
    publish(conversationId, { ...guardianApprovalState(conversationId), approved: new Set([...guardianApprovalState(conversationId).approved, review.id]) });
    return true;
  } finally {
    publish(conversationId, { ...guardianApprovalState(conversationId), pending: null });
  }
}

/** Source rit: newest ten denied actions that still have an unapproved real event. */
export function recentGuardianDenials(conversationId: string, approved = guardianApprovalState(conversationId).approved): GuardianAssessment[] {
  const state = useAppStore.getState();
  const messages = state.conversationId === conversationId ? state.messages : state.conversationMessages[conversationId] ?? [];
  const reviews = messages.flatMap(message => (message.blocks ?? []).flatMap(block => block.type === "guardian_assessment"
    && block.review.status === "denied" && !approved.has(block.review.id) ? [block.review] : []));
  return reviews.reverse().slice(0, 10);
}
