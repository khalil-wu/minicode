/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAppStore } from "../stores";
import { emptyConversationWorkbenchState } from "../stores/ui-slice";
import { emptyComposerDraft, flushComposerDrafts } from "../stores/composer-drafts";
import { flushEditorDrafts } from "../stores/editor-drafts";
import { cacheEditorStateForWorkspace, clearEditorWorkspaceBufferCacheForTests } from "../stores/shared-helpers";
import { buildUpdateActivitySnapshot, startUpdateActivityMirror } from "./updateActivityMirror";
import * as composerDrafts from "../stores/composer-drafts";

describe("update activity snapshot", () => {
  afterEach(() => {
    delete window.__MINICODE_RUNTIME__;
    vi.useRealTimers();
  });
  beforeEach(async () => {
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
    clearEditorWorkspaceBufferCacheForTests();
    cacheEditorStateForWorkspace("C:/hidden", [{ id: "hidden-dirty", path: "background.ts", content: "pending", original: "disk", loading: false }], "background.ts", null);
    useAppStore.setState({
      subagents: [], conversationAgentStates: {},
      ...emptyComposerDraft(), workingDirectory: "", composerDraftsReady: true,
      composerDraftStoragePending: false, composerDraftStorageError: null,
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
          attachments: [{ id: "upload-side", name: "side.txt", type: "text/plain", size: 1, status: "uploading" }],
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
          ...emptyConversationWorkbenchState(),
          attachments: [
            { id: "upload-background", name: "c.txt", type: "text/plain", size: 1, status: "uploading" },
          ],
        },
      },
      workspaceComposerDrafts: { "c:/hidden": { ...emptyComposerDraft(), draft: "hidden workspace draft",
        attachments: [{ id: "upload-workspace", name: "workspace.txt", type: "text/plain", size: 1, status: "uploading" }] } },
      editorTabs: [
        { id: "editor-fixture-1", path: "clean.ts", content: "same", original: "same", loading: false },
        { id: "editor-fixture-2", path: "dirty.ts", content: "changed", original: "old", loading: false },
      ],
      backgroundTasks: [
        { id: "task-running", command: "build", status: "running", timestamp: 1, conversationId: "conv-active" },
        { id: "task-stalled", command: "scaffold", status: "stalled", timestamp: 1, conversationId: "conv-active" },
        { id: "task-done", command: "lint", status: "completed", timestamp: 1, conversationId: "conv-active" },
      ],
    });
    await Promise.all([flushComposerDrafts(), flushEditorDrafts()]);
  });

  it("projects all install-blocking work without message or file content", () => {
    expect(buildUpdateActivitySnapshot(useAppStore.getState())).toEqual({
      runtimeReady: true,
      activeTurns: ["conv-active", "conv-background", "conv-runtime", "side-running"],
      sideChatStreams: ["side-running"],
      pendingPrompts: ["approval-1", "ask-1", "diff-1", "runtime-approval"],
      uploadingAttachments: ["upload-background", "upload-live", "upload-side", "upload-workspace"],
      dirtyEditors: ["c:/hidden/background.ts", "dirty.ts"],
      backgroundTasks: ["runtime-task", "task-running", "task-stalled"],
      pendingDrafts: [],
      draftStates: [
        `primary:revision:${useAppStore.getState().composerDraftRevision}`,
        JSON.stringify(["primary", "conversation", "conv-active"]),
        JSON.stringify(["primary", "conversation", "conv-background"]),
        JSON.stringify(["primary", "side", "side-idle"]),
        JSON.stringify(["primary", "workspace", "c:/hidden"]),
      ].sort(),
    });
  });

  it("blocks upgrade for detached workers after the visible parent has completed", () => {
    const state = useAppStore.getState();
    useAppStore.setState({ isStreaming: false, conversationStreaming: {}, backgroundTasks: [],
      runtimeSession: { ...state.runtimeSession, active_task_id: null, active_stream_conversation_ids: [], running_tasks: [],
        active_subagents: [{ id: "cold-detached", status: "running" }], queued_user_messages: [{ conversation_id: "parent", message_id: "queued-input" }] },
      subagents: [{ id: "live-detached", role: "Ada", status: "running" }],
      conversationAgentStates: { hidden: { plan: null, todos: [], agentProgress: [],
        subagents: [{ id: "pending-child", role: "Bo", status: "pending" }] } },
    });
    expect(buildUpdateActivitySnapshot(useAppStore.getState()).backgroundTasks).toEqual(["cold-detached", "live-detached", "pending-child", "queued-input"]);
  });

  it("fails closed while the runtime is disconnected and ignores read-only diffs", () => {
    useAppStore.setState({
      isConnected: false,
      runtimeSession: null,
      editorTabs: [
        { id: "editor-fixture-3", path: "readonly.ts", content: "changed", original: "old", loading: false, readOnly: true },
      ],
    });

    const snapshot = buildUpdateActivitySnapshot(useAppStore.getState());
    expect(snapshot.runtimeReady).toBe(false);
    expect(snapshot.dirtyEditors).toEqual(["c:/hidden/background.ts"]);
  });

  it("reports unknown recovery, pending commit and storage failure as distinct owned blockers", () => {
    useAppStore.setState({ composerDraftsReady: false, composerDraftStoragePending: true, composerDraftStorageError: "quota exhausted" });
    expect(buildUpdateActivitySnapshot(useAppStore.getState()).pendingDrafts).toEqual([
      "primary:recovery", "primary:commit", "primary:storage-error",
    ]);
  });

  it("ACKs main-process snapshot requests with the current complete projection", async () => {
    const reportActivity = vi.fn().mockResolvedValue({ accepted: true, revision: 1 });
    let requestHandler: ((payload: { requestId?: string }) => void) | undefined;
    window.__MINICODE_RUNTIME__ = {
      windowOwnerId: "primary",
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
      windowOwnerId: "primary",
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
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    const reportActivity = vi.fn()
      .mockRejectedValueOnce(new Error("IPC unavailable"))
      .mockResolvedValue({ accepted: true, revision: 2 });
    window.__MINICODE_RUNTIME__ = {
      windowOwnerId: "primary",
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

  it("skips side-chat text and subagent progress while reporting actual stream and worker completion", async () => {
    const reportActivity = vi.fn().mockResolvedValue({ accepted: true });
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary", desktop: { updates: { reportActivity, onActivityRequest: () => () => {} } } as never };
    const workers = [{ id: "worker", role: "Ada", status: "running" as const }];
    useAppStore.setState(state => ({ conversationStreaming: { "conv-background": true }, subagents: workers,
      conversationAgentStates: { "conv-active": { plan: null, todos: [], agentProgress: [], subagents: workers } },
      sideChats: { ...state.sideChats, "side-running": { ...state.sideChats["side-running"], messages: [{
        id: "side-answer", role: "assistant", content: "", timestamp: 1, artifacts: [], isStreaming: true,
      }] } },
    }));
    const stop = startUpdateActivityMirror();
    await vi.waitFor(() => expect(reportActivity).toHaveBeenCalledOnce());
    const collect = vi.spyOn(composerDrafts, "collectComposerDrafts");
    try {
      for (let index = 0; index < 50; index += 1) {
        useAppStore.getState().appendAgentMessageDelta("reply", `word ${index} `, "side-running", "side-answer", "reply");
        useAppStore.getState().updateSubagent("worker", { currentActivity: `working ${index}`, lastProgressAt: index }, "conv-active");
      }
      expect(collect).not.toHaveBeenCalled();
      expect(reportActivity).toHaveBeenCalledOnce();
      expect(useAppStore.getState().sideChats["side-running"].messages[0].blocks?.some(block => block.type === "text" && block.content.includes("word 49"))).toBe(true);
      useAppStore.getState().finishStreaming("side-running", undefined, "completed", "side-answer");
      useAppStore.getState().updateSubagent("worker", { status: "done" }, "conv-active");
      await vi.waitFor(() => {
        const snapshot = reportActivity.mock.calls.at(-1)?.[0];
        expect(snapshot.sideChatStreams).toEqual([]);
        expect(snapshot.backgroundTasks).not.toContain("worker");
      });
      expect(collect).toHaveBeenCalled();
    } finally { collect.mockRestore(); stop(); }
  });

  it("still collects authored rich drafts and side attachments without treating their text as task progress", async () => {
    const reportActivity = vi.fn().mockResolvedValue({ accepted: true });
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary", desktop: { updates: { reportActivity, onActivityRequest: () => () => {} } } as never };
    const stop = startUpdateActivityMirror();
    await vi.waitFor(() => expect(reportActivity).toHaveBeenCalledOnce());
    const collect = vi.spyOn(composerDrafts, "collectComposerDrafts");
    try {
      useAppStore.setState({ richDraft: { markdown: "", document: { type: "doc", content: [{ type: "paragraph" }] } } });
      expect(collect).toHaveBeenCalled();
      collect.mockClear();
      useAppStore.setState(state => ({ sideChats: { ...state.sideChats, "side-idle": { ...state.sideChats["side-idle"],
        attachments: state.sideChats["side-idle"].attachments!.map(attachment => ({ ...attachment, status: "ready" as const })),
      } } }));
      expect(collect).toHaveBeenCalled();
      await vi.waitFor(() => expect(reportActivity.mock.calls.at(-1)?.[0].uploadingAttachments).not.toContain("upload-side"));
      useAppStore.getState().setSideChatDraft("side-idle", "New side draft");
      await flushComposerDrafts();
      await vi.waitFor(() => expect(reportActivity.mock.calls.at(-1)?.[0].draftStates).toContain(JSON.stringify(["primary", "side", "side-idle"])));
    } finally { collect.mockRestore(); stop(); }
  });
});
