import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "./index";
import { conversationResetPayload } from "./shared-helpers";

describe("approval queue", () => {
  beforeEach(() => {
    useAppStore.setState({
      conversationId: "conv-queue",
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      diffReview: null,
      pendingAskUser: null,
      askUserQueue: [],
    });
  });

  it("deduplicates repeated approval requests", () => {
    const store = useAppStore.getState();
    const approval = { requestId: "approval-1", toolName: "git_status", args: {} };

    store.setApproval(approval);
    store.setApproval(approval);

    expect(useAppStore.getState().pendingApproval?.requestId).toBe("approval-1");
    expect(useAppStore.getState().approvalQueue).toHaveLength(0);
  });

  it("promotes queued approvals after clearing the current approval", () => {
    const store = useAppStore.getState();

    store.setApproval({ requestId: "approval-1", toolName: "run_command", args: {} });
    store.setApproval({ requestId: "approval-2", toolName: "git_status", args: {} });
    store.setApproval({ requestId: "approval-3", toolName: "run_command", args: {} });

    expect(useAppStore.getState().pendingApproval?.requestId).toBe("approval-1");
    expect(useAppStore.getState().approvalQueue.map((item) => item.requestId)).toEqual([
      "approval-2",
      "approval-3",
    ]);

    store.clearApproval("approval-1");

    expect(useAppStore.getState().pendingApproval?.requestId).toBe("approval-2");
    expect(useAppStore.getState().approvalQueue.map((item) => item.requestId)).toEqual([
      "approval-3",
    ]);
  });

  it("can remove a queued approval by id without dropping the active approval", () => {
    const store = useAppStore.getState();

    store.setApproval({ requestId: "approval-1", toolName: "run_command", args: {} });
    store.setApproval({ requestId: "approval-2", toolName: "git_status", args: {} });
    store.clearApproval("approval-2");

    expect(useAppStore.getState().pendingApproval?.requestId).toBe("approval-1");
    expect(useAppStore.getState().approvalQueue).toHaveLength(0);
  });

  it("queues, deduplicates, and advances diff reviews with their detailed state", () => {
    const store = useAppStore.getState();
    const first = {
      requestId: "diff-1",
      diff: "-old\n+first",
      reviewState: {
        requestId: "diff-1",
        toolName: "write_file",
        diff: "-old\n+first",
        files: [],
        status: "pending" as const,
        mode: "approval" as const,
        fileDecisions: {},
        lineComments: [],
      },
    };
    const second = {
      requestId: "diff-2",
      diff: "-old\n+second",
      reviewState: {
        requestId: "diff-2",
        toolName: "edit_file",
        diff: "-old\n+second",
        files: [],
        status: "pending" as const,
        mode: "approval" as const,
        fileDecisions: {},
        lineComments: [],
      },
    };

    store.setDiffReview(first);
    store.setDiffReview(second);
    store.setDiffReview(second);

    expect(useAppStore.getState().pendingDiffReview?.requestId).toBe("diff-1");
    expect(useAppStore.getState().diffReviewQueue.map((item) => item.requestId)).toEqual(["diff-2"]);
    expect(useAppStore.getState().diffReview?.requestId).toBe("diff-1");

    store.clearDiffReview("diff-1");

    expect(useAppStore.getState().pendingDiffReview?.requestId).toBe("diff-2");
    expect(useAppStore.getState().diffReviewQueue).toEqual([]);
    expect(useAppStore.getState().diffReview?.requestId).toBe("diff-2");
  });

  it("removes a queued diff review by id without advancing the current review", () => {
    const store = useAppStore.getState();
    store.setDiffReview({ requestId: "diff-1", diff: "first" });
    store.setDiffReview({ requestId: "diff-2", diff: "second" });

    store.clearDiffReview("diff-2");

    expect(useAppStore.getState().pendingDiffReview?.requestId).toBe("diff-1");
    expect(useAppStore.getState().diffReviewQueue).toEqual([]);
  });

  it("keeps diff panel state scoped to the active conversation across the global prompt queue", () => {
    useAppStore.setState({ conversationId: "conv-active" });
    const store = useAppStore.getState();
    store.setDiffReview({
      requestId: "diff-other",
      conversationId: "conv-other",
      diff: "+other",
      reviewState: {
        requestId: "diff-other",
        conversationId: "conv-other",
        toolName: "write_file",
        diff: "+other",
        files: [],
        status: "pending",
        mode: "approval",
        fileDecisions: {},
        lineComments: [],
      },
    });
    store.setDiffReview({
      requestId: "diff-active",
      conversationId: "conv-active",
      diff: "+active",
      reviewState: {
        requestId: "diff-active",
        conversationId: "conv-active",
        toolName: "write_file",
        diff: "+active",
        files: [],
        status: "pending",
        mode: "approval",
        fileDecisions: {},
        lineComments: [],
      },
    });

    expect(useAppStore.getState().diffReview?.requestId).toBe("diff-active");
    store.clearDiffReview("diff-active");
    expect(useAppStore.getState().pendingDiffReview?.requestId).toBe("diff-other");
    expect(useAppStore.getState().diffReview).toBeNull();
  });

  it("queues, deduplicates, and advances ask-user prompts in FIFO order", () => {
    const store = useAppStore.getState();
    const first = { requestId: "ask-1", question: "First?" };
    const second = { requestId: "ask-2", question: "Second?" };

    store.setAskUser(first);
    store.setAskUser(second);
    store.setAskUser(second);

    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("ask-1");
    expect(useAppStore.getState().askUserQueue.map((item) => item.requestId)).toEqual(["ask-2"]);

    store.clearAskUser("ask-1");

    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("ask-2");
    expect(useAppStore.getState().askUserQueue).toEqual([]);
  });

  it("bulk cancellation removes matching queued prompts and promotes the next item", () => {
    const store = useAppStore.getState();
    store.setDiffReview({ requestId: "diff-1", diff: "first" });
    store.setDiffReview({ requestId: "diff-2", diff: "second" });
    store.setDiffReview({ requestId: "diff-3", diff: "third" });
    store.setAskUser({ requestId: "ask-1", question: "First?" });
    store.setAskUser({ requestId: "ask-2", question: "Second?" });
    store.setAskUser({ requestId: "ask-3", question: "Third?" });

    store.clearDiffReviews(["diff-1", "diff-2"]);
    store.clearAskUsers(["ask-1", "ask-2"]);

    expect(useAppStore.getState().pendingDiffReview?.requestId).toBe("diff-3");
    expect(useAppStore.getState().diffReviewQueue).toEqual([]);
    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("ask-3");
    expect(useAppStore.getState().askUserQueue).toEqual([]);
  });

  it("does not erase globally queued prompts when resetting the active view", () => {
    const reset = conversationResetPayload();
    expect(reset).not.toHaveProperty("pendingApproval");
    expect(reset).not.toHaveProperty("approvalQueue");
    expect(reset).not.toHaveProperty("pendingDiffReview");
    expect(reset).not.toHaveProperty("diffReviewQueue");
    expect(reset).not.toHaveProperty("pendingAskUser");
    expect(reset).not.toHaveProperty("askUserQueue");
    expect(reset).toMatchObject({ diffReview: null });
  });
});
