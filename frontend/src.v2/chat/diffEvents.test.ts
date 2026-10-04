import { beforeEach, describe, expect, it, vi } from "vitest";
import { handleDiffEvent } from "./diffEvents";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

describe("handleDiffEvent", () => {
  beforeEach(() => {
    vi.mocked(pushToast).mockClear();
    useAppStore.setState({
      conversationId: "conv-current",
      workingDirectory: "C:\\workspace",
      messages: [{
        id: "assistant-1",
        role: "assistant",
        content: "",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        turnId: "turn-1",
        isStreaming: true,
      }],
      conversationMessages: {},
      turnDiffs: {},
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });
  });

  it("keeps turn preview diffs scoped to the active assistant turn", () => {
    const handled = handleDiffEvent({
      type: "turn.diff.updated",
      thread_id: "conv-current",
      conversation_id: "conv-current",
      turn_id: "turn-1",
      message_id: "assistant-1",
      tool_call_id: "write-1",
      revision: 2,
      diff: "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@\n-old\n+new",
    } as ServerEvent);

    expect(handled).toBe(true);
    expect(useAppStore.getState().gitChanges.workingTree).toEqual([]);
    expect(useAppStore.getState().turnDiffs["conv-current"]).toMatchObject({
      threadId: "conv-current",
      turnId: "turn-1",
      messageId: "assistant-1",
      toolCallId: "write-1",
      revision: 2,
    });
  });

  it("keeps a turn diff that arrives after the final answer has settled", () => {
    useAppStore.setState({
      messages: [{
        id: "assistant-1",
        role: "assistant",
        content: "Done",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        turnId: "turn-1",
        isStreaming: false,
        terminalStatus: "completed",
      }],
    });

    handleDiffEvent({
      type: "turn.diff.updated",
      thread_id: "conv-current",
      conversation_id: "conv-current",
      turn_id: "turn-1",
      message_id: "assistant-1",
      revision: 3,
      diff: "diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n@@\n-old\n+new",
    } as ServerEvent);

    expect(useAppStore.getState().turnDiffs["conv-current"]).toMatchObject({
      turnId: "turn-1",
      messageId: "assistant-1",
      revision: 3,
    });
  });

  it("stores ordinary worktree diffs as global workspace state", () => {
    handleDiffEvent({
      type: "diff.git_working_tree",
      is_git_repo: true,
      conversation_id: "conv-current",
      workspace_root: "C:\\workspace",
      files: [{ path: "src/app.ts", patch: "diff", additions: 2, deletions: 0 }],
      untracked: ["new.txt"],
    } as ServerEvent);

    expect(useAppStore.getState().turnDiffs).toEqual({});
    expect(useAppStore.getState().gitChanges.workingTree).toEqual([
      { path: "src/app.ts", patch: "diff", additions: 2, deletions: 0, isBinary: undefined },
    ]);
    expect(useAppStore.getState().gitChanges.untracked).toEqual(["new.txt"]);
    expect(useAppStore.getState().gitChanges.isGitRepo).toBe(true);
  });

  it.each(["diff.git_working_tree", "diff.git_staged"] as const)("stores %s for a plain folder without an error notification", (type) => {
    useAppStore.setState({
      gitChanges: {
        workingTree: [{ path: "old.txt", additions: 1, deletions: 0 }],
        staged: [{ path: "old-staged.txt", additions: 1, deletions: 0 }],
        untracked: ["old-untracked.txt"],
        loading: true,
      },
    });
    handleDiffEvent({
      type,
      conversation_id: "conv-current",
      workspace_root: "C:\\workspace",
      is_git_repo: false,
      files: [],
      untracked: [],
    } as ServerEvent);

    expect(useAppStore.getState().gitChanges).toMatchObject({
      isGitRepo: false, workingTree: [], staged: [], untracked: [], loading: false,
    });
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("clears repository detection when switching folders", () => {
    useAppStore.getState().setGitChanges({ isGitRepo: false });
    useAppStore.getState().setWorkingDirectory("C:\\new-project");
    expect(useAppStore.getState().gitChanges.isGitRepo).toBeUndefined();
  });

  it("clears the previous conversation's Git state before restoring another project", () => {
    useAppStore.setState({
      conversations: [{ id: "conv-other", title: "Other project", updatedAt: "", workspaceRoot: "C:\\other-project" }],
      gitChanges: { isGitRepo: false, workingTree: [], staged: [], untracked: [], loading: false },
    });
    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-other" });
    expect(useAppStore.getState().workingDirectory).toBe("C:\\other-project");
    expect(useAppStore.getState().gitChanges.isGitRepo).toBeUndefined();
  });

  it("keeps POSIX workspace owners case-sensitive", () => {
    useAppStore.setState({
      workingDirectory: "/tmp/Project",
      gitChanges: {
        workingTree: [],
        staged: [],
        untracked: [],
        loading: false,
      },
    });

    handleDiffEvent({
      type: "diff.git_working_tree",
      conversation_id: "conv-current",
      workspace_root: "/tmp/project",
      files: [{ path: "wrong-workspace.ts", patch: "diff", additions: 1, deletions: 0 }],
      untracked: ["wrong-workspace.ts"],
    } as ServerEvent);

    expect(useAppStore.getState().gitChanges.workingTree).toEqual([]);
    expect(useAppStore.getState().gitChanges.untracked).toEqual([]);
  });
});
