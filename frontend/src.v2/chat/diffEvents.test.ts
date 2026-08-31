import { beforeEach, describe, expect, it } from "vitest";
import { handleDiffEvent } from "./diffEvents";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";

describe("handleDiffEvent", () => {
  beforeEach(() => {
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
