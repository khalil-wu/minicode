import type { StateCreator } from "zustand";
import type { AppStore, ApprovalSlice } from "./types";
import { promptTargetsConversation, visibleDiffReviewForConversation } from "./shared-helpers";
import { loadPromptDrafts, persistPromptDrafts, promptDraftKey, removePromptDrafts } from "./prompt-drafts";
import { pendingPromptKey, pendingPromptMatchesOwner } from "../lib/pending-prompts";
import type { AgentPromptDraft, PendingApproval } from "./types";
import type { PromptOwner } from "../protocol/prompt-responses";

const removeApprovalDrafts = (removed: PendingApproval[]) => {
  for (const request of removed) {
    removePromptDrafts([request.requestId], request.conversationId,
      { turnId: request.turnId, messageId: request.messageId, taskId: request.taskId });
  }
  return loadPromptDrafts();
};

export const createApprovalSlice: StateCreator<AppStore, [], [], ApprovalSlice> = (set, get) => ({
  promptDrafts: loadPromptDrafts(),
  updatePromptDraft: ({ requestId, conversationId, inputOwner }, patch) => set(() => {
    const key = promptDraftKey({ requestId, conversationId, inputOwner });
    // Read the shared storage before writing: another window may have drafted
    // for a different request since this store loaded.
    const latest = loadPromptDrafts();
    return { promptDrafts: persistPromptDrafts({
      ...latest,
      [key]: { ...latest[key], ...patch, requestId, conversationId, ...(inputOwner ? { inputOwner } : {}) },
    }) };
  }),
  pendingApproval: null,
  approvalQueue: [],
  pendingDiffReview: null,
  diffReviewQueue: [],
  pendingAskUser: null,
  askUserQueue: [],
  setApproval: (a) =>
    set((s) => {
      if (
        [s.pendingApproval, ...s.approvalQueue].some(request => request && pendingPromptKey(request) === pendingPromptKey(a))
      ) {
        return s;
      }
      if (!s.pendingApproval) {
        return { pendingApproval: a };
      }
      // Never silently drop a pending approval: dropping one leaves the backend
      // tool blocked forever waiting on a response the user can no longer give.
      return { approvalQueue: [...s.approvalQueue, a] };
    }),
  markApprovalSubmitted: (requestId, owner) =>
    set((s) => ({
      pendingApproval: s.pendingApproval?.requestId === requestId && pendingPromptMatchesOwner(s.pendingApproval, owner)
        ? { ...s.pendingApproval, status: "submitted", error: undefined }
        : s.pendingApproval,
      approvalQueue: s.approvalQueue.map((queued) =>
        queued.requestId === requestId && pendingPromptMatchesOwner(queued, owner)
          ? { ...queued, status: "submitted", error: undefined }
          : queued,
      ),
    })),
  markApprovalError: (requestId, error, owner) =>
    set((s) => ({
      pendingApproval: s.pendingApproval?.requestId === requestId && pendingPromptMatchesOwner(s.pendingApproval, owner)
        ? { ...s.pendingApproval, status: "error", error }
        : s.pendingApproval,
      approvalQueue: s.approvalQueue.map((queued) =>
        queued.requestId === requestId && pendingPromptMatchesOwner(queued, owner)
          ? { ...queued, status: "error", error }
          : queued,
      ),
    })),
  clearApproval: (requestId, owner) =>
    set((s) => {
      const matches = (request: PendingApproval | null) => request?.requestId === requestId && pendingPromptMatchesOwner(request, owner);
      if (requestId && !matches(s.pendingApproval)) {
        const remaining = s.approvalQueue.filter(item => !matches(item));
        return {
          promptDrafts: removeApprovalDrafts(s.approvalQueue.filter(matches)),
          approvalQueue: remaining,
        };
      }
      const [next, ...rest] = s.approvalQueue;
      return {
        promptDrafts: removeApprovalDrafts(s.pendingApproval ? [s.pendingApproval] : []),
        pendingApproval: next ?? null,
        approvalQueue: rest,
      };
    }),
  clearApprovals: (requestIds, owner) =>
    set((s) => {
      if (requestIds.length === 0) return s;
      const ids = new Set(requestIds);
      const matches = (request: PendingApproval) => ids.has(request.requestId) && pendingPromptMatchesOwner(request, owner);
      const pendingApproval = s.pendingApproval && matches(s.pendingApproval)
        ? null
        : s.pendingApproval;
      const all = [s.pendingApproval, ...s.approvalQueue].filter((item): item is PendingApproval => item !== null);
      const approvalQueue = s.approvalQueue.filter((queued) => !matches(queued));
      const [next, ...rest] = approvalQueue;
      return {
        promptDrafts: removeApprovalDrafts(all.filter(matches)),
        pendingApproval: pendingApproval ?? next ?? null,
        approvalQueue: pendingApproval ? approvalQueue : rest,
      };
    }),
  setDiffReview: (d) =>
    set((s) => {
      if (
        [s.pendingDiffReview, ...s.diffReviewQueue].some(request => request && pendingPromptKey(request) === pendingPromptKey(d))
      ) {
        return s;
      }
      if (!s.pendingDiffReview) {
        return {
          pendingDiffReview: d,
          ...(d.reviewState && promptTargetsConversation(d, s.conversationId)
            ? { diffReview: d.reviewState }
            : {}),
        };
      }
      return {
        diffReviewQueue: [...s.diffReviewQueue, d],
        ...(d.reviewState
          && promptTargetsConversation(d, s.conversationId)
          && !promptTargetsConversation(s.pendingDiffReview, s.conversationId)
          ? { diffReview: d.reviewState }
          : {}),
      };
    }),
  updateDiffReviewStatus: (requestId, patch, owner) => set(s => {
    const matches = (request: (PromptOwner & { requestId: string }) | null) => request?.requestId === requestId && pendingPromptMatchesOwner(request, owner);
    const update = (request: NonNullable<typeof s.pendingDiffReview>) => matches(request) && request.reviewState
      ? { ...request, reviewState: { ...request.reviewState, ...patch } } : request;
    return {
      pendingDiffReview: s.pendingDiffReview ? update(s.pendingDiffReview) : null,
      diffReviewQueue: s.diffReviewQueue.map(update),
      diffReview: matches(s.diffReview) ? { ...s.diffReview!, ...patch } : s.diffReview,
    };
  }),
  clearDiffReview: (requestId, owner) =>
    set((s) => {
      const matches = (request: typeof s.pendingDiffReview) => request?.requestId === requestId && pendingPromptMatchesOwner(request, owner);
      if (requestId && !matches(s.pendingDiffReview)) {
        const nextQueue = s.diffReviewQueue.filter((queued) => !matches(queued));
        return {
          diffReviewQueue: nextQueue,
          ...(matches(s.diffReview)
            ? { diffReview: visibleDiffReviewForConversation(s.conversationId, s.pendingDiffReview, nextQueue) }
            : {}),
        };
      }
      const [next, ...rest] = s.diffReviewQueue;
      return {
        pendingDiffReview: next ?? null,
        diffReviewQueue: rest,
        diffReview: visibleDiffReviewForConversation(s.conversationId, next ?? null, rest),
      };
    }),
  clearDiffReviews: (requestIds, owner) =>
    set((s) => {
      if (requestIds.length === 0) return s;
      const ids = new Set(requestIds);
      const matches = (request: { requestId: string; conversationId?: string; turnId?: string; messageId?: string; taskId?: string }) => ids.has(request.requestId) && pendingPromptMatchesOwner(request, owner);
      const pending = s.pendingDiffReview && matches(s.pendingDiffReview)
        ? null
        : s.pendingDiffReview;
      const queue = s.diffReviewQueue.filter((queued) => !matches(queued));
      if (pending) {
        return {
          pendingDiffReview: pending,
          diffReviewQueue: queue,
          ...(s.diffReview && matches(s.diffReview)
            ? { diffReview: visibleDiffReviewForConversation(s.conversationId, pending, queue) }
            : {}),
        };
      }
      const [next, ...rest] = queue;
      return {
        pendingDiffReview: next ?? null,
        diffReviewQueue: rest,
        diffReview: visibleDiffReviewForConversation(s.conversationId, next ?? null, rest),
      };
    }),
  setAskUser: (a) =>
    set((s) => {
      if (
        [s.pendingAskUser, ...s.askUserQueue].some(request => request?.requestId === a.requestId
          && request.conversationId === a.conversationId && request.turnId === a.turnId
          && request.messageId === a.messageId && request.taskId === a.taskId)
      ) {
        return s;
      }
      if (!s.pendingAskUser) return { pendingAskUser: a };
      return { askUserQueue: [...s.askUserQueue, a] };
    }),
  updateAskUserStatus: (requestId, patch, owner) => set(s => {
    const matches = (request: typeof s.pendingAskUser) => request?.requestId === requestId && pendingPromptMatchesOwner(request, owner);
    return {
      pendingAskUser: s.pendingAskUser && matches(s.pendingAskUser) ? { ...s.pendingAskUser, ...patch } : s.pendingAskUser,
      askUserQueue: s.askUserQueue.map(request => matches(request) ? { ...request, ...patch } : request),
    };
  }),
  clearAskUser: (requestId, conversationId, inputOwner) =>
    set((s) => {
      const matches = (request: typeof s.pendingAskUser) => request !== null && request.requestId === requestId
        && (conversationId === undefined || request.conversationId === conversationId)
        && (inputOwner === undefined || request.turnId === inputOwner.turnId
          && request.messageId === inputOwner.messageId && request.taskId === inputOwner.taskId);
      if (requestId && !matches(s.pendingAskUser)) {
        return {
          promptDrafts: removePromptDrafts([requestId], conversationId, inputOwner),
          askUserQueue: s.askUserQueue.filter((queued) => !matches(queued)),
        };
      }
      const [next, ...rest] = s.askUserQueue;
      return { pendingAskUser: next ?? null, askUserQueue: rest,
        promptDrafts: removePromptDrafts([requestId ?? s.pendingAskUser?.requestId ?? ""], conversationId, inputOwner) };
    }),
  clearAskUsers: (requestIds, conversationId, inputOwner) =>
    set((s) => {
      if (requestIds.length === 0) return s;
      const ids = new Set(requestIds);
      const matches = (request: { requestId: string; conversationId?: string }) => ids.has(request.requestId)
        && (conversationId === undefined || request.conversationId === conversationId) && pendingPromptMatchesOwner(request, inputOwner);
      const pending = s.pendingAskUser && matches(s.pendingAskUser)
        ? null
        : s.pendingAskUser;
      const queue = s.askUserQueue.filter((queued) => !matches(queued));
      const promptDrafts = removePromptDrafts(requestIds, conversationId, inputOwner);
      if (pending) return { pendingAskUser: pending, askUserQueue: queue, promptDrafts };
      const [next, ...rest] = queue;
      return { pendingAskUser: next ?? null, askUserQueue: rest, promptDrafts };
    }),
});
