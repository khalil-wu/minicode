import type { StateCreator } from "zustand";
import type { AppStore, ApprovalSlice } from "./types";

export const createApprovalSlice: StateCreator<AppStore, [], [], ApprovalSlice> = (set, get) => ({
  pendingApproval: null,
  approvalQueue: [],
  pendingDiffReview: null,
  pendingAskUser: null,
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
  setDiffReview: (d) => set({ pendingDiffReview: d }),
  clearDiffReview: () => set({ pendingDiffReview: null }),
  setAskUser: (a) => set({ pendingAskUser: a }),
  clearAskUser: () => set({ pendingAskUser: null }),
});
