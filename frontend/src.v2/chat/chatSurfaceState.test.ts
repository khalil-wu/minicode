import { describe, expect, it } from "vitest";
import type { ChatMessage, ContentBlock } from "../stores/types";
import {
  createRecentTurnProjectionCache,
  projectMessagesToTurns,
  projectRecentMessagesToTurns,
} from "./chatSurfaceState";

const message = (
  id: string,
  role: ChatMessage["role"],
  blocks: ContentBlock[] = [],
  overrides: Partial<ChatMessage> = {},
): ChatMessage => ({
  id,
  role,
  content: "",
  blocks,
  artifacts: [],
  timestamp: Number(id.replace(/\D/g, "")) || 1,
  ...overrides,
});

describe("chat surface explicit projection", () => {
  it("keeps the active user turn streaming before the assistant envelope exists", () => {
    const turns = projectMessagesToTurns([
      message("user-live", "user", [], { content: "执行三个工具" }),
    ], true);

    expect(turns[0]).toEqual(expect.objectContaining({
      id: "user-live",
      status: "streaming",
      finalAnswerCell: null,
    }));
  });

  it("preserves final-answer text around an interleaved image generation result", () => {
    const turns = projectMessagesToTurns([
      message("user-1", "user", [], { content: "画一只猫" }),
      message("assistant-2", "assistant", [
        { type: "text", itemId: "intro", content: "好的，我来生成这张图片。\n\n", source: "reply", status: "completed" },
        {
          type: "progress",
          id: "image-progress",
          stage: "image_generation",
          status: "completed",
          message: "图像生成完成",
          timestamp: 2,
        },
        { type: "text", itemId: "done", content: "图像已经为你生成好了。", source: "reply", status: "completed" },
      ], {
        artifacts: [{ artifactId: "image-1", kind: "image", summary: "生成图片", mediaType: "image/png" }],
      }),
    ]);

    expect(turns[0]?.finalAnswerCell).toMatchObject({
      markdownSource: "好的，我来生成这张图片。\n\n图像已经为你生成好了。",
      markdownBeforeArtifacts: "好的，我来生成这张图片。\n\n",
      markdownAfterArtifacts: "图像已经为你生成好了。",
    });
  });

  it("restores image placement from a durable UTF-16 text anchor when provider text was merged", () => {
    const intro = "好的🙂，我来生成这张图片。\n\n";
    const completion = "图像已经为你生成好了。";
    const turns = projectMessagesToTurns([
      message("user-3", "user", [], { content: "画一只猫" }),
      message("assistant-4", "assistant", [{
        type: "text",
        itemId: "agent-message",
        content: `${intro}${completion}`,
        source: "model_final",
        status: "completed",
      }], {
        artifacts: [{
          artifactId: "image-anchored",
          kind: "image",
          summary: "生成图片",
          mediaType: "image/png",
          textOffset: intro.length,
        }],
      }),
    ]);

    expect(turns[0]?.finalAnswerCell).toMatchObject({
      markdownSource: `${intro}${completion}`,
      markdownBeforeArtifacts: intro,
      markdownAfterArtifacts: completion,
    });
  });

  it("restores legacy merged image replies before the adapter completion sentence", () => {
    const intro = "好的，我来生成这张图片。\n\n";
    const completion = "图像已经为你生成好了。";
    const turns = projectMessagesToTurns([
      message("user-5", "user", [], { content: "画一只猫" }),
      message("assistant-6", "assistant", [
        {
          type: "text",
          itemId: "legacy-agent-message",
          content: `${intro}${completion}`,
          source: "model_final",
          status: "completed",
        },
        {
          type: "progress",
          id: "legacy-image-progress",
          stage: "image_generation",
          status: "completed",
          message: "图像生成完成",
          timestamp: 6,
        },
      ], {
        artifacts: [{
          artifactId: "legacy-image",
          kind: "image",
          summary: "生成图片",
          mediaType: "image/png",
        }],
      }),
    ]);

    expect(turns[0]?.finalAnswerCell).toMatchObject({
      markdownSource: `${intro}${completion}`,
      markdownBeforeArtifacts: intro,
      markdownAfterArtifacts: completion,
    });
  });

  it("pairs a user message with one assistant turn and final answer", () => {
    const turns = projectMessagesToTurns([
      message("user-1", "user", [], { content: "Change it" }),
      message("assistant-2", "assistant", [
        {
          type: "text",
          itemId: "agent-message",
          content: "Done",
          status: "completed",
          source: "model_final",
          isStreaming: false,
        },
      ]),
    ], false);

    expect(turns).toHaveLength(1);
    expect(turns[0]?.userCell?.content).toBe("Change it");
    expect(turns[0]?.finalAnswerCell?.markdownSource).toBe("Done");
    expect(turns[0]?.committedCells).toEqual([]);
  });

  it("projects commentary into the visible ordered work trace", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "process",
        id: "commentary-1",
        itemKind: "process_text",
        content: "我先查询官方天气来源。",
        source: "commentary",
        status: "completed",
        timestamp: 1,
      }]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "thinking",
        id: "commentary-1",
        source: "commentary",
        content: "我先查询官方天气来源。",
      }),
    ]);
  });

  it("projects command activity from explicit tool metadata", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "tool_call",
        record: {
          id: "cmd-1",
          name: "arbitrary_name",
          args: { command: "npm test" },
          status: "success",
          resultKind: "command",
          activityKind: "commandExecution",
          startedAt: 1,
        },
      }]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({ kind: "exec", command: "npm test", status: "success" }),
    ]);
  });

  it("projects task, send_message, and task_stop through the Codex collaboration history-cell path", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [
        {
          type: "tool_call",
          record: {
            id: "task-1",
            name: "task",
            args: {
              description: "审计渲染链路",
              prompt: "请完整审计渲染链路并返回证据。",
              agent_type: "reviewer",
            },
            status: "success",
            resultKind: "subagent",
            activityKind: "genericTool",
            outputPreview: "Background subagent subagent-a1b2c3d4 started.",
            startedAt: 1,
            finishedAt: 2,
          },
        },
        {
          type: "tool_call",
          record: {
            id: "message-1",
            name: "send_message",
            args: { recipient: "Kepler", message: "优先核对真实生产问题。" },
            status: "success",
            resultKind: "subagent",
            activityKind: "genericTool",
            startedAt: 3,
            finishedAt: 4,
          },
        },
        {
          type: "tool_call",
          record: {
            id: "stop-1",
            name: "task_stop",
            args: { subagent_id: "Kepler" },
            status: "success",
            resultKind: "subagent",
            activityKind: "genericTool",
            startedAt: 5,
            finishedAt: 6,
          },
        },
      ]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "collaboration",
        action: "sent_message",
        entries: [{
          agentId: "subagent-a1b2c3d4",
          agentLabel: "a1b2c3d4",
          content: "请完整审计渲染链路并返回证据。",
        }],
      }),
      expect.objectContaining({
        kind: "collaboration",
        action: "sent_message",
        entries: [{
          agentId: "Kepler",
          agentLabel: "Kepler",
          content: "优先核对真实生产问题。",
        }],
      }),
      expect.objectContaining({
        kind: "collaboration",
        action: "closed",
        entries: [{ agentId: "Kepler", agentLabel: "Kepler" }],
      }),
    ]);
  });

  it("groups parallel task prompts into one multi-agent collaboration row", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "tool_call",
        record: {
          id: "task-parallel",
          name: "task",
          args: {
            parallel_tasks: [
              { description: "审计前端", prompt: "检查前端。", agent_type: "explore" },
              { description: "审计后端", prompt: "检查后端。", agent_type: "reviewer" },
            ],
          },
          status: "success",
          resultKind: "subagent",
          activityKind: "genericTool",
          startedAt: 1,
        },
      }]),
    ], false);

    const [cell] = turns[0]?.committedCells ?? [];
    expect(cell).toMatchObject({
      kind: "collaboration",
      action: "sent_message",
      status: "success",
      entries: [
        { agentId: "审计前端", content: "检查前端。" },
        { agentId: "审计后端", content: "检查后端。" },
      ],
    });
  });

  it("projects delegation immediately while the task tool is still running", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "tool_call",
        record: {
          id: "task-live",
          name: "task",
          args: {
            parallel_tasks: [
              { description: "子任务一", prompt: "读取 fact-1.txt", agent_type: "explore" },
              { description: "子任务二", prompt: "读取 fact-2.txt", agent_type: "explore" },
            ],
          },
          status: "running",
          resultKind: "subagent",
          activityKind: "genericTool",
          startedAt: 1,
        },
      }]),
    ], true);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "collaboration",
        status: "running",
        entries: [
          { agentId: "子任务一", agentLabel: "子任务一", content: "读取 fact-1.txt" },
          { agentId: "子任务二", agentLabel: "子任务二", content: "读取 fact-2.txt" },
        ],
      }),
    ]);
  });

  it("keeps completed command output visible from the typed tool result", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "tool_call",
        record: {
          id: "cmd-output",
          name: "run_command",
          args: { command: "npm run check" },
          status: "success",
          resultKind: "command",
          activityKind: "commandExecution",
          summary: "importantTotal: 1034\nhardcodedMotionValues: 0",
          startedAt: 1,
        },
      }]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "exec",
        collapsed: true,
        stdoutFull: "importantTotal: 1034\nhardcodedMotionValues: 0",
      }),
    ]);
  });

  it("removes the command result status envelope from stdout", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "tool_call",
        record: {
          id: "cmd-envelope",
          name: "run_command",
          args: { command: "Get-ChildItem" },
          status: "success",
          resultKind: "command",
          activityKind: "commandExecution",
          summary: "Exit code: 0\n\nsrc\\app.ts\nsrc\\main.ts",
          startedAt: 1,
        },
      }]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "exec",
        exitCode: 0,
        stdoutFull: "src\\app.ts\nsrc\\main.ts",
      }),
    ]);
  });

  it("does not render typed stderr again through the combined output preview", () => {
    const stderr = "Expected ',', got '{'\nSyntaxError: missing ) after argument list";
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "tool_call",
        record: {
          id: "cmd-stderr",
          name: "run_command",
          args: { command: "node -e broken" },
          status: "failed",
          resultKind: "command",
          activityKind: "commandExecution",
          outputPreview: stderr,
          stderrPreview: stderr,
          summary: `Exit code: 1 (failed)\n\n${stderr}`,
          startedAt: 1,
        },
      }]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "exec",
        exitCode: 1,
        stdoutFull: "",
        stdoutPreview: [],
        stderrFull: stderr,
      }),
    ]);
  });

  it("keeps List, Search, and Read semantic kinds when broad file metadata is split", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [
        {
          type: "tool_call",
          record: {
            id: "list-1",
            name: "list_files",
            args: { directory: "frontend/src.v2/chat" },
            status: "success",
            resultKind: "file",
            activityKind: "workspaceSearch",
            displayHint: "List",
            summary: "frontend/src.v2/chat/ (26 entries)\n  ChatPane.tsx",
            startedAt: 1,
          },
        },
        {
          type: "tool_call",
          record: {
            id: "search-1",
            name: "grep_files",
            args: { pattern: "AgentTimeline", path: "frontend/src.v2" },
            status: "success",
            resultKind: "file",
            activityKind: "workspaceSearch",
            displayHint: "Search",
            summary: "frontend/src.v2/agent-loop/components/AgentTimeline.tsx:1",
            startedAt: 2,
          },
        },
        {
          type: "tool_call",
          record: {
            id: "read-1",
            name: "read_file",
            args: { file_path: "frontend/src.v2/chat/ChatPane.tsx" },
            status: "success",
            resultKind: "file",
            activityKind: "fileRead",
            displayHint: "Read",
            summary: "line one\nline two\n\n[content_hash: abc]",
            startedAt: 3,
          },
        },
      ]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({ kind: "activity", activityKind: "workspaceList", subtitle: "frontend/src.v2/chat · 26 项" }),
      expect.objectContaining({ kind: "activity", activityKind: "workspaceSearch", subtitle: "AgentTimeline · frontend/src.v2 · 1 个文件" }),
      expect.objectContaining({ kind: "activity", activityKind: "fileRead", subtitle: "frontend/src.v2/chat/ChatPane.tsx · 2 行" }),
    ]);
  });

  it("projects a typed file change as a diff cell", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "tool_call",
        record: {
          id: "edit-1",
          name: "arbitrary_name",
          args: { file_path: "src/app.ts" },
          status: "success",
          resultKind: "edit",
          activityKind: "fileChange",
          diff: { plus: 2, minus: 1, patch: "@@\n-old\n+new" },
          startedAt: 1,
        },
      }]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({ kind: "activity", id: "edit-1" }),
      expect.objectContaining({ kind: "diff", files: [expect.objectContaining({ path: "src/app.ts" })] }),
    ]);
  });

  it("coalesces consecutive edits to one compact file action", () => {
    const edit = (id: string, filePath: string, plus: number, minus: number, patch: string) => ({
      type: "tool_call" as const,
      record: {
        id,
        name: "apply_patch",
        args: { file_path: filePath },
        status: "success" as const,
        resultKind: "edit",
        activityKind: "fileChange" as const,
        diff: { plus, minus, patch },
        startedAt: plus + minus,
      },
    });
    const cells = projectMessagesToTurns([message("assistant-animated", "assistant", [
      edit("edit-a-1", "src/a.ts", 1, 1, "@@ first"),
      edit("edit-a-2", "src/a.ts", 6, 3, "@@ second"),
      edit("edit-b", "src/b.ts", 2, 0, "@@ b"),
    ])], false)[0]?.committedCells ?? [];

    expect(cells.map((cell) => cell.id)).toEqual(["edit-a-1", "edit-b", "diff-assistant-animated-files"]);
    expect(cells[0]).toEqual(expect.objectContaining({
      kind: "activity",
      toolCallRecords: expect.arrayContaining([
        expect.objectContaining({ id: "edit-a-1" }),
        expect.objectContaining({ id: "edit-a-2" }),
      ]),
    }));
  });

  it("shows write events and appends one aggregate diff after them", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [
        {
          type: "tool_call",
          record: {
            id: "write-running",
            name: "write_file",
            args: { file_path: "src/running.ts" },
            status: "running",
            resultKind: "edit",
            activityKind: "fileChange",
            displayHint: "Write",
            startedAt: 1,
          },
        },
        ...["src/a.ts", "src/b.ts"].map((filePath, index) => ({
          type: "tool_call" as const,
          record: {
            id: `edit-${index}`,
            name: "write_file",
            args: { file_path: filePath },
            status: "success" as const,
            resultKind: "edit",
            activityKind: "fileChange",
            diff: { plus: index + 1, minus: index },
            startedAt: index + 2,
          },
        })),
      ]),
    ], true);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "activity",
        title: "Write src/running.ts",
      }),
      expect.objectContaining({ kind: "activity", id: "edit-0" }),
      expect.objectContaining({ kind: "activity", id: "edit-1" }),
      expect.objectContaining({
        kind: "diff",
        files: [
          expect.objectContaining({ path: "src/a.ts", additions: 1, deletions: 0 }),
          expect.objectContaining({ path: "src/b.ts", additions: 2, deletions: 1 }),
        ],
        summary: { added: 3, deleted: 1, modifiedFiles: 2 },
      }),
    ]);
  });

  it("keeps non-consecutive edit rows and uses the newest patch per path in the final aggregate", () => {
    const edit = (
      id: string,
      filePath: string,
      plus: number,
      minus: number,
      patch: string,
    ) => ({
      type: "tool_call" as const,
      record: {
        id,
        name: "apply_patch",
        args: { file_path: filePath },
        status: "success" as const,
        resultKind: "edit",
        activityKind: "fileChange",
        diff: { plus, minus, patch },
        startedAt: plus + minus,
      },
    });
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [
        edit("edit-a-1", "src/a.ts", 2, 1, "@@ first"),
        {
          type: "tool_call",
          record: {
            id: "command-between-edits",
            name: "exec_command",
            args: { command: "npm test" },
            status: "success",
            activityKind: "commandExecution",
            startedAt: 4,
            finishedAt: 5,
          },
        },
        edit("edit-b", "src/b.ts", 3, 0, "@@ b"),
        edit("edit-a-2", "src/a.ts", 1, 2, "@@ latest"),
      ]),
    ], false);

    const cells = turns[0]?.committedCells ?? [];
    expect(cells.map((cell) => cell.id)).toEqual([
      "edit-a-1",
      "command-between-edits",
      "edit-b",
      "edit-a-2",
      "diff-assistant-1-files",
    ]);
    expect(cells[0]).toEqual(expect.objectContaining({ kind: "activity" }));
    expect(cells[2]).toEqual(expect.objectContaining({ kind: "activity" }));
    expect(cells.filter((cell) => cell.kind === "diff")).toHaveLength(1);
    expect(cells.at(-1)).toEqual(expect.objectContaining({
      kind: "diff",
      files: [
        expect.objectContaining({ path: "src/a.ts", patch: "@@ latest", additions: 3, deletions: 3 }),
        expect.objectContaining({ path: "src/b.ts", patch: "@@ b", additions: 3, deletions: 0 }),
      ],
    }));
  });

  it("merges absolute and relative spellings of the same workspace file", () => {
    const turns = projectMessagesToTurns([message("assistant-paths", "assistant", [
      {
        type: "tool_call",
        record: {
          id: "edit-absolute",
          name: "apply_patch",
          args: { file_path: "C:\\Desktop\\MiniCode\\src\\App.ts" },
          status: "success",
          resultKind: "edit",
          activityKind: "fileChange",
          diff: { plus: 2, minus: 0, patch: "@@ absolute" },
          startedAt: 1,
        },
      },
      {
        type: "tool_call",
        record: {
          id: "edit-relative",
          name: "apply_patch",
          args: { file_path: "src/app.ts" },
          status: "success",
          resultKind: "edit",
          activityKind: "fileChange",
          diff: { plus: 1, minus: 1, patch: "@@ latest" },
          startedAt: 2,
        },
      },
    ])], false, "C:\\Desktop\\MiniCode");

    const cells = turns[0]?.committedCells ?? [];
    expect(cells.filter((cell) => cell.kind === "activity")).toHaveLength(1);
    expect(cells.filter((cell) => cell.kind === "diff")).toEqual([
      expect.objectContaining({
        files: [expect.objectContaining({
          path: "src/app.ts",
          patch: "@@ latest",
          additions: 3,
          deletions: 1,
        })],
        summary: { added: 3, deleted: 1, modifiedFiles: 1 },
      }),
    ]);
  });

  it("keeps failed terminal tool details collapsed until the row is opened", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "tool_call",
        record: {
          id: "fetch-1",
          name: "web_fetch",
          args: { url: "https://example.com/news" },
          status: "failed",
          resultKind: "web",
          activityKind: "webSearch",
          displaySummary: "网页读取失败",
          inputSummary: "https://example.com/news",
          outputPreview: "Fetch failed",
          startedAt: 1,
          finishedAt: 2,
        },
      }]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "activity",
        status: "failed",
        collapsed: true,
      }),
    ]);
  });

  it("formats historical MCP protocol names as server.tool activity labels", () => {
    const turns = projectMessagesToTurns([
      message("assistant-mcp", "assistant", [{
        type: "tool_call",
        record: {
          id: "mcp-1",
          name: "mcp__github__search_users",
          args: { query: "octocat" },
          status: "success",
          displaySummary: "Completed: mcp__github__search_users",
          startedAt: 1,
          finishedAt: 2,
        },
      }]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "activity",
        title: "github.search_users",
      }),
    ]);
  });

  it("adds one terminal error cell when a failed turn has no failed tool", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [], {
        terminalStatus: "failed",
        failureMessage: "Provider failed",
      }),
    ], false);

    expect(turns[0]?.status).toBe("failed");
    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({ kind: "error", message: "Provider failed", recoverable: false }),
    ]);
  });

  it("preserves recoverability on terminal assistant errors", () => {
    const turns = projectMessagesToTurns([
      message("assistant-retryable", "assistant", [], {
        terminalStatus: "failed",
        failureMessage: "Temporary network failure",
        failureRecoverable: true,
      }),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "error",
        message: "Temporary network failure",
        recoverable: true,
      }),
    ]);
  });

  it("shows source-less streaming text as an active answer until its role is known", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [
        {
          type: "text",
          itemId: "agent-message",
          content: "Partial",
          status: "in_progress",
          isStreaming: true,
        },
      ], { isStreaming: true }),
    ], true);

    expect(turns[0]?.activeCell).toEqual(expect.objectContaining({
      kind: "streaming_assistant_tail",
      id: "agent-message",
      partialMarkdown: "Partial",
    }));
    expect(turns[0]?.committedCells).toEqual([]);
    expect(turns[0]?.finalAnswerCell).toBeNull();
  });

  it("keeps only model_final in the reply while preserving commentary and suppressing settled reasoning", () => {
    const turns = projectMessagesToTurns([
      message("assistant-news", "assistant", [
        { type: "thinking", content: "First reasoning", source: "provider" },
        {
          type: "text",
          itemId: "commentary-1",
          content: "我来搜索一下今天的新闻。",
          source: "commentary",
          status: "completed",
          isStreaming: false,
        },
        {
          type: "tool_call",
          record: { id: "search-1", name: "web_search", args: {}, status: "success", startedAt: 2 },
        },
        { type: "thinking", content: "Second reasoning", source: "provider" },
        {
          type: "text",
          itemId: "commentary-2",
          content: "我获取几个主要新闻源的具体内容。",
          source: "commentary",
          status: "completed",
          isStreaming: false,
        },
        {
          type: "tool_call",
          record: { id: "fetch-1", name: "web_fetch", args: {}, status: "success", startedAt: 3 },
        },
        { type: "thinking", content: "Final reasoning", source: "provider" },
        {
          type: "text",
          itemId: "final-1",
          content: "这是今天的新闻摘要。",
          source: "model_final",
          status: "completed",
          isStreaming: false,
        },
      ]),
    ], false);

    expect(turns[0]?.finalAnswerCell?.markdownSource).toBe("这是今天的新闻摘要。");
    expect(turns[0]?.committedCells.map((cell) => cell.kind)).toEqual(["thinking", "activity", "thinking", "activity"]);
    expect(turns[0]?.committedCells.filter((cell) => cell.kind === "thinking").map((cell) => cell.source)).toEqual(["commentary", "commentary"]);
  });

  it("keeps a provider reasoning summary in the completed work trace", () => {
    const turns = projectMessagesToTurns([
      message("assistant-summary", "assistant", [
        {
          type: "thinking",
          content: "已核对提交链路并定位重复请求。",
          source: "provider",
          providerReasoningType: "reasoning_summary_text",
        },
        {
          type: "text",
          itemId: "final-1",
          content: "修复完成。",
          source: "model_final",
          status: "completed",
          isStreaming: false,
        },
      ]),
    ], false);

    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "thinking",
        content: "已核对提交链路并定位重复请求。",
        providerReasoningType: "reasoning_summary_text",
        isStreaming: false,
      }),
    ]);
    expect(turns[0]?.finalAnswerCell?.markdownSource).toBe("修复完成。");
  });

  it("streams an unclassified preamble as a live process cell, never as the answer", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "text",
        itemId: "pending-message",
        content: "我先检查相关文件。",
        source: "pending",
        status: "in_progress",
        isStreaming: true,
      }], { isStreaming: true }),
    ], true);

    expect(turns[0]?.activeCell).toBeNull();
    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "thinking",
        source: "model_preamble",
        content: "我先检查相关文件。",
        isStreaming: true,
      }),
    ]);
    expect(turns[0]?.finalAnswerCell).toBeNull();
  });

  it("preserves commentary before the committed tool trace without settled reasoning", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [
        { type: "thinking", content: "Reasoning before commentary", source: "provider" },
        {
          type: "text",
          itemId: "commentary-1",
          content: "重新发起，将这些调研任务标记为只读以并行执行。",
          source: "commentary",
          status: "in_progress",
          isStreaming: true,
        },
        {
          type: "tool_call",
          record: {
            id: "tool-1",
            name: "start_subagent",
            args: {},
            status: "running",
            startedAt: 2,
          },
        },
      ], { isStreaming: true }),
    ], true);

    expect(turns[0]?.committedCells.map((cell) => cell.id)).toEqual(["commentary-1", "tool-1"]);
    expect(turns[0]?.activeCell).toBeNull();
    expect(turns[0]?.finalAnswerCell).toBeNull();
  });

  it("shows an explicitly labelled streaming item in the answer until completion", () => {
    const turns = projectMessagesToTurns([
      message("assistant-1", "assistant", [{
        type: "text",
        itemId: "final-message",
        content: "已经完成。",
        source: "model_final",
        status: "in_progress",
        isStreaming: true,
      }], { isStreaming: true }),
    ], true);

    expect(turns[0]?.activeCell).toEqual(expect.objectContaining({
      kind: "streaming_assistant_tail",
      id: "final-message",
      partialMarkdown: "已经完成。",
    }));
    expect(turns[0]?.finalAnswerCell).toBeNull();
    expect(turns[0]?.committedCells).toEqual([]);
  });

  it("limits recent turns without changing their content", () => {
    const messages = [
      message("user-1", "user", [], { content: "one" }),
      message("assistant-2", "assistant", [{
        type: "text", itemId: "agent-message", content: "first", status: "completed", isStreaming: false,
      }]),
      message("user-3", "user", [], { content: "two" }),
      message("assistant-4", "assistant", [{
        type: "text", itemId: "agent-message", content: "second", status: "completed", isStreaming: false,
      }]),
    ];

    const projected = projectRecentMessagesToTurns(messages, false, 1);

    expect(projected.hiddenTurnCount).toBe(1);
    expect(projected.totalTurnCount).toBe(2);
    expect(projected.turns[0]?.finalAnswerCell?.markdownSource).toBe("second");
  });

  it("reuses the recent turn boundary without walking the old prefix for every stream delta", () => {
    const messages = Array.from({ length: 200 }, (_, index) => ([
      message(`user-${index * 2 + 1}`, "user", [], { content: `question ${index}` }),
      message(`assistant-${index * 2 + 2}`, "assistant", [{
        type: "text",
        itemId: `answer-${index}`,
        content: `answer ${index}`,
        status: index === 199 ? "in_progress" : "completed",
        source: "model_final",
        isStreaming: index === 199,
      }], { isStreaming: index === 199 }),
    ])).flat();
    const cache = createRecentTurnProjectionCache();
    const first = projectRecentMessagesToTurns(messages, true, 40, cache, "conv-long");
    expect(first.hiddenTurnCount).toBe(160);

    Object.defineProperty(messages[2], "role", {
      configurable: true,
      get: () => {
        throw new Error("old prefix was scanned");
      },
    });
    const last = messages.at(-1)!;
    const nextMessages = messages.slice();
    nextMessages[nextMessages.length - 1] = {
      ...last,
      content: "answer 199 continued",
      blocks: [{
        type: "text",
        itemId: "answer-199",
        content: "answer 199 continued",
        status: "in_progress",
        source: "model_final",
        isStreaming: true,
      }],
    };

    const second = projectRecentMessagesToTurns(nextMessages, true, 40, cache, "conv-long");

    expect(second.hiddenTurnCount).toBe(160);
    expect(second.turns.at(-1)?.finalAnswerCell).toBeNull();
  });

  it("rebuilds exact turn boundaries for a non-streaming middle replacement", () => {
    const messages = Array.from({ length: 60 }, (_, index) => ([
      message(`user-${index * 2 + 1}`, "user", [], { content: `question ${index}` }),
      message(`assistant-${index * 2 + 2}`, "assistant", [], { content: `answer ${index}` }),
    ])).flat();
    const cache = createRecentTurnProjectionCache();
    const initial = projectRecentMessagesToTurns(messages, true, 10, cache, "conv-reload");
    expect(initial.totalTurnCount).toBe(60);

    const replaced = messages.slice();
    replaced[1] = message("system-replacement", "system", [], { content: "notice" });
    const rebuilt = projectRecentMessagesToTurns(replaced, false, 10, cache, "conv-reload");

    expect(rebuilt.totalTurnCount).toBe(61);
    expect(rebuilt.hiddenTurnCount).toBe(51);
  });

  it("omits queued user and assistant placeholders inside the projector", () => {
    const turns = projectMessagesToTurns([
      message("user-visible", "user", [], { content: "visible" }),
      message("assistant-visible", "assistant", [], { content: "answer" }),
      message("user-queued", "user", [], { content: "queued", queueState: "queued" }),
      message("assistant-queued", "assistant", [], { queueState: "queued" }),
    ], true);

    expect(turns).toHaveLength(1);
    expect(turns[0]?.userCell?.content).toBe("visible");
  });
});
