import type { RuntimeSessionSnapshot } from "../protocol/events";

type PendingApproval = NonNullable<RuntimeSessionSnapshot["pending_approvals"]>[number];

const pendingLabel = (
  pending: PendingApproval | null | undefined,
  fallbackCount?: number,
): string | null => {
  if (pending?.subtype === "elicitation") return "等待回复";
  if (pending?.tool_name) return `等待 ${pending.tool_name}`;
  if (pending?.type === "control_request") return "等待批准";
  if (Number(fallbackCount ?? 0) > 0) return "等待批准";
  return null;
};

export const hasRuntimePendingUserAction = (
  runtimeSession: RuntimeSessionSnapshot | null | undefined,
): boolean => {
  if (!runtimeSession) return false;
  const count = Number(runtimeSession.pending_approval_count ?? 0);
  if (count > 0) return true;
  return (runtimeSession.pending_approvals ?? []).length > 0;
};

export const runtimePendingUserActionLabel = (
  runtimeSession: RuntimeSessionSnapshot | null | undefined,
): string | null => {
  if (!runtimeSession) return null;
  const pending = runtimeSession.pending_approvals?.[0];
  return pendingLabel(pending, runtimeSession.pending_approval_count);
};

const pendingApprovalsForConversation = (
  runtimeSession: RuntimeSessionSnapshot | null | undefined,
  conversationId: string | null | undefined,
): PendingApproval[] => {
  if (!runtimeSession || !conversationId) return [];
  const pending = runtimeSession.pending_approvals ?? [];
  const scoped = pending.filter((item) => item.conversation_id === conversationId);
  if (scoped.length > 0) return scoped;
  if (pending.some((item) => item.conversation_id)) return [];
  if (runtimeSession.active_conversation_id && runtimeSession.active_conversation_id !== conversationId) return [];
  return pending;
};

export const hasRuntimePendingUserActionForConversation = (
  runtimeSession: RuntimeSessionSnapshot | null | undefined,
  conversationId: string | null | undefined,
): boolean => {
  if (!runtimeSession || !conversationId) return false;
  const pending = pendingApprovalsForConversation(runtimeSession, conversationId);
  if (pending.length > 0) return true;
  if (runtimeSession.pending_approvals?.some((item) => item.conversation_id)) return false;
  if (runtimeSession.active_conversation_id && runtimeSession.active_conversation_id !== conversationId) return false;
  return Number(runtimeSession.pending_approval_count ?? 0) > 0;
};

export const runtimePendingUserActionLabelForConversation = (
  runtimeSession: RuntimeSessionSnapshot | null | undefined,
  conversationId: string | null | undefined,
): string | null => {
  if (!runtimeSession || !conversationId) return null;
  const pending = pendingApprovalsForConversation(runtimeSession, conversationId)[0];
  if (pending) return pendingLabel(pending);
  if (!hasRuntimePendingUserActionForConversation(runtimeSession, conversationId)) return null;
  return pendingLabel(null, runtimeSession.pending_approval_count);
};

/** Sidebar projection: one pass over approvals, independent of progress fields.
 * An empty key retains the existing unscoped, no-active-thread contract. */
const pendingActionLabels = new WeakMap<RuntimeSessionSnapshot, ReadonlyMap<string, string | null>>();
const noPendingActionLabels: ReadonlyMap<string, string | null> = new Map();
export const runtimePendingUserActionLabels = (
  runtimeSession: RuntimeSessionSnapshot | null | undefined,
): ReadonlyMap<string, string | null> => {
  if (!runtimeSession) return noPendingActionLabels;
  const cached = pendingActionLabels.get(runtimeSession);
  if (cached) return cached;
  const labels = new Map<string, string | null>();
  const pending = runtimeSession.pending_approvals ?? [];
  if (pending.some(item => item.conversation_id)) {
    for (const item of pending) {
      if (item.conversation_id && !labels.has(item.conversation_id)) labels.set(item.conversation_id, pendingLabel(item));
    }
  } else {
    const label = pending.length ? pendingLabel(pending[0]) : pendingLabel(null, runtimeSession.pending_approval_count);
    if (label) labels.set(runtimeSession.active_conversation_id || "", label);
  }
  pendingActionLabels.set(runtimeSession, labels);
  return labels;
};
