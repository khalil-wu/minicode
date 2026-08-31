/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const {
  sendMock,
  sendClientCommandMock,
  sendClientCommandAwaitResultMock,
  fetchAttachmentPreviewMock,
} = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
  return {
    sendMock: vi.fn(),
    sendClientCommandMock: vi.fn(() => true),
    sendClientCommandAwaitResultMock: vi.fn(() => Promise.resolve({ status: "ok" })),
    fetchAttachmentPreviewMock: vi.fn(),
  };
});

const writeTextMock = vi.fn(() => Promise.resolve());
const scrollIntoViewMock = vi.fn();

vi.mock("../desktop/runtime", () => ({
  isDesktop: () => false,
  revealPath: vi.fn(),
}));

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://api.test",
  authHeaders: () => ({ Authorization: "Bearer test" }),
  fetchWithTimeout: (url: string, init?: RequestInit) => fetch(url, init),
  errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
  attachmentRawResourceUrlWithToken: (artifactId: string, sessionId: string, conversationId: string) => (
    `http://api.test/api/attachments/raw?artifact_id=${artifactId}&session_id=${sessionId}&conversation_id=${conversationId}`
  ),
  fetchAttachmentPreview: fetchAttachmentPreviewMock,
}));

vi.mock("../panels/BrowserPanel", () => ({
  BrowserPanel: () => <div>Browser Control panel</div>,
}));

vi.mock("../panels/PreviewPanel", () => ({
  PreviewPanel: () => <div>Preview panel</div>,
}));

vi.mock("../panels/DiffPanel", () => ({
  DiffPanel: () => <div>Diff panel</div>,
}));

vi.mock("../panels/TerminalPanel", () => ({
  TerminalPanel: () => <div>Terminal panel</div>,
}));

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ send: sendMock, sessionId: "session-sidebar-test" }),
}));

vi.mock("../protocol/ws-outbox", () => ({
  createClientCommandId: () => "preview-request-sidebar",
  commandResultSucceeded: () => true,
  sendClientCommand: sendClientCommandMock,
  sendClientCommandAwaitResult: sendClientCommandAwaitResultMock,
}));

import { useAppStore } from "../stores";
import { SidebarRight } from "./SidebarRight";
import type { ChatMessage } from "../stores/types";

const chatMessage = (patch: Partial<ChatMessage> & Pick<ChatMessage, "id" | "role">): ChatMessage => ({
  content: "",
  artifacts: [],
  timestamp: 1,
  ...patch,
});

const resetSidebarState = () => {
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: scrollIntoViewMock,
  });
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
    configurable: true,
    value: vi.fn(),
  });
  Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
    configurable: true,
    value: vi.fn(),
  });
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", {
    configurable: true,
    value: vi.fn(() => true),
  });
  useAppStore.setState({
    messages: [],
    rightStackTab: "diagnostics",
    rightStackTabLocked: false,
    rightPanelOpen: true,
    rightSidebarWidth: 380,
    plan: null,
    todos: [],
    subagents: [],
    livePreviewUrl: "",
    previewArtifact: null,
    previewVerification: null,
    previewServers: [],
    previewLaunchProcesses: [],
    diffReview: null,
    quickOpenVisible: false,
    sideChatOpen: false,
    terminalSessions: [],
    terminalSnapshots: {},
    activeTerminalSessionId: null,
    activeBottomTab: "terminal",
    dockCollapsed: true,
    backgroundTasks: [],
    scheduledTasks: [],
    browserAnnotations: [],
    mcpServers: [],
    conversations: [],
    activeGoal: null,
    contextUsage: null,
    currentModel: "gpt-5.4",
    runtimeCapabilities: null,
    workingDirectory: "C:\\Desktop\\MiniCode",
    workspaceGit: { branch: "main", isWorktree: false },
    conversationWorkbenchStates: {},
  });
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: writeTextMock },
  });
  sendMock.mockClear();
  sendClientCommandMock.mockClear();
  sendClientCommandAwaitResultMock.mockClear();
  fetchAttachmentPreviewMock.mockReset();
  writeTextMock.mockClear();
  scrollIntoViewMock.mockClear();
};

describe("SidebarRight activity", () => {
  beforeEach(() => {
    resetSidebarState();
  });

  afterEach(() => {
    cleanup();
  });

  it("starts with one quiet primary tab and keeps other panels in the launcher", () => {
    useAppStore.setState({ rightStackTab: "tasks" });
    render(<SidebarRight />);

    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(screen.getByRole("tab", { name: /打开上下文/ })).toBeTruthy();
    expect(screen.getAllByText("上下文")).toHaveLength(1);
    expect(screen.queryByRole("tab", { name: /打开子智能体/ })).toBeNull();
    expect(screen.queryByRole("tab", { name: /打开产物/ })).toBeNull();
  });

  it("keeps panel actions outside the scrollable tab strip", () => {
    render(<SidebarRight />);

    const tablist = screen.getByRole("tablist", { name: "右侧栏面板" });
    const actions = screen.getByTestId("right-sidebar-actions");

    expect(tablist.className).toContain("mc-sidebar-right-tabs");
    expect(tablist.nextElementSibling).toBe(actions);
    expect(actions.contains(screen.getByRole("button", { name: "添加面板" }))).toBe(true);
    expect(actions.contains(screen.getByRole("button", { name: "关闭右侧栏" }))).toBe(true);
  });

  it("opens Inspector explicitly from the panel launcher", () => {
    useAppStore.setState({ rightStackTab: "tasks", rightStackTabLocked: false });
    render(<SidebarRight />);

    fireEvent.click(screen.getByRole("button", { name: "添加面板" }));
    const inspectorButton = screen.getByRole("button", { name: "运行详情" });
    expect(inspectorButton).toBeTruthy();

    fireEvent.click(inspectorButton);

    expect(useAppStore.getState()).toMatchObject({
      rightStackTab: "inspector",
      rightStackTabLocked: true,
    });
    expect(screen.getByRole("tab", { name: /打开运行详情/ })).toBeTruthy();
  });

  it("renders Activity as a compact runtime center with task, workspace, and evidence", async () => {
    useAppStore.setState({
      conversationId: "conv-activity",
      conversations: [{
        id: "conv-activity",
        title: "Activity",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      activeGoal: {
        id: "goal-activity",
        text: "Ship calm output UI",
        status: "active",
      },
      messages: [
        chatMessage({
          id: "assistant-activity",
          role: "assistant",
          content: "Final answer [1].",
          artifacts: [{ artifactId: "artifact-1", kind: "image", summary: "preview.png", mediaType: "image/png", url: "blob:test" }],
          citations: [{ source: "https://docs.example/guide", url: "https://docs.example/guide", label: "Docs", title: "Guide", range: [0, 0] }],
        }),
      ],
      todos: [{ id: "todo-1", content: "Inspect files", activeForm: "Inspecting files", status: "in_progress" }],
      livePreviewUrl: "http://localhost:5173",
      previewVerification: { url: "http://localhost:5173", ok: true, elapsed_ms: 31, checkedAt: 1 },
      previewServers: [{ name: "Vite", port: 5173, url: "http://localhost:5173", framework: "React" }],
      scheduledTasks: [{
        id: "auto-activity",
        name: "Daily smoke",
        prompt: "Run smoke checks",
        schedule: "0 9 * * 1-5",
        permission_mode: "auto_approve",
        enabled: true,
      }],
      browserAnnotations: [{
        id: "note-activity",
        targetId: "target-activity",
        url: "http://localhost:5173",
        title: "Local preview",
        xPercent: 25,
        yPercent: 75,
        note: "Main app frame is visible.",
        createdAt: 1,
      }],
    });

    render(<SidebarRight embedded initialTab="tasks" />);

    const text = document.body.textContent ?? "";
    expect(text).toContain("任务");
    expect(text.indexOf("来源")).toBeLessThan(text.lastIndexOf("输出"));
    expect(text.lastIndexOf("输出")).toBeLessThan(text.indexOf("页面备注"));
    expect(text.indexOf("页面备注")).toBeLessThan(text.indexOf("浏览器"));
    expect(screen.getByText("Ship calm output UI - 进行中的目标")).toBeTruthy();
    expect(screen.getByText("Inspecting files")).toBeTruthy();
    expect(screen.getByText("main")).toBeTruthy();
    expect(screen.queryByText(/Main workspace/)).toBeNull();
    expect(screen.getByText("preview.png")).toBeTruthy();
    expect(screen.getByText("Vite")).toBeTruthy();
    expect(screen.getByText("Docs")).toBeTruthy();
    expect(screen.getByText("Guide")).toBeTruthy();
    expect(screen.getByText("Daily smoke")).toBeTruthy();
    expect(screen.getByText("Point 25%, 75%")).toBeTruthy();
    expect(document.querySelector('a[href="https://docs.example/guide"]')).toBeNull();

    fireEvent.click(screen.getByText("Docs"));
    expect(useAppStore.getState().rightStackTab).toBe("browser");
    expect(useAppStore.getState().livePreviewUrl).not.toBe("https://docs.example/guide");
    expect(sendMock).not.toHaveBeenCalledWith({ type: "preview.navigate", url: "https://docs.example/guide" });

    useAppStore.getState().setSettingsTab("general");
    fireEvent.click(screen.getByText("Daily smoke"));

    expect(useAppStore.getState().automationsOpen).toBe(false);
    expect(useAppStore.getState().settingsOpen).toBe(true);
    await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("scheduler"));
  });

  it("keeps tool and skill internals out of Activity while preserving human-readable task progress", () => {
    useAppStore.setState({
      conversationId: "conv-task-sidebar",
      conversations: [{
        id: "conv-task-sidebar",
        title: "Task sidebar",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      messages: [
        chatMessage({
          id: "assistant-task-sidebar",
          role: "assistant",
          content: "Working.",
          blocks: [
            {
              type: "process",
              id: "skill-hidden",
              itemKind: "skill",
              title: "已加载 Skill: frontend-dev",
              content: "已加载 Skill: frontend-dev。",
              status: "completed",
              source: "runtime",
              visibility: "timeline",
              skillName: "frontend-dev",
            },
            {
              type: "tool_call",
              record: {
                id: "tool-hidden",
                name: "run_command",
                args: { command: "npm test" },
                status: "success",
                summary: "Exit code: 0",
              },
            },
          ],
        }),
      ],
      todos: [{ id: "todo-1", content: "Check task", activeForm: "Checking task", status: "in_progress" }],
    });

    render(<SidebarRight embedded initialTab="tasks" />);

    expect(screen.getByText("Checking task")).toBeTruthy();
    expect(screen.queryByText("Skills")).toBeNull();
    expect(screen.queryByText("Tools")).toBeNull();
    expect(screen.queryByText(/frontend-dev/)).toBeNull();
    expect(screen.queryByText(/Run Command/i)).toBeNull();
  });

  it("shows run-level tool timeline in the Inspector sidebar", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/replay/")) {
        return new Response(JSON.stringify({
          kind: "minicode_ws_replay_export",
          schema_version: 1,
          session_id: "session-sidebar-test",
          conversation_id: "conv-context-tools",
          after_seq: 0,
          current_seq: 7,
          event_count: 2,
          first_seq: 6,
          last_seq: 7,
          sequence_gaps: [],
          can_replay_without_gap: true,
          type_counts: { "agent_message.delta": 1, done: 1 },
          omitted_fields: [],
          truncated_fields: [],
          events: [{ type: "agent_message.delta", seq: 6 }, { type: "done", seq: 7 }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({ branch: "main", modified: [], staged: [], untracked: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    useAppStore.setState({
      conversationId: "conv-context-tools",
      conversations: [{
        id: "conv-context-tools",
        title: "Context tools",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      runtimeCapabilities: {
        feature_flags: {
          agent_trace_export_v1: { enabled: true, source: "settings" },
        },
      },
      messages: [
        chatMessage({
          id: "assistant-context-tools",
          role: "assistant",
          timestamp: 1000,
          blocks: [{
            type: "tool_call",
            record: {
              id: "tool-context",
              name: "run_command",
              args: { command: "npm test" },
              status: "success",
              displaySummary: "Ran command",
              inputSummary: "npm test",
              summary: "Exit code: 0",
              groupId: "batch-1",
              startedAt: 1350,
              finishedAt: 1750,
            },
          }, {
            type: "tool_call",
            record: {
              id: "tool-context-read",
              name: "read_file",
              args: { file_path: "package.json" },
              status: "success",
              displaySummary: "Read file",
              inputSummary: "package.json",
              summary: "Read package.json",
              groupId: "batch-1",
              startedAt: 1360,
              finishedAt: 1460,
            },
          }],
        }),
      ],
      agentProgress: [{
        type: "progress",
        id: "run-started",
        stage: "status",
        phase: "status",
        status: "info",
        message: "run.started",
        timestamp: 1000,
      }, {
        type: "progress",
        id: "provider-first-token",
        stage: "planning",
        phase: "model",
        status: "info",
        message: "provider.first_token",
        timestamp: 1200,
      }, {
        type: "progress",
        id: "tool-preparing",
        stage: "tool",
        phase: "tool",
        status: "completed",
        message: "tool preparing",
        toolCallId: "tool-context",
        timestamp: 1300,
      }, {
        type: "progress",
        id: "recover-ok",
        stage: "status",
        phase: "recover",
        status: "completed",
        message: "recovery.succeeded",
        timestamp: 1800,
      }, {
        type: "progress",
        id: "subagent:agent-1",
        stage: "status",
        phase: "subagent",
        status: "completed",
        message: "Reviewed runtime spans",
        timestamp: 1900,
      }, {
        type: "progress",
        id: "workflow:workflow-1",
        stage: "status",
        phase: "workflow",
        status: "completed",
        message: "Workflow completed",
        timestamp: 2100,
      }, {
        type: "progress",
        id: "cache:provider.prompt:sig",
        stage: "status",
        phase: "cache",
        status: "completed",
        message: "Cache hit: provider.prompt",
        visibility: "debug",
        timestamp: 2200,
      }],
    });

    render(<SidebarRight embedded initialTab="inspector" />);

    expect(await screen.findByText("会话", {}, { timeout: 5_000 })).toBeTruthy();
    expect(screen.queryByText("运行记录")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /高级诊断/ }));
    expect(screen.getByText("运行记录")).toBeTruthy();
    expect(screen.getByText("性能指标")).toBeTruthy();
    expect(screen.getByRole("button", { name: "查看最近事件" })).toBeTruthy();
    expect(screen.queryByText("回放预览")).toBeNull();
    expect(screen.queryByText("Ran command")).toBeNull();
    expect(screen.getByText("200 ms")).toBeTruthy();
    expect(screen.getByText("50 ms")).toBeTruthy();
    expect(screen.getByText("250 ms")).toBeTruthy();
    expect(screen.getByText("2 个工具 / 1 组")).toBeTruthy();
    expect(screen.getByText("1 个子智能体阶段 · 1 个缓存阶段")).toBeTruthy();
    expect(screen.getByText("1/1 成功")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "查看最近事件" }));

    expect(screen.getByText("回放预览")).toBeTruthy();
    expect(screen.getAllByText("Ran command").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Read file").length).toBeGreaterThan(0);
    expect(screen.getByText("回放已完成")).toBeTruthy();
    expect(screen.getAllByText("tool.completed").length).toBeGreaterThan(0);
    expect(screen.getByText("400ms")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "复制 JSONL" }));
    expect(writeTextMock).toHaveBeenCalledOnce();
    const exported = String(writeTextMock.mock.calls[0][0])
      .split("\n")
      .map((line) => JSON.parse(line))
      .find((event) => event.label === "Ran command");
    expect(exported).toMatchObject({
      kind: "minicode_run_timeline_event",
      phase: "tool",
      status: "completed",
      label: "Ran command",
      tool_name: "run_command",
    });

    writeTextMock.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "复制回放" }));
    expect(writeTextMock).toHaveBeenCalledOnce();
    const replayed = String(writeTextMock.mock.calls[0][0])
      .split("\n")
      .map((line) => JSON.parse(line))
      .find((event) => event.label === "Ran command");
    expect(replayed).toMatchObject({
      kind: "minicode_run_replay_event",
      schema_version: 1,
      phase: "tool",
      status: "completed",
      label: "Ran command",
      tool_name: "run_command",
    });

    writeTextMock.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "复制会话回放" }));
    await waitFor(() => expect(writeTextMock).toHaveBeenCalledOnce());
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/replay/session-sidebar-test?"),
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(String(fetchMock.mock.calls.find(([url]) => String(url).includes("/api/replay/"))?.[0])).toContain("conversation_id=conv-context-tools");
    expect(JSON.parse(String(writeTextMock.mock.calls[0][0]))).toMatchObject({
      kind: "minicode_ws_replay_export",
      event_count: 2,
      can_replay_without_gap: true,
    });
    fetchMock.mockRestore();
  });

  it("renders provider trace summaries in the Inspector tab", async () => {
    useAppStore.setState({
      conversationId: "conv-provider-trace",
      conversations: [{
        id: "conv-provider-trace",
        title: "Provider trace",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      runtimeCapabilities: {
        feature_flags: {
          agent_trace_export_v1: { enabled: true, source: "settings" },
        },
      },
      inspectorEntries: [
        {
          targetKind: "provider",
          targetId: "trace-0",
          timestamp: 0,
          payload: {
            kind: "provider_trace",
            provider: "openai_responses",
            model: "gpt-5.4",
            finish_reason: "tool_calls",
            usage: { input_tokens: 80, output_tokens: 10 },
            output_items: [{ type: "function_call", index: 0, name: "shell_command" }],
            provider_timeline: [{ event: "response.completed", output_items_len: 1 }],
            request_summary: {
              instructions_len: 21459,
              instructions_hash: "27060ff34e82",
              tools_len: 1,
              tools_hash: "abcdef123456",
              tool_names: ["shell_command"],
              prompt_cache_key_present: true,
              prompt_cache_key_hash: "cache1234567",
              request_params: { stream: true, store: false, reasoning: { effort: "low", summary: "auto" } },
              input_items_len: 5,
              input_item_counts: { message: 4, function_call_output: 1 },
              metadata_keys: ["cwd", "turn_id"],
            },
            safety: { redacted_prompt: true },
          },
        },
        {
          targetKind: "provider",
          targetId: "trace-1",
          timestamp: 1,
          payload: {
            kind: "provider_trace",
            provider: "openai_responses",
            model: "gpt-5.4",
            finish_reason: "stop",
            usage: {
              input_tokens: 100,
              output_tokens: 20,
              cache_read_input_tokens: 90,
              prompt_cache_total_tokens: 100,
              prompt_cache_hit_rate: 90,
              reasoning_output_tokens: 7,
            },
            output_items: [
              { type: "reasoning", index: 0, has_encrypted_content: true },
              { type: "message", index: 1, role: "assistant", phase: "commentary", content_types: ["output_text"] },
              { type: "function_call", index: 2, name: "shell_command" },
            ],
            provider_timeline: [
              { event: "response.created", response_id_hash: "respabc12345", status: "in_progress" },
              { event: "response.output_item.added", item_type: "reasoning" },
              { event: "response.function_call_arguments.delta", name: "shell_command", delta_chars: 80 },
              { event: "response.completed", response_id_hash: "respabc12345", output_items_len: 2, usage_present: true },
            ],
            request_summary: {
              instructions_len: 21459,
              instructions_hash: "27060ff34e82",
              tools_len: 2,
              tools_hash: "fedcba654321",
              tool_names: ["shell_command", "web_search"],
              prompt_cache_key_present: true,
              prompt_cache_key_hash: "cache1234567",
              request_params: { stream: true, store: false, prompt_cache_retention: "24h", reasoning: { effort: "high", summary: "auto" } },
              input_items_len: 7,
              input_item_counts: { message: 5, function_call_output: 2 },
              metadata_keys: ["cwd", "turn_id"],
              prompt_section_summary: {
                section_count: 4,
                total_chars: 1200,
                layers: {
                  stable: { chars: 800, sections: 1, cache_break_sections: 0 },
                  context: { chars: 250, sections: 2, cache_break_sections: 0 },
                  volatile: { chars: 150, sections: 1, cache_break_sections: 1 },
                },
                largest_sections: [
                  { name: "stable_system", layer: "stable", chars: 800 },
                  { name: "workspace_summary", layer: "context", chars: 180 },
                ],
              },
            },
            loop_metrics: {
              provider_call_count: 7,
              iteration: 7,
              iteration_limit: 12,
              iteration_hard_limit: 60,
              tool_batch_count: 6,
              tool_call_count: 24,
              elapsed_ms: 185000,
              dynamic_iteration_budget_enabled: true,
            },
            prompt_cache_diagnostic: {
              reason: "prompt sections changed",
              token_drop: 6000,
              prompt_section_delta: {
                status: "changed",
                added: ["skill_context"],
                removed: ["workspace_summary"],
                changed_sections: [
                  { name: "stable_system", changes: ["content"], chars_delta: 30 },
                ],
                layer_char_deltas: { stable: 30, context: -120, volatile: 0 },
              },
            },
            safety: { redacted_prompt: true, has_encrypted_reasoning: true },
          },
        },
      ],
      inspectorFocus: { kind: "provider", id: "trace-1" },
    });

    render(<SidebarRight embedded initialTab="inspector" />);

    fireEvent.click(screen.getByRole("button", { name: /高级诊断/ }));
    expect(await screen.findByText("2 次", {}, { timeout: 5_000 })).toBeTruthy();
    expect(screen.getByText("27060ff34e82 · 21,459 chars")).toBeTruthy();
    expect(screen.getByText("reasoning:encrypted -> message:assistant:commentary:output_text -> function_call:shell_command")).toBeTruthy();
    expect(screen.getAllByText("commentary x1").length).toBeGreaterThan(0);
    expect(screen.getByText(/response\.completed x1, response\.created x1, response\.function_call_arguments\.delta x1, response\.output_item\.added x1/)).toBeTruthy();
    expect(screen.getByText(/response\.created x1 -> response\.completed x1 · response respabc12345/)).toBeTruthy();
    expect(screen.getAllByText(/response\.function_call_arguments\.delta/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/shell_command/).length).toBeGreaterThan(0);
    expect(screen.getByText(/responses · retention 24h · store false/)).toBeTruthy();
    expect(screen.getByText(/7 provider calls · iter 7\/12\/60 · 6 tool batches · 24 tools · 185000ms/)).toBeTruthy();
    expect(screen.getByText(/cache hit is high; latency is likely loop\/tool-bound/)).toBeTruthy();
    expect(screen.getByText(/prompt cache key cache1234567/)).toBeTruthy();
    expect(screen.getByText(/encrypted reasoning present; content redacted/)).toBeTruthy();
    expect(screen.getByText(/4 sections · 1200 chars · stable 800 chars \/ 1 sections/)).toBeTruthy();
    expect(screen.getByText(/stable_system \(stable, 800 chars\) · workspace_summary \(context, 180 chars\)/)).toBeTruthy();
    expect(screen.getByText(/prompt sections changed · token drop 6000/)).toBeTruthy();
    expect(screen.getByText(/added skill_context · removed workspace_summary · changed 1 section/)).toBeTruthy();
    expect(screen.getByText(/stable \+30 chars · context -120 chars/)).toBeTruthy();
    expect(screen.getByText(/stable_system \[content, \+30 chars\]/)).toBeTruthy();
    expect(screen.getByText(/Prompt unchanged · Tools changed \(\+web_search\) · Params changed \(prompt_cache_retention, reasoning\) · Cache routing unchanged · Input items \+2 \(function_call_output \+1, message \+1\) · Metadata keys unchanged · Request scaffold changed/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Copy Trace/ }));
    expect(writeTextMock).toHaveBeenCalledOnce();
    const exported = JSON.parse(String(writeTextMock.mock.calls[0][0]));
    expect(exported).toMatchObject({
      kind: "minicode_provider_trace_export",
      provider: "openai_responses",
      output_sequence: "reasoning:encrypted -> message:assistant:commentary:output_text -> function_call:shell_command",
      output_phase_counts: "commentary x1",
      response_lifecycle: "response.created x1 -> response.completed x1 · response respabc12345",
      provider_timeline_event_counts: "response.completed x1, response.created x1, response.function_call_arguments.delta x1, response.output_item.added x1",
      request_summary: {
        instructions_hash: "27060ff34e82",
        prompt_cache_key_hash: "cache1234567",
      },
      prompt_cache_diagnostic: {
        reason: "prompt sections changed",
      },
      loop_metrics: {
        provider_call_count: 7,
        tool_call_count: 24,
      },
      request_diff_summary: [
        "Prompt unchanged",
        "Tools changed (+web_search)",
        "Params changed (prompt_cache_retention, reasoning)",
        "Cache routing unchanged",
        "Input items +2 (function_call_output +1, message +1)",
        "Metadata keys unchanged",
        "Request scaffold changed",
      ],
    });
    expect(exported.diagnostics).toEqual(expect.arrayContaining([
      "encrypted reasoning present; content redacted",
      "prompt section delta captured",
      "cache hit is high; latency is likely loop/tool-bound",
    ]));
    expect(String(writeTextMock.mock.calls[0][0])).not.toContain("VERY PRIVATE");
  });

  it("keeps Run Timeline visible while hiding trace export controls behind the export flag", async () => {
    useAppStore.setState({
      conversationId: "conv-no-trace-export",
      conversations: [{
        id: "conv-no-trace-export",
        title: "No export",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      runtimeCapabilities: {
        feature_flags: {
          agent_trace_export_v1: { enabled: false, source: "settings" },
        },
      },
      messages: [
        chatMessage({
          id: "assistant-no-trace-export",
          role: "assistant",
          blocks: [{
            type: "tool_call",
            record: {
              id: "tool-no-export",
              name: "run_command",
              args: { command: "npm test" },
              status: "success",
              summary: "Exit code: 0",
              startedAt: 1,
              finishedAt: 2,
            },
          }],
        }),
      ],
    });

    render(<SidebarRight embedded initialTab="inspector" />);

    fireEvent.click(screen.getByRole("button", { name: /高级诊断/ }));
    expect(await screen.findByText("运行记录", {}, { timeout: 5_000 })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "复制 JSONL" })).toBeNull();
    expect(screen.queryByRole("button", { name: "下载 JSONL" })).toBeNull();
    expect(screen.queryByRole("button", { name: "复制回放" })).toBeNull();
    expect(screen.queryByRole("button", { name: "复制会话回放" })).toBeNull();
  });

  it("opens generated artifacts from the Activity section in preview", () => {
    useAppStore.setState({
      conversationId: "conv-artifact",
      conversations: [{
        id: "conv-artifact",
        title: "Artifact",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      messages: [
        chatMessage({
          id: "assistant-artifact",
          role: "assistant",
          artifacts: [{ artifactId: "artifact-42", kind: "file", summary: "report.md", mediaType: "text/markdown" }],
        }),
      ],
    });

    render(<SidebarRight embedded initialTab="tasks" />);

    fireEvent.click(screen.getByRole("button", { name: /report\.md/ }));

    expect(useAppStore.getState().rightStackTab).toBe("preview");
    expect(sendClientCommandAwaitResultMock).toHaveBeenCalledWith({
      type: "read_artifact",
      artifact_id: "artifact-42",
      conversation_id: "conv-artifact",
      request_id: "preview-request-sidebar",
      client_command_id: "preview-request-sidebar",
    }, "read_artifact");
  });

  it("summarizes cache lookup diagnostics in the Inspector tab", async () => {
    useAppStore.setState({
      conversationId: "conv-cache",
      conversations: [{
        id: "conv-cache",
        title: "Cache",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      inspectorEntries: [
        {
          targetKind: "cache",
          targetId: "grep:sig",
          timestamp: 0,
          payload: {
            kind: "cache_metric",
            type: "cache.lookup",
            cache_layer: "grep_files.search",
            tool_name: "grep_files",
            args_signature: "sig",
            hit: true,
            stale: false,
            evicted: false,
            estimated_saved_ms: 120,
            payload_size_bytes: 42,
          },
        },
      ],
    });

    render(<SidebarRight embedded initialTab="inspector" />);

    fireEvent.click(screen.getByRole("button", { name: /高级诊断/ }));
    expect(await screen.findByText("1/1 hits (100%)")).toBeTruthy();
    expect(screen.getByText("120 ms estimated")).toBeTruthy();
    expect(screen.getByText("grep_files.search")).toBeTruthy();
    expect(screen.getByText("性能指标")).toBeTruthy();
    expect(screen.getByText("120 ms · 命中 100%")).toBeTruthy();
    expect(screen.getByText("1 个缓存信号")).toBeTruthy();
  });

  it("opens uploaded image attachments in the unified Preview panel", async () => {
    fetchAttachmentPreviewMock.mockResolvedValue({
      artifact_id: "artifact-image-99",
      file_name: "photo.png",
      media_type: "image/png",
      kind: "image",
      content: "",
      size_bytes: 128,
      content_chars: 0,
      truncated: false,
      has_native: true,
    });
    useAppStore.setState({
      conversationId: "conv-attachment",
      conversations: [{
        id: "conv-attachment",
        title: "Attachment",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      rightStackTab: "tasks",
      messages: [
        chatMessage({
          id: "user-attachment",
          role: "user",
          attachmentRefs: [{
            id: "att-image",
            name: "photo.png",
            kind: "image",
            mediaType: "image/png",
            sizeBytes: 128,
            artifactId: "artifact-image-99",
          }],
        }),
      ],
    });

    render(<SidebarRight embedded initialTab="tasks" />);

    fireEvent.click(screen.getByRole("button", { name: /photo\.png/ }));

    expect(useAppStore.getState().rightStackTab).toBe("preview");
    expect(fetchAttachmentPreviewMock).toHaveBeenCalledWith(
      "session-sidebar-test",
      "conv-attachment",
      "artifact-image-99",
      expect.any(AbortSignal),
    );
    await waitFor(() => expect(useAppStore.getState().previewArtifact).toMatchObject({
      artifactId: "artifact-image-99",
      name: "photo.png",
      mediaType: "image/png",
      url: expect.stringContaining("/api/attachments/raw"),
      source: "attachment",
    }));
  });

  it("shows an empty state without leaking previous conversation data when no conversation is active", () => {
    useAppStore.setState({
      conversationId: null,
      messages: [
        chatMessage({
          id: "assistant-old",
          role: "assistant",
          artifacts: [{ artifactId: "old", kind: "file", summary: "old-output.txt" }],
        }),
      ],
      todos: [{ id: "todo-old", content: "Old task", activeForm: "Old task", status: "in_progress" }],
      livePreviewUrl: "http://localhost:5173",
    });

    render(<SidebarRight embedded initialTab="tasks" />);

    expect(screen.getByText("暂无当前会话")).toBeTruthy();
    expect(screen.queryByText("old-output.txt")).toBeNull();
    expect(screen.queryByText("Old task")).toBeNull();
    expect(screen.queryByText("localhost:5173")).toBeNull();
  });

  it("normalizes stale plan sidebar state back to Activity", () => {
    useAppStore.setState({
      conversationId: "conv-plan-empty",
      conversations: [{
        id: "conv-plan-empty",
        title: "No plan",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      rightStackTab: "plan",
      agentProgress: [{
        type: "progress",
        id: "progress-main",
        stage: "final",
        phase: "final",
        status: "running",
        message: "main",
        summary: "Final answer ready",
        visibility: "compact",
        timestamp: 1,
        conversationId: "conv-plan-empty",
      }],
    });

    render(<SidebarRight embedded initialTab="plan" />);

    expect(screen.queryByText("No proposed plan in this session.")).toBeNull();
    expect(screen.queryByRole("tab", { name: /Open Plan/i })).toBeNull();
    expect(document.body.textContent).toContain("Final answer ready");
    expect(document.body.textContent).toContain("main");
  });

  it("shows compact running progress in Activity without auto-switching the sidebar", async () => {
    useAppStore.setState({
      conversationId: "conv-running-activity",
      conversations: [{
        id: "conv-running-activity",
        title: "Running",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      rightStackTab: "preview",
      rightPanelOpen: true,
      plan: {
        planId: "plan-running",
        status: "executing",
        currentStep: 0,
        steps: [{ id: "step-1", title: "Check result", status: "running" }],
      },
      agentProgress: [{
        type: "progress",
        id: "progress-running",
        stage: "verification",
        phase: "verify",
        status: "running",
        message: "Checking work",
        summary: "Checking work",
        visibility: "compact",
        timestamp: 1,
        conversationId: "conv-running-activity",
      }],
    });

    render(<SidebarRight />);

    await waitFor(() => {
      expect(useAppStore.getState().rightStackTab).toBe("preview");
    });
    expect(await screen.findByText("Preview panel")).toBeTruthy();
  });

  it("resizes the right sidebar from the left edge handle", () => {
    useAppStore.setState({
      rightStackTab: "diff",
      rightPanelOpen: true,
      rightSidebarWidth: 380,
    });

    render(<SidebarRight />);

    const handle = screen.getByRole("separator", { name: "调整右侧栏宽度" });
    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 800 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 620 });

    expect(useAppStore.getState().rightSidebarWidth).toBe(600);
    expect(document.body.classList.contains("layout-dragging")).toBe(true);

    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(document.body.classList.contains("layout-dragging")).toBe(false);

    fireEvent.doubleClick(handle);

    expect(useAppStore.getState().rightSidebarWidth).toBe(640);
  });

  it("supports keyboard resizing from the right sidebar separator", () => {
    useAppStore.setState({ rightStackTab: "tasks", rightPanelOpen: true, rightSidebarWidth: 380 });
    render(<SidebarRight />);
    const handle = screen.getByRole("separator", { name: "调整右侧栏宽度" });

    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(useAppStore.getState().rightSidebarWidth).toBe(396);
    fireEvent.keyDown(handle, { key: "Home" });
    expect(useAppStore.getState().rightSidebarWidth).toBe(360);
    fireEvent.keyDown(handle, { key: "End" });
    expect(useAppStore.getState().rightSidebarWidth).toBe(1040);
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(useAppStore.getState().rightSidebarWidth).toBe(380);
  });

  it("keeps Review available in the primary tabs", () => {
    useAppStore.setState({
      gitChanges: {
        workingTree: [{
          path: "src/app.ts",
          patch: "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
          additions: 1,
          deletions: 1,
        }],
        staged: [],
        untracked: ["src/new.ts"],
        loading: false,
      },
    });

    render(<SidebarRight />);

    const reviewTab = screen.getByRole("tab", { name: /打开审阅/i });
    expect(reviewTab).toBeTruthy();

    fireEvent.click(reviewTab);

    expect(screen.getByText("Diff panel")).toBeTruthy();
    expect(useAppStore.getState().rightStackTab).toBe("diff");
  });

  it("keeps global file, chat, and terminal actions out of the right-panel launcher", () => {
    useAppStore.setState({
      rightStackTab: "diff",
      rightStackTabLocked: true,
      quickOpenVisible: false,
    });

    render(<SidebarRight />);

    fireEvent.click(screen.getByRole("button", { name: "添加面板" }));
    expect(screen.getByRole("navigation", { name: "面板选择" })).toBeTruthy();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.queryByRole("button", { name: "文件" })).toBeNull();
    expect(screen.queryByRole("button", { name: "侧边对话" })).toBeNull();
    expect(screen.queryByRole("button", { name: "终端" })).toBeNull();
    expect(screen.getByRole("button", { name: "上下文" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "预览" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "产物" })).toBeTruthy();
    expect(useAppStore.getState().quickOpenVisible).toBe(false);
    expect(useAppStore.getState().rightStackTab).toBe("diff");
  });

  it("keeps the desktop card mounted while open state fades", () => {
    useAppStore.setState({ rightPanelOpen: false, rightStackTab: "tasks" });
    const { container } = render(<SidebarRight />);
    const sidebar = container.querySelector<HTMLElement>(".mc-sidebar-right");

    expect(sidebar?.dataset.open).toBe("false");
    expect(sidebar?.style.position).toBe("relative");

    act(() => useAppStore.setState({ rightPanelOpen: true }));

    expect(container.querySelector(".mc-sidebar-right")).toBe(sidebar);
    expect(sidebar?.dataset.open).toBe("true");
  });

  it("opens Browser Control from the in-panel launcher", async () => {
    useAppStore.setState({
      rightStackTab: "tasks",
      rightStackTabLocked: true,
    });

    render(<SidebarRight />);

    fireEvent.click(screen.getByRole("button", { name: "添加面板" }));
    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));

    expect(useAppStore.getState().rightStackTab).toBe("browser");
    expect(await screen.findByText("Browser Control panel")).toBeTruthy();
  });

  it("scrolls the Agents tab into view when the summary card opens it", async () => {
    useAppStore.setState({
      rightStackTab: "tasks",
      rightStackTabLocked: true,
      subagents: [],
    });

    render(<SidebarRight />);

    scrollIntoViewMock.mockClear();
    act(() => useAppStore.getState().setRightStackTab("subagents"));

    expect(useAppStore.getState().rightStackTab).toBe("subagents");
    const agentsTab = screen.getByRole("tab", { name: "打开子智能体" });
    const agentsTabFrame = agentsTab.closest<HTMLElement>(".mc-sidebar-right-tab-frame");
    expect(agentsTab.querySelector("svg.lucide-bot")).toBeTruthy();
    expect(agentsTabFrame?.dataset.sidebarTabFrame).toBe("subagents");
    await waitFor(() => expect(scrollIntoViewMock).toHaveBeenCalled());
    expect(scrollIntoViewMock.mock.instances.at(-1)).toBe(agentsTabFrame);
    expect(scrollIntoViewMock).toHaveBeenLastCalledWith({ inline: "nearest", block: "nearest" });
  });

  it("keeps the trailing close control reachable after several panels open", () => {
    useAppStore.setState({ rightStackTab: "tasks", rightStackTabLocked: true });
    render(<SidebarRight />);

    for (const label of ["子智能体", "产物", "运行详情", "浏览器"]) {
      fireEvent.click(screen.getByRole("button", { name: "添加面板" }));
      fireEvent.click(screen.getByRole("button", { name: label, exact: true }));
    }

    const browserTab = screen.getByRole("tab", { name: "打开浏览器" });
    const browserFrame = browserTab.closest<HTMLElement>(".mc-sidebar-right-tab-frame");
    expect(scrollIntoViewMock.mock.instances.at(-1)).toBe(browserFrame);

    fireEvent.click(screen.getByRole("button", { name: "关闭浏览器标签页" }));

    expect(screen.queryByRole("tab", { name: "打开浏览器" })).toBeNull();
    expect(useAppStore.getState().rightStackTab).toBe("inspector");
  });

  it("shows delegated work after an external Agents navigation request", () => {
    useAppStore.setState({
      rightStackTab: "tasks",
      rightStackTabLocked: true,
      subagents: [{ id: "subagent-1", role: "verification", status: "done", summary: "Checked tests" }],
    });

    render(<SidebarRight />);

    act(() => useAppStore.getState().setRightStackTab("subagents"));

    expect(useAppStore.getState().rightStackTab).toBe("subagents");
    expect(screen.getByRole("tab", { name: "打开子智能体" })).toBeTruthy();
  });
});

describe("SidebarRight diagnostics", () => {
  beforeEach(() => {
    resetSidebarState();
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({
        backend: { status: "ok", active_sessions: 1 },
        llm: { provider: "openai", active_model: "gpt-5.4" },
        mcp: [],
        capabilities: {
          tools: [
            { type: "function", function: { name: "read_file" } },
            { type: "function", function: { name: "write_file" } },
            { type: "function", function: { name: "list_mcp_resources" } },
            { type: "function", function: { name: "tool_search" } },
          ],
          tool_views: [
            {
              name: "read_file",
              exposure: "core",
              direct: true,
              schema_available: true,
              toolset: "core",
              capability: "filesystem.read",
              permission: "ask",
              read_only: true,
              short_description: "Read files from the workspace",
            },
            {
              name: "tool_call",
              exposure: "deferred",
              direct: false,
              schema_available: true,
              toolset: "deferred",
              capability: "tools.deferred",
              permission: "auto",
              read_only: false,
              short_description: "Call a deferred tool",
            },
            {
              name: "mcp__secret__danger",
              exposure: "hidden",
              direct: false,
              schema_available: false,
              toolset: "mcp",
              capability: "mcp.secret",
              permission: "deny",
              read_only: false,
              short_description: "Hidden dangerous tool",
            },
          ],
          commands: [
            { name: "conversation.list" },
            { name: "mcp.list" },
          ],
          skills: [
            { name: "code-review", description: "Review code" },
            { name: "debugging", description: "Debug failures" },
            { name: "planning", description: "Plan work" },
          ],
          summary: {
            tools_total: 42,
            direct_tools: 12,
            core_tools: 4,
            deferred_tools: 9,
            hidden_tools: 1,
            commands: 2,
            skills: 3,
            mcp_proxy_tools: 2,
            mcp_resource_bridge: true,
            deferred_bridge: true,
            skill_catalog: true,
          },
        },
        workspace: { root: "C:\\Desktop\\MiniCode" },
        git: { branch: "main" },
        preview: { url: "" },
      }),
    })));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders agent capability status from the doctor payload", async () => {
    render(<SidebarRight embedded initialTab="diagnostics" />);

    expect(await screen.findByText("智能体能力")).toBeTruthy();
    expect(await screen.findByText("42 total / 12 direct")).toBeTruthy();
    expect(screen.getByText("Ready (9)")).toBeTruthy();
    expect(screen.getByText("Ready (3)")).toBeTruthy();
    expect(screen.getByText("2 dynamic tools")).toBeTruthy();
    expect(screen.getAllByText("Ready").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Doctor")).toBeTruthy();
    expect(screen.getByText("4 core / 9 deferred / 1 hidden")).toBeTruthy();
    expect(screen.getByText("直接可用")).toBeTruthy();
    expect(screen.getByText("read_file")).toBeTruthy();
    expect(screen.getByText("按需加载")).toBeTruthy();
    expect(screen.getByText("tool_call")).toBeTruthy();
    expect(screen.getByText("未开放")).toBeTruthy();
    expect(screen.getByText("mcp__secret__danger")).toBeTruthy();
    expect(screen.getByText("2 commands")).toBeTruthy();
    expect(screen.getByText("read_file, write_file, list_mcp_resources, tool_search")).toBeTruthy();
    expect(screen.getByText("conversation.list, mcp.list")).toBeTruthy();
    expect(screen.getByText("code-review, debugging, planning")).toBeTruthy();

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "http://api.test/api/doctor",
        expect.objectContaining({
          cache: "no-store",
          headers: { Authorization: "Bearer test" },
        }),
      );
    });
  });

  it("falls back to status capabilities when doctor omits capability details", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/api/status")) {
        return {
          ok: true,
          json: async () => ({
            capabilities: {
              tools: [
                { type: "function", function: { name: "read_file" } },
                { type: "function", function: { name: "list_mcp_resources" } },
              ],
              commands: [
                { name: "conversation.list" },
                { name: "skills.list" },
              ],
              skills: [
                { name: "code-review" },
                { name: "debugging" },
              ],
            },
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({
          backend: { status: "ok", active_sessions: 1 },
          llm: { provider: "openai", active_model: "gpt-5.4" },
          mcp: [],
          workspace: { root: "C:\\Desktop\\MiniCode" },
          git: { branch: "main" },
          preview: { url: "" },
        }),
      };
    }));

    render(<SidebarRight embedded initialTab="diagnostics" />);

    expect(await screen.findByText("2 total")).toBeTruthy();
    expect(screen.getByText("2 skills")).toBeTruthy();
    expect(screen.getByText("read_file, list_mcp_resources")).toBeTruthy();
    expect(screen.getByText("conversation.list, skills.list")).toBeTruthy();
    expect(screen.getByText("code-review, debugging")).toBeTruthy();
    expect(screen.getByText("2 commands")).toBeTruthy();
    expect(screen.getByText("Status fallback")).toBeTruthy();
    expect(fetch).toHaveBeenCalledWith("http://api.test/api/status", expect.objectContaining({ cache: "no-store" }));
  });

  it("refreshes doctor capabilities when MCP status changes while diagnostics is open", async () => {
    let doctorCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      doctorCalls += 1;
      const connected = doctorCalls > 1;
      return {
        ok: true,
        json: async () => ({
          backend: { status: "ok", active_sessions: 1 },
          llm: { provider: "openai", active_model: "gpt-5.4" },
          mcp: connected ? [{ name: "demo", status: "connected" }] : [],
          capabilities: {
            tools: [{ type: "function", function: { name: "read_file" } }],
            tool_views: connected
              ? [
                  { name: "read_file", exposure: "core", direct: true, schema_available: true },
                  { name: "mcp__demo__echo", exposure: "core", direct: true, schema_available: true },
                ]
              : [
                  { name: "read_file", exposure: "core", direct: true, schema_available: true },
                ],
            summary: {
              tools_total: connected ? 2 : 1,
              direct_tools: connected ? 2 : 1,
              core_tools: connected ? 2 : 1,
              deferred_tools: 0,
              hidden_tools: 0,
              mcp_proxy_tools: connected ? 1 : 0,
              mcp_resource_bridge: true,
              deferred_bridge: true,
              skill_catalog: true,
            },
          },
        }),
      };
    }));

    render(<SidebarRight embedded initialTab="diagnostics" />);

    expect(await screen.findByText("1 total / 1 direct")).toBeTruthy();
    expect(screen.getByText("0 dynamic tools")).toBeTruthy();

    useAppStore.getState().setMcpServers([
      { name: "demo", status: "connected", tools: 1, phase: "connected" },
    ]);

    expect(await screen.findByText("2 total / 2 direct")).toBeTruthy();
    expect(screen.getByText("1 dynamic tool")).toBeTruthy();
    expect(screen.getByText((content) => content.includes("mcp__demo__echo"))).toBeTruthy();
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledTimes(2);
    });
  });

  it("does not let automatic preview state steal focus from diagnostics", async () => {
    useAppStore.setState({
      rightStackTab: "diagnostics",
      rightStackTabLocked: false,
      rightPanelOpen: true,
      livePreviewUrl: "http://localhost:5173",
    });

    render(<SidebarRight />);

    expect(await screen.findByText("运行诊断")).toBeTruthy();
    await waitFor(() => {
      expect(useAppStore.getState().rightStackTab).toBe("diagnostics");
    });
  });

  it("locks diagnostics after manual selection so preview updates do not steal focus", async () => {
    useAppStore.setState({
      rightStackTab: "preview",
      rightStackTabLocked: false,
      rightPanelOpen: true,
      livePreviewUrl: "",
      mcpServers: [{ name: "websearch", command: "websearch", status: "error", tools: [] }],
    });

    render(<SidebarRight />);

    act(() => useAppStore.getState().setRightStackTab("diagnostics"));

    expect(useAppStore.getState().rightStackTab).toBe("diagnostics");
    expect(useAppStore.getState().rightStackTabLocked).toBe(true);

    useAppStore.getState().setLivePreviewUrl("http://localhost:5174");

    await waitFor(() => {
      expect(useAppStore.getState().rightStackTab).toBe("diagnostics");
    });
    expect(await screen.findByText("运行诊断")).toBeTruthy();
  });
});
