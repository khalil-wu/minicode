import type { StateCreator } from "zustand";
import type { AppStore, ApprovalSlice } from "./types";
import { promptTargetsConversation, visibleDiffReviewForConversation } from "./shared-helpers";
import { loadPromptDrafts, persistPromptDrafts, promptDraftKey, removePromptDrafts } from "./prompt-drafts";

export const createApprovalSlice: StateCreator<AppStore, [], [], ApprovalSlice> = (set, get) => ({
  promptDrafts: loadPromptDrafts(),
  updatePromptDraft: ({ requestId, conversationId, inputOwner }, patch) => set((s) => {
    const key = promptDraftKey({ requestId, conversationId, inputOwner });
    return { promptDrafts: persistPromptDrafts({
      ...s.promptDrafts,
      [key]: { ...s.promptDrafts[key], ...patch, requestId, conversationId, ...(inputOwner ? { inputOwner } : {}) },
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
        s.pendingApproval?.requestId === a.requestId ||
        s.approvalQueue.some((queued) => queued.requestId === a.requestId)
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
  markApprovalSubmitted: (requestId) =>
    set((s) => ({
      pendingApproval: s.pendingApproval?.requestId === requestId
        ? { ...s.pendingApproval, status: "submitted", error: undefined }
        : s.pendingApproval,
      approvalQueue: s.approvalQueue.map((queued) =>
        queued.requestId === requestId
          ? { ...queued, status: "submitted", error: undefined }
          : queued,
      ),
    })),
  markApprovalError: (requestId, error) =>
    set((s) => ({
      pendingApproval: s.pendingApproval?.requestId === requestId
        ? { ...s.pendingApproval, status: "error", error }
        : s.pendingApproval,
      approvalQueue: s.approvalQueue.map((queued) =>
        queued.requestId === requestId
          ? { ...queued, status: "error", error }
          : queued,
      ),
    })),
  clearApproval: (requestId) =>
    set((s) => {
      if (requestId && s.pendingApproval?.requestId !== requestId) {
        return {
          promptDrafts: removePromptDrafts(s.promptDrafts, [requestId]),
          approvalQueue: s.approvalQueue.filter((queued) => queued.requestId !== requestId),
        };
      }
      const [next, ...rest] = s.approvalQueue;
      return {
        promptDrafts: removePromptDrafts(s.promptDrafts, [requestId ?? s.pendingApproval?.requestId ?? ""]),
        pendingApproval: next ?? null,
        approvalQueue: rest,
      };
    }),
  clearApprovals: (requestIds) =>
    set((s) => {
      if (requestIds.length === 0) return s;
      const ids = new Set(requestIds);
      const pendingApproval = s.pendingApproval && ids.has(s.pendingApproval.requestId)
        ? null
        : s.pendingApproval;
      const approvalQueue = s.approvalQueue.filter((queued) => !ids.has(queued.requestId));
      const [next, ...rest] = approvalQueue;
      return {
        promptDrafts: removePromptDrafts(s.promptDrafts, requestIds),
        pendingApproval: pendingApproval ?? next ?? null,
        approvalQueue: pendingApproval ? approvalQueue : rest,
      };
    }),
  setDiffReview: (d) =>
    set((s) => {
      if (
        s.pendingDiffReview?.requestId === d.requestId ||
        s.diffReviewQueue.some((queued) => queued.requestId === d.requestId)
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
  clearDiffReview: (requestId) =>
    set((s) => {
      if (requestId && s.pendingDiffReview?.requestId !== requestId) {
        const nextQueue = s.diffReviewQueue.filter((queued) => queued.requestId !== requestId);
        return {
          diffReviewQueue: nextQueue,
          ...(s.diffReview?.requestId === requestId
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
  clearDiffReviews: (requestIds) =>
    set((s) => {
      if (requestIds.length === 0) return s;
      const ids = new Set(requestIds);
      const pending = s.pendingDiffReview && ids.has(s.pendingDiffReview.requestId)
        ? null
        : s.pendingDiffReview;
      const queue = s.diffReviewQueue.filter((queued) => !ids.has(queued.requestId));
      if (pending) {
        return {
          pendingDiffReview: pending,
          diffReviewQueue: queue,
          ...(s.diffReview && ids.has(s.diffReview.requestId)
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
  clearAskUser: (requestId, conversationId, inputOwner) =>
    set((s) => {
      const matches = (request: typeof s.pendingAskUser) => request !== null && request.requestId === requestId
        && (conversationId === undefined || request.conversationId === conversationId)
        && (inputOwner === undefined || request.turnId === inputOwner.turnId
          && request.messageId === inputOwner.messageId && request.taskId === inputOwner.taskId);
      if (requestId && !matches(s.pendingAskUser)) {
        return {
          promptDrafts: removePromptDrafts(s.promptDrafts, [requestId], conversationId, inputOwner),
          askUserQueue: s.askUserQueue.filter((queued) => !matches(queued)),
        };
      }
      const [next, ...rest] = s.askUserQueue;
      return { pendingAskUser: next ?? null, askUserQueue: rest,
        promptDrafts: removePromptDrafts(s.promptDrafts, [requestId ?? s.pendingAskUser?.requestId ?? ""], conversationId, inputOwner) };
    }),
  clearAskUsers: (requestIds, conversationId) =>
    set((s) => {
      if (requestIds.length === 0) return s;
      const ids = new Set(requestIds);
      const matches = (request: { requestId: string; conversationId?: string }) => ids.has(request.requestId)
        && (conversationId === undefined || request.conversationId === conversationId);
      const pending = s.pendingAskUser && matches(s.pendingAskUser)
        ? null
        : s.pendingAskUser;
      const queue = s.askUserQueue.filter((queued) => !matches(queued));
      const promptDrafts = removePromptDrafts(s.promptDrafts, requestIds, conversationId);
      if (pending) return { pendingAskUser: pending, askUserQueue: queue, promptDrafts };
      const [next, ...rest] = queue;
      return { pendingAskUser: next ?? null, askUserQueue: rest, promptDrafts };
    }),
});
