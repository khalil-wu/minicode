import { describe, expect, it } from "vitest";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { projectTurn } from "./turn-projection";
import { projectMessagesToTurns } from "../chat/chatSurfaceState";
import { projectChatTurnToAgentLoop } from "../agent-loop/projection/project-turn";

const toolBlock = (overrides: Record<string, unknown> = {}): ContentBlock => ({
  type: "tool_call",
  record: {
    id: "tool-1",
    name: "custom_tool",
    args: {},
    status: "success",
    startedAt: 10,
    ...overrides,
  },
});

const streamingAssistantMessage = (id: string, blocks: ContentBlock[]): ChatMessage => ({
  id,
  role: "assistant",
  content: "",
  blocks,
  artifacts: [],
  timestamp: 1,
  isStreaming: true,
});

describe("projectTurn explicit event contract", () => {
  it("uses only a completed agent-message item as the final answer", () => {
    const projection = projectTurn([
      {
        type: "process",
        id: "inspect",
        itemKind: "process_text",
        content: "Inspecting files",
        status: "completed",
        timestamp: 1,
      },
      {
        type: "text",
        itemId: "agent-message",
        content: "Implemented the change.",
        status: "completed",
        source: "model_final",
        isStreaming: false,
      },
    ]);

    expect(projection.finalAnswer).toBe("Implemented the change.");
    expect(projection.activityItems).toEqual([
      expect.objectContaining({ kind: "processNote", content: "Inspecting files" }),
    ]);
  });

  it("uses tool-owned activity metadata without inspecting the tool name", () => {
    const projection = projectTurn([
      toolBlock({
        name: "looks_like_web_search_but_is_not_classified",
        resultKind: "command",
        activityKind: "commandExecution",
        displayHint: "Run check",
      }),
    ]);

    expect(projection.activityItems).toEqual([
      expect.objectContaining({ kind: "commandExecution", title: "Run check", status: "completed" }),
    ]);
  });

  it("keeps debug tool evidence out of the timeline unless Inspector projection is requested", () => {
    const block = toolBlock({
      name: "tool_search",
      displayHint: "Find tools",
      displaySummary: "Found 4 tools",
      visibility: "debug",
    });

    expect(projectTurn([block]).activityItems).toEqual([]);
    expect(projectTurn([block], { includeHiddenActivity: true }).activityItems).toEqual([
      expect.objectContaining({ id: "tool-1", kind: "genericTool" }),
    ]);
  });

  it("does not render a legacy progress mirror beside its typed tool lifecycle", () => {
    const projection = projectTurn([
      toolBlock({ id: "read-1", name: "read_file", status: "running" }),
      {
        type: "progress",
        id: "tool:read-1",
        stage: "tool",
        status: "running",
        message: "Running read_file",
        toolCallId: "read-1",
      },
    ]);

    expect(projection.activityItems).toHaveLength(1);
    expect(projection.activityItems[0]).toEqual(expect.objectContaining({ id: "read-1" }));
  });

  it("keeps unclassified tools generic", () => {
    const projection = projectTurn([toolBlock({ name: "web_search_like_name" })]);

    expect(projection.activityItems[0]).toEqual(expect.objectContaining({ kind: "genericTool" }));
  });

  it("normalizes completion-side result metadata when the start omitted activity_kind", () => {
    const projection = projectTurn([toolBlock({
      name: "read_file",
      resultKind: "file",
      displaySummary: "已读取 README.md",
      inputSummary: "README.md",
    })]);

    expect(projection.activityItems[0]).toEqual(expect.objectContaining({ kind: "fileRead" }));
  });

  it("classifies canonical built-in names when an older event omits projection metadata", () => {
    expect(projectTurn([toolBlock({ name: "read_file" })]).activityItems[0]).toEqual(
      expect.objectContaining({ kind: "fileRead" }),
    );
    expect(projectTurn([toolBlock({ name: "run_command" })]).activityItems[0]).toEqual(
      expect.objectContaining({ kind: "commandExecution" }),
    );
    expect(projectTurn([toolBlock({ name: "list_files", activityKind: "genericTool", resultKind: "generic" })]).activityItems[0]).toEqual(
      expect.objectContaining({ kind: "workspaceList" }),
    );
    expect(projectTurn([toolBlock({ name: "grep_files", activityKind: "genericTool", resultKind: "file" })]).activityItems[0]).toEqual(
      expect.objectContaining({ kind: "workspaceSearch" }),
    );
  });

  it("keeps browser activity distinct from the generic tool fallback", () => {
    expect(projectTurn([toolBlock({
      name: "browser_control",
      resultKind: "browser",
      activityKind: "genericTool",
      args: { action: "screenshot" },
    })]).activityItems[0]).toEqual(
      expect.objectContaining({ kind: "browser" }),
    );
    expect(projectTurn([toolBlock({
      name: "browser_control",
      resultKind: "preview",
    })]).activityItems[0]).toEqual(
      expect.objectContaining({ kind: "browser" }),
    );
  });

  it("repairs a stale non-browser activity label when the record is a screenshot", () => {
    expect(projectTurn([toolBlock({
      name: "browser_control",
      args: { action: "screenshot" },
      activityKind: "fileRead",
      resultKind: "browser",
      artifactId: "artifact-screen",
      artifactKind: "image",
      artifactMediaType: "image/png",
    })]).activityItems[0]).toEqual(
      expect.objectContaining({ kind: "browser" }),
    );
  });

  it("does not project model-facing tool result markup into the timeline summary", () => {
    const projection = projectTurn([toolBlock({
      displaySummary: "已搜索网页",
      inputSummary: "2026 年新闻",
      summary: '<untrusted_tool_result source="web_search">raw model context</untrusted_tool_result>',
    })]);

    expect(projection.activityItems[0]).toEqual(expect.objectContaining({
      title: "已搜索网页",
      summary: "2026 年新闻",
    }));
  });

  it("drops standalone protocol tool tokens that duplicate typed tool activity", () => {
    const projection = projectTurn([
      {
        type: "process",
        id: "leaked-search-name",
        itemKind: "process_text",
        content: "web_search",
        source: "commentary",
        status: "completed",
        timestamp: 1,
      },
      { type: "thinking", content: "`web_fetch`", source: "model_preamble" },
      toolBlock({ id: "search-1", name: "web_search", activityKind: "webSearch" }),
      toolBlock({ id: "fetch-1", name: "web_fetch", activityKind: "webSearch" }),
    ]);

    expect(projection.activityItems.map((item) => item.id)).toEqual(["search-1", "fetch-1"]);
  });

  it("drops bare punctuation emitted as model pre-tool narration", () => {
    const projection = projectTurn([
      {
        type: "process",
        id: "placeholder",
        itemKind: "process_text",
        content: "...",
        source: "model_preamble",
        status: "completed",
        timestamp: 1,
      },
      toolBlock({ id: "real-tool", name: "read_file", activityKind: "fileRead" }),
    ]);

    expect(projection.activityItems.map((item) => item.id)).toEqual(["real-tool"]);
  });

  it("keeps normal process prose that mentions a tool name", () => {
    const projection = projectTurn([
      {
        type: "process",
        id: "search-note",
        itemKind: "process_text",
        content: "我会用 web_search 核对官方来源。",
        source: "commentary",
        status: "completed",
        timestamp: 1,
      },
      toolBlock({ name: "web_search", activityKind: "webSearch" }),
    ]);

    expect(projection.activityItems[0]).toEqual(expect.objectContaining({
      id: "search-note",
      content: "我会用 web_search 核对官方来源。",
    }));
  });

  it("maps typed thinking and process blocks directly", () => {
    const projection = projectTurn([
      { type: "thinking", content: "Provider reasoning", source: "provider", visibility: "debug" },
      {
        type: "process",
        id: "process-1",
        itemKind: "status",
        content: "Waiting for approval",
        status: "running",
        visibility: "timeline",
        timestamp: 20,
      },
    ]);

    expect(projection.activityItems.map((item) => item.kind)).toEqual([
      "processNote",
    ]);
    expect(projection.activityItems[0]?.status).toBe("running");
  });

  it("does not restore settled provider reasoning from legacy transcripts", () => {
    const blocks: ContentBlock[] = [
      { type: "thinking", content: "raw provider reasoning", source: "provider", visibility: "debug" },
    ];

    expect(projectTurn(blocks).activityItems).toEqual([]);
    expect(projectTurn(blocks, { includeHiddenActivity: true }).activityItems).toEqual([]);
  });

  it("marks only the latest reasoning block as streaming", () => {
    const projection = projectTurn([
      { type: "thinking", content: "First reasoning", source: "provider" },
      {
        type: "process",
        id: "commentary",
        itemKind: "process_text",
        content: "Checking files",
        status: "completed",
        timestamp: 1,
      },
      { type: "thinking", content: "Second reasoning", source: "provider" },
    ], { isThinkingStreaming: true });

    expect(projection.activityItems.map((item) => item.status)).toEqual(["completed", "running"]);
  });

  it("keeps commentary in the ordered process trace before the following tool", () => {
    const blocks: ContentBlock[] = [
      { type: "thinking", content: "Reasoning", source: "provider" },
      {
        type: "text",
        itemId: "commentary-1",
        content: "Checking the three implementations.",
        source: "commentary",
        status: "in_progress",
        isStreaming: true,
      },
      toolBlock({ id: "tool-after-commentary", status: "running" }),
    ];

    const projection = projectTurn(blocks, { isStreaming: true });

    expect(projection.activityItems.map((item) => item.id)).toEqual(["commentary-1", "tool-after-commentary"]);
    // Provisional text is not an authoritative answer either.
    expect(projection.finalAnswer).toBe("");

    const [turn] = projectMessagesToTurns(
      [streamingAssistantMessage("assistant-commentary", blocks)],
      true,
    );
    expect(turn?.activeCell).toBeNull();
    expect(turn?.committedCells.map((cell) => cell.id)).toEqual(["commentary-1", "tool-after-commentary"]);
  });

  it("renders explicitly labelled streaming text as the live answer cell", () => {
    const blocks: ContentBlock[] = [{
      type: "text",
      itemId: "final-1",
      content: "Done.",
      source: "model_final",
      status: "in_progress",
      isStreaming: true,
    }];

    const projection = projectTurn(blocks, { isStreaming: true });

    expect(projection.activityItems).toEqual([]);
    expect(projection.finalAnswer).toBe("");

    const [turn] = projectMessagesToTurns(
      [streamingAssistantMessage("assistant-final", blocks)],
      true,
    );
    expect(turn?.activeCell).toMatchObject({
      kind: "streaming_assistant_tail",
      id: "final-1",
      partialMarkdown: "Done.",
    });

    const agentLoop = projectChatTurnToAgentLoop(turn!);
    expect(agentLoop.activeAnswerCell?.partialMarkdown).toBe("Done.");
    expect(agentLoop.answerCell).toMatchObject({
      kind: "assistant_markdown",
      markdownSource: "Done.",
      isStreaming: true,
    });
    expect(agentLoop.answerIsStreaming).toBe(true);
  });

  it("uses the authoritative terminal status without fabricating an answer", () => {
    const projection = projectTurn([], { terminalStatus: "failed" });

    expect(projection.status).toBe("failed");
    expect(projection.hasFailure).toBe(true);
    expect(projection.finalAnswer).toBe("");
  });

  it("does not project successful provider request completion as a work item", () => {
    const projection = projectTurn([
      {
        type: "progress",
        id: "provider:request-1",
        stage: "status",
        phase: "model",
        status: "completed",
        message: "提供商响应完成",
        providerState: "completed",
        retryAttempt: 0,
        maxRetries: 3,
        visibility: "timeline",
        timestamp: 100,
      },
      {
        type: "progress",
        id: "provider:mcp-tool",
        stage: "tool",
        phase: "tool",
        status: "completed",
        message: "MCP tool completed",
        timestamp: 110,
      },
    ]);

    expect(projection.activityItems.map((item) => item.id)).toEqual(["provider:mcp-tool"]);
  });

  it("does not infer a final answer from an untyped text block", () => {
    const projection = projectTurn([{ type: "text", content: "Legacy answer" }]);

    expect(projection.finalAnswer).toBe("");
  });

  it("never deletes an authoritative final answer just because it names a tool", () => {
    const projection = projectTurn([
      toolBlock({ name: "web_search", activityKind: "webSearch" }),
      {
        type: "text",
        itemId: "agent-message",
        content: "web_search",
        status: "completed",
        source: "model_final",
        isStreaming: false,
      },
    ]);

    expect(projection.finalAnswer).toBe("web_search");
  });
});
