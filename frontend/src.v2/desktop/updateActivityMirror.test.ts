/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAppStore } from "../stores";
import { buildUpdateActivitySnapshot, startUpdateActivityMirror } from "./updateActivityMirror";

describe("update activity snapshot", () => {
  afterEach(() => {
    delete window.__MINICODE_RUNTIME__;
    vi.useRealTimers();
  });
  beforeEach(() => {
    useAppStore.setState({
      conversationId: "conv-active",
      isStreaming: true,
      isConnected: true,
      runtimeSession: {
        active_stream_conversation_ids: ["conv-runtime"],
        pending_approval_count: 1,
        pending_approvals: [{ request_id: "runtime-approval", type: "approval" }],
        running_tasks: [{ task_id: "runtime-task" }],
      },
      conversationStreaming: {
        "conv-background": true,
        "side-running": true,
      },
      sideChats: {
        "side-running": {
          id: "side-running",
          messages: [],
          isStreaming: true,
          draft: "",
        },
        "side-idle": {
          id: "side-idle",
          messages: [],
          isStreaming: false,
          draft: "",
        },
      },
      pendingApproval: {
        requestId: "approval-1",
        toolName: "Bash",
        args: {},
      },
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [{ requestId: "diff-1", diff: "" }],
      pendingAskUser: null,
      askUserQueue: [{ requestId: "ask-1", question: "Continue?" }],
      attachments: [
        { id: "upload-live", name: "a.txt", type: "text/plain", size: 1, status: "uploading" },
        { id: "upload-done", name: "b.txt", type: "text/plain", size: 1, status: "ready" },
      ],
      conversationWorkbenchStates: {
        "conv-background": {
          attachments: [
            { id: "upload-background", name: "c.txt", type: "text/plain", size: 1, status: "uploading" },
          ],
        } as never,
      },
      editorTabs: [
        { path: "clean.ts", content: "same", original: "same", loading: false },
        { path: "dirty.ts", content: "changed", original: "old", loading: false },
      ],
      backgroundTasks: [
        { id: "task-running", command: "build", status: "running", timestamp: 1, conversationId: "conv-active" },
        { id: "task-stalled", command: "scaffold", status: "stalled", timestamp: 1, conversationId: "conv-active" },
        { id: "task-done", command: "lint", status: "completed", timestamp: 1, conversationId: "conv-active" },
      ],
    });
  });

  it("projects all install-blocking work without message or file content", () => {
    expect(buildUpdateActivitySnapshot(useAppStore.getState())).toEqual({
      runtimeReady: true,
      activeTurns: ["conv-active", "conv-background", "conv-runtime", "side-running"],
      sideChatStreams: ["side-running"],
      pendingPrompts: ["approval-1", "ask-1", "diff-1", "runtime-approval"],
      uploadingAttachments: ["upload-background", "upload-live"],
      dirtyEditors: ["dirty.ts"],
      backgroundTasks: ["runtime-task", "task-running", "task-stalled"],
    });
  });

  it("fails closed while the runtime is disconnected and ignores read-only diffs", () => {
    useAppStore.setState({
      isConnected: false,
      runtimeSession: null,
      editorTabs: [
        { path: "readonly.ts", content: "changed", original: "old", loading: false, readOnly: true },
      ],
    });

    const snapshot = buildUpdateActivitySnapshot(useAppStore.getState());
    expect(snapshot.runtimeReady).toBe(false);
    expect(snapshot.dirtyEditors).toEqual([]);
  });

  it("ACKs main-process snapshot requests with the current complete projection", async () => {
    const reportActivity = vi.fn().mockResolvedValue({ accepted: true, revision: 1 });
    let requestHandler: ((payload: { requestId?: string }) => void) | undefined;
    window.__MINICODE_RUNTIME__ = {
      desktop: {
        updates: {
          reportActivity,
          onActivityRequest: (handler: typeof requestHandler) => {
            requestHandler = handler;
            return () => { requestHandler = undefined; };
          },
        },
      } as never,
    };

    const stop = startUpdateActivityMirror();
    await vi.waitFor(() => expect(reportActivity).toHaveBeenCalledTimes(1));
    requestHandler?.({ requestId: "install-preflight-1" });
    await vi.waitFor(() => expect(reportActivity).toHaveBeenCalledTimes(2));
    expect(reportActivity.mock.calls[1]?.[1]).toEqual(["install-preflight-1"]);
    stop();
  });

  // Regression: the mirror subscribed to the whole store with no selector, so a
  // streaming token rebuilt the snapshot (flat-mapping every workbench, string
  // comparing every editor tab) and JSON.stringify'd it on every frame.
  it("ignores store changes the snapshot does not read", async () => {
    const reportActivity = vi.fn().mockResolvedValue({ accepted: true, revision: 1 });
    window.__MINICODE_RUNTIME__ = {
      desktop: {
        updates: { reportActivity, onActivityRequest: () => () => {} },
      } as never,
    };

    const stop = startUpdateActivityMirror();
    await vi.waitFor(() => expect(reportActivity).toHaveBeenCalledTimes(1));

    useAppStore.setState({
      messages: [{
        id: "assistant-token",
        role: "assistant",
        content: "streaming token",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      toolCallCount: 7,
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(reportActivity).toHaveBeenCalledTimes(1);

    // A change the snapshot does read still publishes.
    useAppStore.setState({ approvalQueue: [{ requestId: "approval-2", toolName: "Bash", args: {} }] });
    await vi.waitFor(() => expect(reportActivity).toHaveBeenCalledTimes(2));
    stop();
  });

  it("retries a rejected activity report instead of confirming stale state", async () => {
    vi.useFakeTimers();
    const reportActivity = vi.fn()
      .mockRejectedValueOnce(new Error("IPC unavailable"))
      .mockResolvedValue({ accepted: true, revision: 2 });
    window.__MINICODE_RUNTIME__ = {
      desktop: {
        updates: {
          reportActivity,
          onActivityRequest: () => () => {},
        },
      } as never,
    };

    const stop = startUpdateActivityMirror();
    await Promise.resolve();
    await Promise.resolve();
    expect(reportActivity).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(reportActivity).toHaveBeenCalledTimes(2);
    stop();
  });
});
