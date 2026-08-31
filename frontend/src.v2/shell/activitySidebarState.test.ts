import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../stores/types";
import { buildActivitySidebarState, buildOutput } from "./activitySidebarState";

const message = (patch: Partial<ChatMessage> & Pick<ChatMessage, "id" | "role">): ChatMessage => ({
  content: "",
  artifacts: [],
  timestamp: 1,
  ...patch,
});

describe("buildActivitySidebarState", () => {
  it("projects agent.progress events without inferring ownership from tool names", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-current",
      isStreaming: false,
      messages: [],
      todos: [],
      plan: null,
      agentProgress: [
        {
          type: "progress",
          id: "tool:task:start",
          stage: "tool",
          status: "running",
          message: "Subagent running: 查询广州当前天气",
          summary: "Subagent running: 查询广州当前天气",
          visibility: "compact",
          toolName: "task",
          conversationId: "conv-current",
          timestamp: 1,
        },
        {
          type: "progress",
          id: "tool:task_status:done",
          stage: "tool",
          status: "completed",
          message: "1 delegated task(s): completed",
          summary: "1 delegated task(s): completed",
          visibility: "compact",
          toolName: "task_status",
          conversationId: "conv-current",
          timestamp: 2,
        },
      ],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.progress.map((item) => item.id)).toEqual([
      "tool:task:start",
      "tool:task_status:done",
    ]);
  });

  it("returns an empty sidebar state when there is no active conversation", () => {
    const state = buildActivitySidebarState({
      conversationId: null,
      messages: [
        message({
          id: "assistant-old",
          role: "assistant",
          artifacts: [{ artifactId: "old", kind: "file", summary: "old.txt" }],
          citations: [{ source: "https://old.example", url: "https://old.example", range: [0, 0] }],
        }),
      ],
      todos: [{ id: "todo-old", content: "old task", activeForm: "old task", status: "in_progress" }],
      plan: null,
      agentProgress: [],
      livePreviewUrl: "http://localhost:5173",
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.hasConversation).toBe(false);
    expect(state.summary).toEqual([]);
    expect(state.workspace).toEqual([]);
    expect(state.progress).toEqual([]);
    expect(state.output).toEqual([]);
    expect(state.browser).toEqual([]);
    expect(state.sources).toEqual([]);
    expect(state.attachments).toEqual([]);
    expect(state.runs).toEqual([]);
    expect(state.browserAnnotations).toEqual([]);
    expect(state.isEmpty).toBe(true);
  });

  it("derives output evidence while keeping only the current task progress in the sidebar", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      isStreaming: true,
      messages: [
        message({
          id: "assistant-1",
          role: "assistant",
          content: "Done with source [2].",
          artifacts: [
            { artifactId: "artifact-1", kind: "image", summary: "preview.png", mediaType: "image/png", url: "blob:test" },
          ],
          citations: [
            { source: "https://unused.example/a", url: "https://unused.example/a", range: [0, 0] },
            { source: "https://docs.example/used", url: "https://docs.example/used", label: "Docs", title: "Used docs", range: [0, 0] },
          ],
          blocks: [
            {
              type: "tool_call",
              record: {
                id: "search-1",
                name: "web_search",
                args: { query: "unused" },
                status: "success",
                startedAt: 1,
                sourceUrl: "https://candidate.example/not-cited",
              },
            },
          ],
        }),
      ],
      todos: [
        { id: "todo-1", content: "Read files", activeForm: "Reading files", status: "completed" },
        { id: "todo-2", content: "Verify UI", activeForm: "Verifying UI", status: "in_progress" },
      ],
      plan: null,
      agentProgress: [],
      livePreviewUrl: "http://localhost:5173/app",
      previewArtifact: null,
      previewVerification: { url: "http://localhost:5173/app", ok: true, elapsed_ms: 44, checkedAt: 1 },
      previewServers: [{ name: "Vite", framework: "React", port: 5173, url: "http://localhost:5173/app" }],
      previewLaunchProcesses: [],
    });

    expect(state.progress).toEqual([
      {
        id: "todo-2",
        label: "Verifying UI",
        status: "running",
      },
    ]);
    expect(state.output).toMatchObject([
      { id: "artifact-1", kind: "image", label: "preview.png", artifactId: "artifact-1" },
    ]);
    expect(state.browser).toMatchObject([
      {
        id: "live-preview",
        url: "http://localhost:5173/app",
        label: "Vite",
        status: "verified",
      },
    ]);
    expect(state.sources).toMatchObject([
      { url: "https://docs.example/used", label: "Docs", title: "Used docs" },
    ]);
    expect(state.sources.map((item) => item.url)).not.toContain("https://candidate.example/not-cited");
  });

  it("projects browser screenshots from legacy toolCalls when blocks are absent", () => {
    const legacyMessage = Object.assign(message({
      id: "assistant-legacy-screenshot",
      role: "assistant",
    }), {
      toolCalls: [{
        id: "browser-legacy",
        name: "browser_control",
        args: { action: "screenshot" },
        status: "success",
        artifactId: "legacy-shot",
        artifactKind: "browser_screenshot",
        artifactMediaType: "IMAGE/PNG; charset=binary",
        displaySummary: "浏览器截图",
      }],
    }) as ChatMessage;

    expect(buildOutput([legacyMessage], null)).toMatchObject([{
      id: "legacy-shot",
      artifactId: "legacy-shot",
      kind: "image",
      mediaType: "image/png",
      label: "浏览器截图",
    }]);
  });

  it("merges sparse message artifacts with richer tool metadata", () => {
    const merged = message({
      id: "assistant-sparse-screenshot",
      artifacts: [{
        artifactId: "sparse-shot",
        kind: "browser_screenshot" as never,
        summary: " ",
      }],
      blocks: [{
        type: "tool_call",
        record: {
          id: "browser-rich",
          name: "browser_control",
          args: { action: "screenshot" },
          status: "success",
          artifactId: "sparse-shot",
          artifactKind: "image",
          artifactMediaType: "image/webp; charset=binary",
          artifactBytes: 2048,
          displaySummary: "浏览器截图",
        },
      }],
    });

    expect(buildOutput([merged], null)).toMatchObject([{
      id: "sparse-shot",
      kind: "image",
      label: "浏览器截图",
      mediaType: "image/webp",
    }]);
  });

  it("keeps the current preview visible after applying the output cap", () => {
    const messages = Array.from({ length: 31 }, (_, index) => message({
      id: `assistant-output-${index}`,
      artifacts: [{
        artifactId: `artifact-${index}`,
        kind: "file",
        summary: `output-${index}.txt`,
      }],
    }));
    const output = buildOutput(messages, {
      artifactId: "preview-current",
      content: "",
      name: "preview-current.png",
      kind: "image",
      mediaType: "image/png",
      loadedAt: 1,
    });

    expect(output).toHaveLength(12);
    expect(output[0]).toMatchObject({
      id: "preview-current",
      label: "preview-current.png",
      kind: "image",
    });
  });

  it("does not show candidate citations unless the final answer cites them inline", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [
        message({
          id: "assistant-weather",
          role: "assistant",
          content: "北京今天多云，午后可能有阵雨。",
          citations: [
            { source: "https://weather.example/beijing", url: "https://weather.example/beijing", title: "Beijing weather", range: [0, 0] },
            { source: "https://nmc.example/beijing", url: "https://nmc.example/beijing", title: "NMC forecast", range: [0, 0] },
          ],
        }),
      ],
      todos: [],
      plan: null,
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.sources).toEqual([]);
    expect(state.isEmpty).toBe(true);
  });

  it("keeps sources empty for uncited web search candidates", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [
        message({
          id: "assistant-search-candidates",
          role: "assistant",
          content: "北京今天多云。",
          blocks: [
            {
              type: "tool_call",
              record: {
                id: "search-1",
                name: "web_search",
                args: { query: "today weather beijing" },
                status: "success",
                summary: "[1] Weather\nURL: https://candidate.example/weather",
                sourceUrl: "https://candidate.example/weather",
              },
            },
          ],
        }),
      ],
      todos: [],
      plan: null,
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.sources).toEqual([]);
  });

  it("does not surface placeholder null values as sources or output labels", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [
        message({
          id: "assistant-null",
          role: "assistant",
          artifacts: [
            { artifactId: "artifact-null", kind: "file", summary: "$null", mediaType: "text/plain" },
          ],
          blocks: [
            {
              type: "tool_call",
              record: {
                id: "read-null",
                name: "read_file",
                args: { path: "$null" },
                status: "success",
              },
            },
          ],
        }),
      ],
      todos: [],
      plan: null,
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.output).toMatchObject([
      { id: "artifact-null", label: "生成文件", path: undefined },
    ]);
    expect(state.output.map((item) => item.label)).not.toContain("$null");
    expect(state.sources).toEqual([]);
  });

  it("keeps workspace file reads out of the sources sidebar", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [
        message({
          id: "assistant-file-sources",
          role: "assistant",
          blocks: [
            {
              type: "tool_call",
              record: {
                id: "read-1",
                name: "read_file",
                args: { path: "C:/Desktop/MiniCode/src/app.ts" },
                status: "success",
                startedAt: 1,
                resultKind: "file",
              },
            },
            {
              type: "tool_call",
              record: {
                id: "grep-1",
                name: "grep_files",
                args: { path: "C:/Desktop/MiniCode/src" },
                status: "success",
                startedAt: 2,
                resultKind: "search",
              },
            },
            {
              type: "tool_call",
              record: {
                id: "write-1",
                name: "write_file",
                args: { path: "C:/Desktop/MiniCode/src/new.ts" },
                status: "success",
                startedAt: 3,
                resultKind: "file",
              },
            },
            {
              type: "tool_call",
              record: {
                id: "command-1",
                name: "run_command",
                args: { command: "npm test", path: "C:/Desktop/MiniCode/package.json" },
                status: "success",
                startedAt: 4,
                resultKind: "command",
              },
            },
          ],
        }),
      ],
      todos: [],
      plan: null,
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.sources).toEqual([]);
    expect(state.sources.map((item) => item.path)).not.toContain("C:/Desktop/MiniCode/src/new.ts");
    expect(state.sources.map((item) => item.path)).not.toContain("C:/Desktop/MiniCode/package.json");
  });

  it("still shows only the explicitly cited citation indexes", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [
        message({
          id: "assistant-cited",
          role: "assistant",
          content: "北京天气参考中央气象台 [2]。",
          citations: [
            { source: "https://unused.example/a", url: "https://unused.example/a", range: [0, 0] },
            { source: "https://nmc.example/beijing", url: "https://nmc.example/beijing", label: "NMC", title: "Beijing forecast", range: [0, 0] },
          ],
        }),
      ],
      todos: [],
      plan: null,
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.sources.map((item) => item.url)).toEqual(["https://nmc.example/beijing"]);
  });

  it("compresses plan and compact agent progress into the runtime activity state", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [],
      isStreaming: true,
      todos: [],
      plan: {
        threadId: "conv-1",
        turnId: "turn-1",
        plan: [
          { step: "Inspect files", status: "completed" },
          { step: "Run tests", status: "in_progress" },
        ],
      },
      agentProgress: [
        {
          type: "progress",
          id: "progress-1",
          stage: "verification",
          phase: "verify",
          status: "running",
          message: "Running focused tests",
          visibility: "compact",
          timestamp: 1,
          conversationId: "conv-1",
        },
        {
          type: "progress",
          id: "debug-1",
          stage: "tool",
          status: "running",
          message: "raw retry guard",
          visibility: "debug",
          timestamp: 2,
          conversationId: "conv-1",
        },
      ],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.progress).toEqual([
      {
        id: "plan-step-1",
        label: "Run tests",
        detail: undefined,
        status: "running",
      },
      {
        id: "progress-1",
        label: "Running focused tests",
        detail: undefined,
        status: "running",
      },
    ]);
    expect(state.isEmpty).toBe(false);
  });

  it("shows provider reconnect progress while running but omits successful request completion", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [],
      isStreaming: true,
      todos: [],
      plan: null,
      agentProgress: [
        {
          type: "progress",
          id: "provider:running",
          stage: "status",
          phase: "model",
          status: "running",
          message: "Provider diagnostic",
          summary: "retrying request",
          visibility: "compact",
          retryAttempt: 2,
          maxRetries: 5,
          conversationId: "conv-1",
          timestamp: 1,
        },
        {
          type: "progress",
          id: "provider:completed",
          stage: "status",
          phase: "model",
          status: "completed",
          message: "连接成功",
          summary: "provider completed",
          visibility: "compact",
          retryAttempt: 2,
          maxRetries: 5,
          conversationId: "conv-1",
          timestamp: 2,
        },
        {
          type: "progress",
          id: "provider:failed",
          stage: "status",
          phase: "model",
          status: "failed",
          message: "连接失败",
          summary: "provider failed",
          visibility: "compact",
          retryAttempt: 5,
          maxRetries: 5,
          conversationId: "conv-1",
          timestamp: 3,
        },
        {
          type: "progress",
          id: "provider:partial",
          stage: "status",
          phase: "model",
          status: "partial",
          message: "连接中断",
          summary: "provider partial",
          visibility: "compact",
          retryAttempt: 3,
          maxRetries: 5,
          conversationId: "conv-1",
          timestamp: 4,
        },
      ],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.progress).toMatchObject([
      { id: "provider:running", label: "正在重新连接 2/5", status: "running", retryAttempt: 2, maxRetries: 5 },
      { id: "provider:failed", label: "连接失败（重试 5/5 后）", status: "failed" },
      { id: "provider:partial", label: "连接中断（重试 3/5）", status: "failed" },
    ]);
    expect(state.progress.map((item) => item.id)).not.toContain("provider:completed");
  });

  it("does not show restored stale running progress as live", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [],
      isStreaming: false,
      todos: [],
      plan: {
        threadId: "conv-1",
        turnId: "turn-1",
        plan: [
          { step: "Create files", status: "in_progress" },
          { step: "Run tests", status: "pending" },
        ],
      },
      agentProgress: [
        {
          type: "progress",
          id: "verify-1",
          stage: "verification",
          status: "running",
          message: "Running tests",
          visibility: "compact",
          timestamp: 1,
          conversationId: "conv-1",
        },
      ],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.progress).toEqual([
      {
        id: "plan-step-0",
        label: "Create files",
        detail: undefined,
        status: "pending",
      },
      {
        id: "verify-1",
        label: "Running tests",
        detail: undefined,
        status: "pending",
      },
    ]);
  });

  it("surfaces the current step from the canonical pending plan", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [],
      isStreaming: true,
      todos: [],
      plan: {
        threadId: "conv-1",
        turnId: "turn-1",
        plan: [
          { step: "需求分析与技术选型", status: "pending" },
          { step: "项目结构与基础搭建", status: "pending" },
        ],
      },
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.progress).toEqual([{
      id: "plan-step-0",
      label: "需求分析与技术选型",
      detail: undefined,
      status: "pending",
    }]);
    expect(state.isEmpty).toBe(false);
  });

  it("keeps main-agent lifecycle phases in user-facing progress", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [],
      todos: [],
      plan: null,
      agentProgress: [
        {
          type: "progress",
          id: "agent-run:run-1",
          stage: "planning",
          phase: "planning",
          status: "running",
          message: "Agent run started",
          visibility: "timeline",
          timestamp: 1,
          conversationId: "conv-1",
        },
        {
          type: "progress",
          id: "agent-phase:run-1:context",
          stage: "planning",
          phase: "planning",
          status: "running",
          message: "Preparing agent context",
          visibility: "timeline",
          timestamp: 2,
          conversationId: "conv-1",
        },
        {
          type: "progress",
          id: "agent-phase:run-1:execute",
          stage: "planning",
          phase: "tool",
          status: "running",
          message: "Model deciding next action",
          visibility: "timeline",
          timestamp: 3,
          conversationId: "conv-1",
        },
      ],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.progress).toMatchObject([
      { id: "agent-run:run-1", label: "Agent run started", status: "completed" },
      { id: "agent-phase:run-1:context", label: "Preparing agent context", status: "completed" },
      { id: "agent-phase:run-1:execute", label: "Model deciding next action", status: "pending" },
    ]);
  });

  it("keeps background commands scoped to their owning conversation", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-active",
      messages: [],
      todos: [],
      plan: null,
      agentProgress: [],
      backgroundTasks: [
        { id: "active", command: "npm test", status: "running", timestamp: 1, conversationId: "conv-active" },
        { id: "other", command: "npm run dev", status: "running", timestamp: 2, conversationId: "conv-other" },
      ],
    });

    expect(state.runs.filter((item) => item.kind === "background-command")).toMatchObject([
      { id: "background:active", label: "npm test", status: "running" },
    ]);
  });

  it("surfaces an interactive background stall with prompt evidence and attention", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-active",
      messages: [],
      todos: [],
      plan: null,
      agentProgress: [],
      backgroundTasks: [{
        id: "stalled",
        command: "npm create vite",
        status: "stalled",
        timestamp: 1,
        conversationId: "conv-active",
        stalledTail: "Overwrite existing files? [y/N]",
        stalledAdvice: "Use a non-interactive flag.",
      }],
    });

    expect(state.runs).toMatchObject([{
      id: "background:stalled",
      label: "npm create vite",
      detail: "等待输入 · Overwrite existing files? [y/N]",
      status: "stalled",
      attention: true,
    }]);
  });

  it("projects progress by event type rather than a display-scope field", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [],
      todos: [],
      plan: null,
      agentProgress: [
        {
          type: "progress",
          id: "provider-request:iteration-1",
          stage: "planning",
          phase: "model",
          status: "completed",
          message: "Provider request completed",
          summary: "Provider request completed",
          detail: "duration_ms=5794",
          visibility: "timeline",
          timestamp: 1,
          conversationId: "conv-1",
        },
        {
          type: "progress",
          id: "status-visible",
          stage: "status",
          phase: "status",
          status: "completed",
          message: "测试通过",
          detail: "duration_ms=1250 · 3 checks",
          visibility: "timeline",
          timestamp: 2,
          conversationId: "conv-1",
        },
      ],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.progress).toMatchObject([
      {
        id: "status-visible",
        label: "测试通过",
        detail: "1.3 秒 · 3 checks",
      },
    ]);
    expect(state.progress.map((item) => item.id)).not.toContain("provider-request:iteration-1");
  });

  it("derives task summary and workspace status", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [],
      isStreaming: true,
      todos: [],
      plan: null,
      agentProgress: [],
      activeGoal: {
        id: "goal-1",
        text: "Ship MiniCode desktop parity",
        status: "active",
      },
      currentProvider: "openai",
      currentModel: "gpt-5",
      contextUsage: {
        used: 7500,
        limit: 10000,
        compactSummary: "Earlier work summarized.",
      },
      workspaceGit: {
        branch: "minicode/parity",
        isWorktree: true,
        currentPath: "C:/Desktop/MiniCode-worktree",
        mainRepoPath: "C:/Desktop/MiniCode",
        worktreeCount: 2,
        isolatedCount: 1,
      },
      workingDirectory: "C:/Desktop/MiniCode",
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.summary).toMatchObject([
      { id: "goal", kind: "goal", label: "Ship MiniCode desktop parity", status: "running" },
      { id: "context", kind: "context", label: "7,500 / 10,000 tokens", detail: "Earlier work summarized." },
    ]);
    expect(state.summary.map((item) => item.id)).not.toContain("model");
    expect(state.workspace).toMatchObject([
      { id: "branch", kind: "branch", label: "minicode/parity" },
      {
        id: "worktree",
        kind: "worktree",
        label: "Isolated worktree",
        detail: "2 worktrees, 1 isolated",
        path: "C:/Desktop/MiniCode-worktree",
      },
    ]);
    expect(state.isEmpty).toBe(false);
  });

  it("treats non-git folders as neutral local workspace state", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [],
      todos: [],
      plan: null,
      agentProgress: [],
      workspaceGit: {
        isWorktree: false,
        currentPath: "C:/Desktop/temp",
        error: "Not a git repository",
      },
      workingDirectory: "C:/Desktop/temp",
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.workspace).toMatchObject([
      {
        id: "worktree",
        label: "Main workspace",
        detail: "C:/Desktop/temp",
        status: "info",
      },
    ]);
  });

  it("keeps skill lifecycle items out of the Output sidebar state", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      isStreaming: true,
      messages: [
        message({
          id: "assistant-skill",
          role: "assistant",
          blocks: [
            {
              type: "process",
              id: "skill-openai-docs-loaded",
              itemKind: "skill",
              content: "Loaded the OpenAI docs skill.",
              title: "Using Skill: openai-docs",
              status: "completed",
              visibility: "timeline",
              skillName: "openai-docs",
              triggerMode: "implicit",
              sourceLevel: "builtin",
              reason: "The user asked about MiniCode official docs.",
              timestamp: 1,
            },
          ],
        }),
      ],
      todos: [],
      plan: null,
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.isEmpty).toBe(true);
  });

  it("keeps raw tool evidence out of the Output sidebar state", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [
        message({
          id: "assistant-tools",
          role: "assistant",
          blocks: [
            {
              type: "tool_call",
              record: {
                id: "read-1",
                name: "read_file",
                args: { path: "src/app.ts" },
                status: "success",
                startedAt: 1,
                displayHint: "Read app.ts",
                displaySummary: "Loaded the app entry file",
                resultKind: "file",
              },
            },
            {
              type: "tool_call",
              record: {
                id: "hidden-1",
                name: "debug_probe",
                args: {},
                status: "success",
                startedAt: 2,
              },
            },
          ],
        }),
      ],
      todos: [],
      plan: null,
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
    });

    expect(state.sources).toEqual([]);
    expect(state.isEmpty).toBe(true);
  });

  it("derives attachment and terminal evidence without surfacing background tasks in Output", () => {
    const state = buildActivitySidebarState({
      conversationId: "conv-1",
      messages: [
        message({
          id: "user-attachments",
          role: "user",
          attachmentRefs: [
            {
              id: "att-1",
              name: "screen.png",
              kind: "image",
              mediaType: "image/png",
              sizeBytes: 2048,
              artifactId: "artifact-screen",
            },
          ],
        }),
        message({
          id: "assistant-attachments",
          role: "assistant",
          replyAttachments: [
            { path: "C:/Desktop/MiniCode/report.txt", size: 512, isImage: false },
          ],
        }),
      ],
      todos: [],
      plan: null,
      agentProgress: [],
      livePreviewUrl: null,
      previewArtifact: null,
      previewVerification: null,
      previewServers: [],
      previewLaunchProcesses: [],
      activeTerminalSessionId: "term-1",
      terminalSnapshots: {
        "term-1": {
          id: "term-1",
          conversationId: "conv-1",
          shell: "powershell",
          cwd: "C:/Desktop/MiniCode",
          status: "running",
          output: "npm run build",
          outputChars: 13,
          totalOutputChars: 13,
          capturedAt: 1,
        },
      },
      backgroundTasks: [
        { id: "task-1", command: "npm run dev", status: "running", timestamp: 1, conversationId: "conv-1" },
      ],
      scheduledTasks: [
        {
          id: "auto-1",
          name: "Morning check",
          prompt: "Check the build",
          schedule: "0 9 * * 1-5",
          permission_mode: "auto_approve",
          enabled: true,
          last_run_at: "2026-06-22T01:00:00.000Z",
        },
      ],
      browserAnnotations: [
        {
          id: "note-1",
          targetId: "target-1",
          url: "http://localhost:5173/settings",
          title: "Settings",
          selector: "#save",
          note: "Save button overlaps the footer on narrow screens.",
          createdAt: 10,
          screenshotCapturedAt: 9,
          screenshotWidth: 1280,
          screenshotHeight: 720,
        },
        {
          id: "note-2",
          targetId: "target-1",
          url: "http://localhost:5173/settings",
          title: "Settings",
          xPercent: 42.4,
          yPercent: 68.2,
          note: "Primary callout is too close to the viewport edge.",
          createdAt: 11,
          screenshotCapturedAt: 9,
          screenshotWidth: 1280,
          screenshotHeight: 720,
        },
      ],
    });

    expect(state.attachments).toMatchObject([
      {
        id: "C:/Desktop/MiniCode/report.txt",
        messageId: "assistant-attachments",
        label: "report.txt",
        kind: "file",
      },
      {
        id: "artifact-screen",
        messageId: "user-attachments",
        label: "screen.png",
        kind: "image",
        artifactId: "artifact-screen",
      },
    ]);
    expect(state.runs).toMatchObject([
      {
        id: "terminal:term-1",
        kind: "terminal",
        label: "powershell",
        status: "running",
        terminalId: "term-1",
      },
      {
        id: "background:task-1",
        kind: "background-command",
        label: "npm run dev",
        status: "running",
      },
      {
        id: "automation:auto-1",
        kind: "automation",
        label: "Morning check",
        status: "idle",
        automationId: "auto-1",
      },
    ]);
    expect(state.runs.map((item) => item.kind)).toContain("background-command");
    expect(state.runs.map((item) => item.kind)).toContain("automation");
    expect(state.browserAnnotations).toMatchObject([
      {
        id: "note-1",
        label: "#save",
        url: "http://localhost:5173/settings",
        host: "localhost:5173",
        note: "Save button overlaps the footer on narrow screens.",
        selector: "#save",
        title: "Settings",
        screenshotDetail: expect.stringContaining("1280x720"),
      },
      {
        id: "note-2",
        label: "Point 42%, 68%",
        xPercent: 42.4,
        yPercent: 68.2,
        screenshotDetail: expect.stringContaining("Point 42%, 68%"),
      },
    ]);
    expect(state.isEmpty).toBe(false);
  });
});
