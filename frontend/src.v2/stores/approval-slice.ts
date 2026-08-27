import type { StateCreator } from "zustand";
import type { AppStore, ApprovalSlice } from "./types";
import { promptTargetsConversation, visibleDiffReviewForConversation } from "./shared-helpers";

export const createApprovalSlice: StateCreator<AppStore, [], [], ApprovalSlice> = (set, get) => ({
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
          approvalQueue: s.approvalQueue.filter((queued) => queued.requestId !== requestId),
        };
      }
      const [next, ...rest] = s.approvalQueue;
      return {
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
        s.pendingAskUser?.requestId === a.requestId ||
        s.askUserQueue.some((queued) => queued.requestId === a.requestId)
      ) {
        return s;
      }
      if (!s.pendingAskUser) return { pendingAskUser: a };
      return { askUserQueue: [...s.askUserQueue, a] };
    }),
  clearAskUser: (requestId) =>
    set((s) => {
      if (requestId && s.pendingAskUser?.requestId !== requestId) {
        return {
          askUserQueue: s.askUserQueue.filter((queued) => queued.requestId !== requestId),
        };
      }
      const [next, ...rest] = s.askUserQueue;
      return { pendingAskUser: next ?? null, askUserQueue: rest };
    }),
  clearAskUsers: (requestIds) =>
    set((s) => {
      if (requestIds.length === 0) return s;
      const ids = new Set(requestIds);
      const pending = s.pendingAskUser && ids.has(s.pendingAskUser.requestId)
        ? null
        : s.pendingAskUser;
      const queue = s.askUserQueue.filter((queued) => !ids.has(queued.requestId));
      if (pending) return { pendingAskUser: pending, askUserQueue: queue };
      const [next, ...rest] = queue;
      return { pendingAskUser: next ?? null, askUserQueue: rest };
    }),
});
