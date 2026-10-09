import { describe, expect, it } from "vitest";
import type { AgentProgressEntry } from "../../stores/types";
import type { ChatMessage } from "../../stores/types";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import {
  buildRunReplayEvents,
  buildRunReplaySummary,
  buildRunTimelineItems,
  runTimelineExportJsonl,
} from "./ToolCallTimeline";

const progress = (patch: Partial<AgentProgressEntry>): AgentProgressEntry => ({
  type: "progress",
  id: "progress-1",
  stage: "status",
  phase: "status",
  status: "running",
  message: "Runtime progress",
  timestamp: 100,
  ...patch,
});

describe("ToolCallTimeline helpers", () => {
  it("keeps subagent work distinct from generic context progress", () => {
    const items = buildRunTimelineItems([], [
      progress({
        id: "subagent:agent-1",
        phase: "subagent",
        message: "Reviewing runtime spans",
        timestamp: 100,
      }),
      progress({
        id: "workflow:workflow-1",
        phase: "workflow",
        status: "completed",
        message: "Workflow completed",
        timestamp: 200,
      }),
    ]);

    expect(items.map((item) => item.phase)).toEqual(["subagent", "context"]);
    expect(items.map((item) => item.label)).toEqual(["Reviewing runtime spans", "Workflow completed"]);
  });

  it("excludes debug cache from visible timeline but includes it in replay exports", () => {
    const cacheProgress = progress({
      id: "cache:provider.prompt:sig",
      phase: "cache",
      status: "completed",
      message: "Cache hit: provider.prompt",
      visibility: "debug",
      timestamp: 300,
    });

    expect(buildRunTimelineItems([], [cacheProgress])).toEqual([]);

    const replay = buildRunReplayEvents([], [cacheProgress]);
    expect(replay).toEqual([
      expect.objectContaining({
        event: "cache.completed",
        phase: "cache",
        label: "Cache hit: provider.prompt",
      }),
    ]);
    expect(runTimelineExportJsonl([], [cacheProgress])).toContain('"phase":"cache"');
  });

  it("preserves cancelled tool calls as cancelled in timeline and replay", () => {
    const message = {
      id: "assistant-cancelled",
      role: "assistant",
      content: "",
      artifacts: [],
      timestamp: 100,
      toolCalls: [{
        id: "tool-cancelled",
        name: "run_command",
        args: { command: "python long_task.py" },
        status: "cancelled",
        startedAt: 100,
      }],
    } as ChatMessage;

    expect(buildRunTimelineItems([message], [])[0]?.status).toBe("cancelled");
    expect(buildRunReplayEvents([message], [])[0]).toMatchObject({
      event: "tool.cancelled",
      status: "cancelled",
    });
  });

  it("preserves pending and timeout in timeline and replay", () => {
    const message = {
      id: "assistant-statuses",
      role: "assistant",
      content: "",
      artifacts: [],
      timestamp: 100,
      toolCalls: [
        { id: "tool-pending", name: "read_file", args: {}, status: "pending", startedAt: 100 },
        { id: "tool-timeout", name: "run_command", args: {}, status: "timeout", startedAt: 200 },
      ],
    } as ChatMessage;

    expect(buildRunTimelineItems([message], []).map((item) => item.status)).toEqual([
      "pending",
      "timeout",
    ]);
    const replay = buildRunReplayEvents([message], []);
    expect(replay.map((event) => event.event)).toEqual(["tool.pending", "tool.timeout"]);
    expect(buildRunReplaySummary(replay)).toMatchObject({ outcome: "needs_attention", pending: 1, running: 0, failedOrBlocked: 1 });
  });

  it.each([
    [{ status: "pending" }, "pending"],
    [{ status: "running", transition: "queued" }, "pending"],
    [{ status: "running", transition: "prepared" }, "pending"],
    [{ status: "running", transition: "waiting_approval" }, "pending_approval"],
    [{ status: "pending", waitingOn: "approval" }, "pending_approval"],
    [{ status: "running", transition: "streaming_output" }, "running"],
    [{ status: "success", waitingOn: "approval", transition: "waiting_approval" }, "completed"],
  ] as const)("projects actual execution and approval state %j", (patch, expected) => {
    const record: ToolCallRecord = { id: "tool-state", name: "web_fetch", args: {}, startedAt: 100, ...patch };
    const message = { id: "assistant-state", role: "assistant", content: "", artifacts: [], timestamp: 100, toolCalls: [record] } as ChatMessage;
    const replay = buildRunReplayEvents([message], []);

    expect(buildRunTimelineItems([message], [])[0].status).toBe(expected);
    expect(replay[0]).toMatchObject({ status: expected, event: `tool.${expected}` });
    const pending = expected === "pending" || expected === "pending_approval";
    expect(buildRunReplaySummary(replay)).toMatchObject({
      outcome: pending ? "pending" : expected === "running" ? "running" : "completed",
      pending: pending ? 1 : 0,
      running: expected === "running" ? 1 : 0,
    });
  });

  it("does not use model-facing tool result markup as inspector summary", () => {
    const message = {
      id: "assistant-web",
      role: "assistant",
      content: "",
      artifacts: [],
      timestamp: 100,
      toolCalls: [{
        id: "tool-web",
        name: "web_search",
        args: { query: "2026 年新闻" },
        status: "success",
        displaySummary: "已搜索网页",
        inputSummary: "2026 年新闻",
        summary: '<untrusted_tool_result source="web_search">raw model context</untrusted_tool_result>',
        startedAt: 100,
      }],
    } as ChatMessage;

    expect(buildRunTimelineItems([message], [])[0]).toMatchObject({
      label: "已搜索网页",
      summary: "2026 年新闻",
    });
  });

  it("formats historical MCP names for display while preserving replay identity", () => {
    const message = {
      id: "assistant-mcp",
      role: "assistant",
      content: "",
      artifacts: [],
      timestamp: 100,
      toolCalls: [{
        id: "tool-mcp",
        name: "mcp__github__search_users",
        args: { query: "octocat" },
        status: "success",
        startedAt: 100,
      }],
    } as ChatMessage;

    expect(buildRunTimelineItems([message], [])[0]).toMatchObject({
      label: "github.search_users",
      toolName: "mcp__github__search_users",
    });
    expect(runTimelineExportJsonl([message], [])).toContain('"tool_name":"mcp__github__search_users"');
  });

  it("uses readable web labels in the visible timeline while preserving export identity", () => {
    const message = {
      id: "assistant-web-labels",
      role: "assistant",
      content: "",
      artifacts: [],
      timestamp: 100,
      toolCalls: [
        {
          id: "tool-fetch",
          name: "web_fetch",
          args: { url: "https://example.com" },
          status: "success",
          startedAt: 100,
        },
        {
          id: "tool-search",
          name: "web_search",
          args: { query: "MiniCode docs" },
          displayHint: "Searching official docs",
          status: "running",
          startedAt: 200,
        },
      ],
    } as ChatMessage;

    expect(buildRunTimelineItems([message], [])).toEqual([
      expect.objectContaining({ label: "读取网页", toolName: "web_fetch" }),
      expect.objectContaining({ label: "搜索 official docs", toolName: "web_search" }),
    ]);
    expect(runTimelineExportJsonl([message], [])).toContain('"tool_name":"web_fetch"');
  });

  it("does not project provider request lifecycle rows", () => {
    const items = buildRunTimelineItems([], [
      progress({
        id: "provider:running",
        phase: "model",
        status: "running",
        message: "Provider diagnostic",
        summary: "retrying request",
        retryAttempt: 2,
        maxRetries: 5,
        timestamp: 100,
      }),
      progress({
        id: "provider:completed",
        phase: "model",
        status: "completed",
        message: "连接成功",
        summary: "provider completed",
        retryAttempt: 2,
        maxRetries: 5,
        timestamp: 200,
      }),
      progress({
        id: "provider:failed",
        phase: "model",
        status: "failed",
        message: "连接失败",
        summary: "provider failed",
        retryAttempt: 5,
        maxRetries: 5,
        timestamp: 300,
      }),
      progress({
        id: "provider:partial",
        phase: "model",
        status: "partial",
        message: "连接中断",
        summary: "provider partial",
        retryAttempt: 3,
        maxRetries: 5,
        timestamp: 400,
      }),
    ]);

    expect(items).toEqual([]);
    expect(items.slice(1).map((item) => item.label).join(" ")).not.toContain("正在重新连接");
  });
});
