import { describe, expect, it } from "vitest";
import type { ChatTurnState } from "../../chat/cells/cellTypes";
import { projectChatTurnToAgentLoop } from "./project-turn";

const baseTurn = (): ChatTurnState => ({
  id: "turn-1",
  userCell: null,
  committedCells: [
    {
      kind: "activity",
      id: "read-1",
      activityKind: "fileRead",
      title: "Read files",
      status: "done",
      collapsed: true,
      startedAt: 1,
      toolCallRecords: [{
        id: "read-call-1",
        name: "read_file",
        args: { file_path: "src/index.ts" },
        status: "success",
        startedAt: 1,
        finishedAt: 2,
      }],
    },
    {
      kind: "status_notice",
      id: "warning-1",
      tone: "warning",
      title: "Connection interrupted",
      createdAt: 2,
    },
    {
      kind: "exec",
      id: "exec-1",
      command: "npm test",
      status: "failed",
      stdoutPreview: [],
      stderrPreview: ["failed"],
      collapsed: true,
      createdAt: 3,
    },
  ],
  activeCell: null,
  finalAnswerCell: null,
  status: "completed",
  startedAt: 1,
  completedAt: 2,
  durationMs: 1_500,
  usage: { input: 1_200, output: 80, reasoning: 25 },
});

describe("agent-loop process detail projection", () => {
  it("keeps the useful trace in summary mode", () => {
    const projection = projectChatTurnToAgentLoop(baseTurn(), undefined, "summary");

    expect(projection.processCells.map((cell) => cell.id)).toEqual(["read-1", "warning-1", "exec-1"]);
    expect(projection.initialProcessExpanded).toBe(true);
    expect(projection.hasProcessContent).toBe(true);
    expect(projection.durationMs).toBe(1_500);
  });

  it("keeps the normal work trace while leaving a completed turn collapsed", () => {
    const projection = projectChatTurnToAgentLoop(baseTurn(), undefined, "normal");

    expect(projection.processCells).toHaveLength(3);
    expect(projection.initialProcessExpanded).toBe(true);
  });

  it("keeps and expands the complete trace in verbose mode", () => {
    const projection = projectChatTurnToAgentLoop(baseTurn(), undefined, "verbose");

    expect(projection.processCells).toHaveLength(3);
    expect(projection.initialProcessExpanded).toBe(true);
  });

  it("does not advertise an empty disclosure for a completed turn without work", () => {
    const projection = projectChatTurnToAgentLoop({
      ...baseTurn(),
      committedCells: [],
      usage: undefined,
    }, undefined, "summary");

    expect(projection.processCells).toEqual([]);
    expect(projection.hasProcessContent).toBe(false);
    expect(projection.durationMs).toBe(1_500);
  });

  it("keeps the processing area present before the first activity without fabricating a model state", () => {
    const projection = projectChatTurnToAgentLoop({
      ...baseTurn(),
      committedCells: [],
      status: "streaming",
      completedAt: undefined,
      durationMs: undefined,
      usage: undefined,
    }, undefined, "summary");

    expect(projection.hasProcessContent).toBe(true);
  });

  it("keeps an active answer visible in summary mode", () => {
    const projection = projectChatTurnToAgentLoop({
      ...baseTurn(),
      committedCells: [],
      activeCell: {
        kind: "streaming_assistant_tail",
        id: "answer-tail",
        partialMarkdown: "Live answer",
        updatedAt: 3,
      },
      status: "streaming",
      completedAt: undefined,
      usage: undefined,
    }, undefined, "summary");

    expect(projection.answerCell).toEqual(expect.objectContaining({
      kind: "assistant_markdown",
      markdownSource: "Live answer",
      isStreaming: true,
    }));
    expect(projection.answerIsStreaming).toBe(true);
    expect(projection.hasProcessContent).toBe(false);
  });

  it("merges a committed answer prefix with its active continuation", () => {
    const projection = projectChatTurnToAgentLoop({
      ...baseTurn(),
      activeCell: {
        kind: "streaming_assistant_tail",
        id: "answer-tail",
        partialMarkdown: " continued",
        updatedAt: 4,
      },
      finalAnswerCell: {
        kind: "assistant_markdown",
        id: "answer-final",
        markdownSource: "Already committed",
        phase: "final",
        copyable: true,
        createdAt: 2,
      },
      status: "streaming",
      completedAt: undefined,
    }, undefined, "summary");

    expect(projection.answerCell).toEqual(expect.objectContaining({
      id: "answer-final",
      markdownSource: "Already committed continued",
      isStreaming: true,
    }));
    expect(projection.activeAnswerCell?.partialMarkdown).toBe(" continued");
  });

  it("appends an active continuation after existing answer artifacts", () => {
    const projection = projectChatTurnToAgentLoop({
      ...baseTurn(),
      activeCell: {
        kind: "streaming_assistant_tail",
        id: "answer-tail",
        partialMarkdown: "Tail after image",
        updatedAt: 4,
      },
      finalAnswerCell: {
        kind: "assistant_markdown",
        id: "answer-final",
        markdownSource: "Before image\nAfter image",
        markdownBeforeArtifacts: "Before image",
        markdownAfterArtifacts: "After image",
        artifacts: [{ artifactId: "image-1", kind: "image", summary: "result.png" }],
        phase: "final",
        copyable: true,
        createdAt: 2,
      },
      status: "streaming",
      completedAt: undefined,
    });

    expect(projection.answerCell).toEqual(expect.objectContaining({
      markdownSource: "Before image\nAfter imageTail after image",
      markdownBeforeArtifacts: "Before image",
      markdownAfterArtifacts: "After imageTail after image",
      isStreaming: true,
    }));
  });

  it("preserves changed files and collaboration cells without a parallel metrics projection", () => {
    const projection = projectChatTurnToAgentLoop({
      ...baseTurn(),
      committedCells: [
        {
          kind: "diff",
          id: "diff-1",
          status: "updated",
          files: [
            { path: "a.ts", additions: 4, deletions: 1 },
            { path: "b.ts", additions: 2, deletions: 0 },
          ],
          summary: { added: 6, deleted: 1, modifiedFiles: 2 },
          toolCallCount: 2,
          collapsed: true,
          createdAt: 1,
        },
        {
          kind: "collaboration",
          id: "collab-1",
          action: "sent_message",
          status: "success",
          entries: [
            { agentId: "subagent-a", agentLabel: "a" },
            { agentId: "subagent-b", agentLabel: "b" },
          ],
          collapsed: false,
          createdAt: 2,
        },
        {
          kind: "collaboration",
          id: "collab-2",
          action: "closed",
          status: "success",
          entries: [{ agentId: "subagent-a", agentLabel: "a" }],
          collapsed: false,
          createdAt: 3,
        },
      ],
    });

    expect(projection.processCells.map((cell) => cell.kind)).toEqual([
      "diff",
      "collaboration",
      "collaboration",
    ]);
  });
});
