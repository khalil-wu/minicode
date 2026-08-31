import { beforeEach, describe, expect, it, vi } from "vitest";
import { handleNoticeEvent } from "./noticeEvents";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";
import { handlePeripheralEvent } from "./peripheralEvents";
import { sendClientCommand } from "../protocol/ws-outbox";

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  createClientCommandId: vi.fn(() => "workspace-refresh-request"),
  sendClientCommand: vi.fn(() => true),
}));

const workspaceImportedEvent = (overrides: Record<string, unknown> = {}) => ({
  type: "workspace.imported",
  conversation_id: "conv-active",
  workspace_root: "C:\\Desktop\\RAG",
  request_id: "workspace-request-1",
  project: {
    root_path: "C:\\Desktop\\RAG",
    project_type: "python",
    name: "RAG",
    description: "Retrieval project",
    file_count: 42,
    total_size: 123_456,
    has_project_instructions: false,
    index_truncated: false,
  },
  summary: "Python retrieval project",
  file_count: 42,
  ...overrides,
});

describe("handleNoticeEvent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [],
      messages: [],
      conversationMessages: {},
      contextUsage: null,
      backgroundTasks: [],
      terminalSessions: [],
      terminalSnapshots: {},
      activeTerminalSessionId: null,
      inspectorEntries: [],
      inspectorFocus: null,
    });
  });

  it("keeps ordinary system notices in the conversation without a duplicate toast", () => {
    expect(handleNoticeEvent({
      type: "system_notice",
      content: "Index refreshed",
      conversation_id: "conv-active",
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    const [message] = useAppStore.getState().messages;
    expect(message).toMatchObject({
      role: "system",
      content: "Index refreshed",
    });
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("keeps provider/model status notices out of the chat transcript", () => {
    for (const content of [
      "Provider updated to custom (gpt-5.5)",
      "Model set to gpt-5.5",
      "Provider authentication and a small generation check succeeded.",
      "Missing API key for current provider.",
    ]) {
      expect(handleNoticeEvent({
        type: "system_notice",
        content,
        conversation_id: "conv-active",
      } as unknown as ServerEvent, "conv-active")).toBe(true);
    }

    expect(useAppStore.getState().messages).toEqual([]);
    expect(pushToast).toHaveBeenCalledWith(
      "Provider updated to custom (gpt-5.5)",
      "info",
      3000,
    );
    expect(pushToast).toHaveBeenCalledWith(
      "Provider authentication and a small generation check succeeded.",
      "info",
      3000,
    );
  });

  it("projects titled notices with informative punctuation, stable deduplication and inspector detail", () => {
    const event = {
      type: "system_notice",
      conversation_id: "conv-active",
      title: "Resumed from checkpoint",
      message: "Continuing from iteration 3.",
      data: { iteration: 3, checkpoint_id: "cp-3" },
      event_id: "session-1:notice-3",
    } as unknown as ServerEvent;

    expect(handleNoticeEvent(event, "conv-active")).toBe(true);
    expect(handleNoticeEvent({ ...event, replayed: true } as ServerEvent, "conv-active")).toBe(true);

    const state = useAppStore.getState();
    expect(state.messages).toEqual([
      expect.objectContaining({
        id: "system-notice-session-1:notice-3",
        role: "system",
        content: "Resumed from checkpoint — Continuing from iteration 3.",
      }),
    ]);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "message",
        targetId: "system-notice-session-1:notice-3",
        payload: expect.objectContaining({
          event: "system_notice",
          conversation_id: "conv-active",
          data: { iteration: 3, checkpoint_id: "cp-3" },
          replayed: true,
        }),
      }),
    ]);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("routes inactive ordinary notices to their owner and suppresses inactive or replayed control toasts", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [],
      conversationMessages: { "conv-other": [] },
    });

    expect(handleNoticeEvent({
      type: "system_notice",
      conversation_id: "conv-other",
      content: "Background index refreshed",
      event_id: "notice-other",
    } as unknown as ServerEvent, "conv-other")).toBe(true);
    expect(handleNoticeEvent({
      type: "system_notice",
      conversation_id: "conv-other",
      content: "Provider updated to custom (gpt-5.5)",
    } as unknown as ServerEvent, "conv-other")).toBe(true);
    expect(handleNoticeEvent({
      type: "system_notice",
      conversation_id: "conv-active",
      content: "Model set to gpt-5.5",
      replayed: true,
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(useAppStore.getState().messages).toEqual([]);
    expect(useAppStore.getState().conversationMessages["conv-other"]).toEqual([
      expect.objectContaining({
        id: "system-notice-notice-other",
        content: "Background index refreshed",
      }),
    ]);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("stores compaction metadata without duplicating transcript state", () => {
    useAppStore.setState({
      conversations: [{
        id: "conv-active",
        title: "Release audit",
        updatedAt: "2026-08-15T09:00:00Z",
      }],
      contextUsage: {
        used: 120,
        limit: 1000,
      },
    });

    expect(handleNoticeEvent({
      type: "conversation.compaction.updated",
      conversation_id: "conv-active",
      state: "compacted",
      summary: "Kept project goals and current task.",
      timestamp: "2026-08-15T10:00:00Z",
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(useAppStore.getState().contextUsage).toMatchObject({
      used: 120,
      limit: 1000,
      compactSummary: "Kept project goals and current task.",
      compactedAt: Date.parse("2026-08-15T10:00:00Z"),
    });
    expect(useAppStore.getState().conversations[0]).toMatchObject({
      compactionState: "compacted",
      compactionSummary: "Kept project goals and current task.",
    });
    expect(useAppStore.getState().messages).toEqual([]);
  });

  it("ignores compaction metadata from inactive conversations", () => {
    useAppStore.setState({
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-08-15T09:00:00Z" },
        { id: "conv-other", title: "Other", updatedAt: "2026-08-15T08:00:00Z" },
      ],
      contextUsage: {
        used: 120,
        limit: 1000,
      },
    });

    expect(handleNoticeEvent({
      type: "conversation.compaction.updated",
      conversation_id: "conv-other",
      state: "compacted",
      summary: "Old thread summary.",
    } as unknown as ServerEvent, "conv-other")).toBe(true);

    expect(useAppStore.getState().contextUsage).toEqual({ used: 120, limit: 1000 });
    expect(useAppStore.getState().conversations[1]).toMatchObject({
      compactionState: "compacted",
      compactionSummary: "Old thread summary.",
    });
  });

  it("updates conversation metadata from summary events without a full list refresh", () => {
    useAppStore.setState({
      conversations: [{
        id: "conv-active",
        title: "New chat",
        updatedAt: "2026-05-28T00:00:00.000Z",
      }],
    });

    expect(handleNoticeEvent({
      type: "conversation.summary.updated",
      conversation_id: "conv-active",
      title: "今天北京天气如何",
      updated_at: "2026-05-29T10:00:00.000Z",
      summary: "Asked about Beijing weather.",
      memory_mode: "polluted",
      memory_polluted: true,
      memory_pollution_sources: ["web_search", "mcp__github__search_code"],
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(useAppStore.getState().conversations[0]).toMatchObject({
      id: "conv-active",
      title: "今天北京天气如何",
      updatedAt: "2026-05-29T10:00:00.000Z",
      summary: "Asked about Beijing weather.",
      memoryMode: "polluted",
      memoryPolluted: true,
      memoryPollutionSources: ["web_search", "mcp__github__search_code"],
    });
    expect(useAppStore.getState().messages).toEqual([]);
  });

  it("does not consume transport notices owned by the websocket layer", () => {
    // client.command.ack and pong are handled by useWebSocket directly
    // (acknowledgeClientCommand / heartbeat), not by the notice projector.
    expect(handleNoticeEvent({ type: "client.command.ack", client_command_id: "cmd-1" } as ServerEvent)).toBe(false);
    expect(handleNoticeEvent({ type: "pong" } as ServerEvent)).toBe(false);

    expect(useAppStore.getState().messages).toEqual([]);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("leaves information-bearing control-plane events to their dedicated projector", () => {
    expect(handleNoticeEvent({
      type: "checkpoint.list",
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
      checkpoints: [],
    } as unknown as ServerEvent)).toBe(false);
    expect(handleNoticeEvent({
      type: "workspace.recent.list",
      projects: [],
    } as ServerEvent)).toBe(false);
    expect(handleNoticeEvent({
      type: "guidelines.updated",
      message: "Project guidelines have been updated",
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
    } as ServerEvent)).toBe(false);
  });

  it("keeps the active conversation workspace in sync when workspace opens", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [{
        id: "conv-active",
        title: "Active",
        updatedAt: "2026-05-28T00:00:00.000Z",
        workspaceRoot: "C:\\Desktop\\MiniCode",
      }],
      workingDirectory: "C:\\Desktop\\MiniCode",
    });

    expect(handlePeripheralEvent({
      ...workspaceImportedEvent(),
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.workingDirectory).toBe("C:\\Desktop\\RAG");
    expect(state.conversations[0].workspaceRoot).toBe("C:\\Desktop\\RAG");
    expect(sendClientCommand).toHaveBeenCalledTimes(4);
    expect(vi.mocked(sendClientCommand).mock.calls.map(([command]) => command.type)).toEqual([
      "diff.git_working_tree",
      "diff.git_staged",
      "git.pr_status",
      "scheduler.list",
    ]);
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "git.pr_status",
      conversation_id: "conv-active",
      workspace_root: "C:\\Desktop\\RAG",
    });
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "scheduler.list",
      conversation_id: "conv-active",
      workspace_root: "C:\\Desktop\\RAG",
    });
    expect(pushToast).toHaveBeenCalledOnce();
    expect(pushToast).toHaveBeenCalledWith(
      "Opened workspace: RAG · python · 42 files",
      "success",
      3500,
    );
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "workspace",
        payload: expect.objectContaining({
          conversation_id: "conv-active",
          workspace_root: "C:\\Desktop\\RAG",
          project_name: "RAG",
          project_type: "python",
          file_count: 42,
          total_size: 123_456,
          index_truncated: false,
          summary: "Python retrieval project",
          replayed: false,
        }),
      }),
    ]);
  });

  it("does not let an inactive workspace import contaminate the active workspace", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-08-15T00:00:00Z", workspaceRoot: "C:\\active" },
        { id: "conv-other", title: "Other", updatedAt: "2026-08-15T00:00:00Z", workspaceRoot: "C:\\old" },
      ],
      workingDirectory: "C:\\active",
    });

    expect(handlePeripheralEvent(workspaceImportedEvent({
      conversation_id: "conv-other",
      workspace_root: "C:\\other",
      project: {
        ...workspaceImportedEvent().project,
        root_path: "C:\\other",
        name: "Other",
      },
    }) as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.workingDirectory).toBe("C:\\active");
    expect(state.conversations.find((conversation) => conversation.id === "conv-active")?.workspaceRoot).toBe("C:\\active");
    expect(state.conversations.find((conversation) => conversation.id === "conv-other")?.workspaceRoot).toBe("C:\\other");
    expect(state.inspectorEntries).toEqual([]);
    expect(sendClientCommand).not.toHaveBeenCalled();
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("hydrates a replayed active workspace without issuing fresh side effects", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [{ id: "conv-active", title: "Active", updatedAt: "2026-08-15T00:00:00Z" }],
      workingDirectory: "",
    });

    expect(handlePeripheralEvent({
      ...workspaceImportedEvent({
        project: {
          ...workspaceImportedEvent().project,
          index_truncated: true,
        },
      }),
      replayed: true,
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.workingDirectory).toBe("C:\\Desktop\\RAG");
    expect(state.conversations[0].workspaceRoot).toBe("C:\\Desktop\\RAG");
    expect(state.inspectorEntries[0]?.payload).toMatchObject({ replayed: true, index_truncated: true });
    expect(sendClientCommand).not.toHaveBeenCalled();
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("stores terminal snapshots for agent/runtime context", () => {
    useAppStore.setState({
      terminalSessions: [{ id: "term_1", conversationId: "conv-active", shell: "pwsh", cwd: "C:/repo", status: "running" }],
      terminalSnapshots: {},
    });

    expect(handlePeripheralEvent({
      type: "terminal.snapshot",
      conversation_id: "conv-active",
      session_id: "term_1",
      cwd: "C:/repo",
      shell: "pwsh",
      is_alive: true,
      output: "npm run dev\nready\n",
      truncated: false,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().terminalSnapshots["term_1"]).toMatchObject({
      id: "term_1",
      cwd: "C:/repo",
      output: "npm run dev\nready\n",
      truncated: false,
    });
  });

  it("converts RFC3339 file-change timestamps and requests a git refresh", () => {
    vi.useFakeTimers();
    const originalRequestGitChanges = useAppStore.getState().requestGitChanges;
    const requestGitChanges = vi.fn();
    useAppStore.setState({
      conversationId: "conv-active",
      workingDirectory: "C:\\repo",
      fileChanges: [],
      requestGitChanges,
    });

    try {
      expect(handlePeripheralEvent({
        type: "file.changed",
        conversation_id: "conv-active",
        workspace_root: "C:\\repo",
        path: "src/app.ts",
        event: "modified",
        timestamp: "2026-08-09T08:00:00Z",
      } as unknown as ServerEvent)).toBe(true);

      expect(useAppStore.getState().fileChanges).toEqual([{
        path: "src/app.ts",
        event: "modified",
        timestamp: Date.parse("2026-08-09T08:00:00Z"),
      }]);
      // Bursts of file.changed collapse into one trailing git refresh.
      expect(requestGitChanges).not.toHaveBeenCalled();
      vi.advanceTimersByTime(400);
      expect(requestGitChanges).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
      useAppStore.setState({ requestGitChanges: originalRequestGitChanges });
    }
  });

  it("does not let replayed file-change events mark an open image tab as externally changed", () => {
    const requestGitChanges = vi.fn();
    useAppStore.setState({
      conversationId: "conv-active",
      workingDirectory: "C:\\repo",
      editorTabs: [{
        path: "assets/screenshot.png",
        content: "",
        original: "",
        loading: false,
        error: null,
        externalChanged: false,
      }],
      activeTabPath: "assets/screenshot.png",
      fileChanges: [],
      requestGitChanges,
    });

    expect(handlePeripheralEvent({
      type: "file.changed",
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
      path: "assets/screenshot.png",
      event: "modified",
      timestamp: "2026-08-09T08:00:00Z",
      replayed: true,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().fileChanges).toEqual([]);
    expect(useAppStore.getState().editorTabs[0]?.externalChanged).toBe(false);
    expect(requestGitChanges).not.toHaveBeenCalled();
  });

  it("projects negotiated MCP capabilities into connector state", () => {
    expect(handlePeripheralEvent({
      type: "mcp_status",
      servers: [{
        name: "docs",
        status: "connected",
        tools_count: 2,
        capabilities: {
          tools: true,
          resources: true,
          resources_subscribe: false,
          resources_list_changed: true,
          prompts: true,
          logging: false,
        },
      }],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().mcpServers[0]).toMatchObject({
      name: "docs",
      tools: 2,
      capabilities: { resources: true, prompts: true },
    });
  });

  it("keeps legacy MCP control-plane notices out of the chat transcript", () => {
    for (const content of [
      "Connector configuration was saved. The current Agent turn keeps its existing tool schema; the connector will be available on the next turn.",
      "Connector 'github' installed and ready",
      "Connector 'figma' was saved, but is not ready: offline",
    ]) {
      expect(handleNoticeEvent({
        type: "system_notice",
        content,
        conversation_id: "conv-active",
      } as unknown as ServerEvent, "conv-active")).toBe(true);
    }

    expect(useAppStore.getState().messages).toEqual([]);
    expect(pushToast).toHaveBeenCalledTimes(3);
  });

  it("tracks agent-owned background commands from start through cancellation", () => {
    expect(handlePeripheralEvent({
      type: "background.started",
      command_id: "bg-owned",
      command: "npm run dev",
      cwd: "C:/repo",
      status: "running",
      started_at: 10,
      conversation_id: "conv-active",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().backgroundTasks[0]).toMatchObject({
      id: "bg-owned",
      command: "npm run dev",
      status: "running",
      timestamp: 10_000,
      conversationId: "conv-active",
      cwd: "C:/repo",
    });

    expect(handlePeripheralEvent({
      type: "background.completed",
      command_id: "bg-owned",
      command: "npm run dev",
      status: "cancelled",
      started_at: 10,
      completed_at: 12,
      conversation_id: "conv-active",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().backgroundTasks[0]).toMatchObject({
      id: "bg-owned",
      status: "cancelled",
      timestamp: 10_000,
      completedAt: 12_000,
      conversationId: "conv-active",
    });
  });

  it("projects a recovered interrupted command as failed instead of successful", () => {
    expect(handlePeripheralEvent({
      type: "background.completed",
      command_id: "bg-recovered",
      command: "npm run dev",
      status: "interrupted",
      started_at: 10,
      completed_at: 12,
      conversation_id: "conv-active",
      cleanup_reason: "background_owner_exited",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().backgroundTasks[0]).toMatchObject({
      id: "bg-recovered",
      status: "failed",
      timestamp: 10_000,
      completedAt: 12_000,
      conversationId: "conv-active",
    });
    expect(pushToast).toHaveBeenCalledWith(
      "npm run dev 因上次 MiniCode 进程退出而中断",
      "error",
    );
  });

  it("projects a stalled background command as actionable owner-scoped state without replay noise", () => {
    expect(handlePeripheralEvent({
      type: "background.started",
      command_id: "bg-stalled",
      command: "npm create vite",
      cwd: "C:/repo",
      status: "running",
      started_at: 10,
      conversation_id: "conv-active",
    } as unknown as ServerEvent)).toBe(true);

    expect(handlePeripheralEvent({
      type: "background.stalled",
      command_id: "bg-stalled",
      command: "npm create vite",
      conversation_id: "conv-active",
      tail: "Overwrite existing files? [y/N]",
      advice: "Re-run with piped input or a non-interactive flag.",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().backgroundTasks[0]).toMatchObject({
      id: "bg-stalled",
      command: "npm create vite",
      status: "stalled",
      timestamp: 10_000,
      conversationId: "conv-active",
      stalledTail: "Overwrite existing files? [y/N]",
      stalledAdvice: "Re-run with piped input or a non-interactive flag.",
    });
    expect(pushToast).toHaveBeenCalledWith(
      "后台命令等待输入：npm create vite · Overwrite existing files? [y/N]",
      "warning",
      7000,
    );

    vi.mocked(pushToast).mockClear();
    expect(handlePeripheralEvent({
      type: "background.completed",
      command_id: "bg-stalled",
      command: "npm create vite",
      status: "failed",
      exit_code: 1,
      output: "Cancelled by prompt timeout",
      completed_at: 12,
      conversation_id: "conv-active",
      replayed: true,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().backgroundTasks[0]).toMatchObject({
      id: "bg-stalled",
      status: "failed",
      outputPreview: "Cancelled by prompt timeout",
      completedAt: 12_000,
    });
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("does not guess an owner for background command projection", () => {
    expect(handlePeripheralEvent({
      type: "background.started",
      command_id: "bg-missing-owner",
      command: "npm run dev",
      status: "running",
    } as unknown as ServerEvent)).toBe(true);
    expect(handlePeripheralEvent({
      type: "background.completed",
      command_id: "bg-missing-owner",
      command: "npm run dev",
      status: "completed",
      exit_code: 0,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().backgroundTasks).toEqual([]);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("does not project terminal resources owned by another or missing conversation", () => {
    useAppStore.setState({ terminalSessions: [], terminalSnapshots: {} });

    expect(handlePeripheralEvent({
      type: "terminal.created",
      conversation_id: "conv-other",
      session_id: "term_other",
      shell: "pwsh",
      cwd: "C:/other",
    } as unknown as ServerEvent)).toBe(true);
    expect(handlePeripheralEvent({
      type: "terminal.snapshot",
      session_id: "term_missing_owner",
      output: "should not project",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().terminalSessions).toEqual([]);
    expect(useAppStore.getState().terminalSnapshots).toEqual({});
  });

  it("ignores delayed terminal lists from the previous conversation", () => {
    useAppStore.setState({
      conversationId: "conv-next",
      terminalSessions: [{
        id: "term-next",
        conversationId: "conv-next",
        shell: "pwsh",
        cwd: "C:/next",
        status: "running",
      }],
      activeTerminalSessionId: "term-next",
    });

    expect(handlePeripheralEvent({
      type: "terminal.list",
      conversation_id: "conv-old",
      sessions: [{
        session_id: "term-old",
        conversation_id: "conv-old",
        shell: "pwsh",
        cwd: "C:/old",
        is_alive: true,
      }],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().terminalSessions.map((session) => session.id)).toEqual(["term-next"]);
    expect(useAppStore.getState().activeTerminalSessionId).toBe("term-next");
  });

  it("keeps a multi-terminal preferred id when the authoritative list still contains it", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      activeTerminalSessionId: "term-second",
      terminalSessions: [],
      conversationWorkbenchStates: {
        "conv-active": {
          diffReview: null,
          previewArtifact: null,
          livePreviewUrl: null,
          terminalSessions: [],
          activeTerminalSessionId: "term-second",
          rightStackTab: "tasks",
          rightPanelOpen: false,
          rightStackTabLocked: false,
          draft: "",
          attachments: [],
          quotedMessage: null,
          selectedMentions: [],
          selectedSkills: [],
          allowedRemoteImageDomains: [],
        },
      },
    });

    expect(handlePeripheralEvent({
      type: "terminal.list",
      conversation_id: "conv-active",
      sessions: [
        { session_id: "term-first", conversation_id: "conv-active", shell: "pwsh", cwd: "C:/repo", is_alive: true },
        { session_id: "term-second", conversation_id: "conv-active", shell: "pwsh", cwd: "C:/repo", is_alive: true },
      ],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().terminalSessions.map((session) => session.id)).toEqual(["term-first", "term-second"]);
    expect(useAppStore.getState().activeTerminalSessionId).toBe("term-second");
  });

  it("keeps MCP local-app setup metadata from status events", () => {
    useAppStore.setState({ mcpServers: [] });

    expect(handlePeripheralEvent({
      type: "mcp_status",
      servers: [{
        name: "figma-desktop",
        status: "offline",
        transport: "http",
        auth_status: "not_logged_in",
        requires_user_action: true,
        setup_hint: "Open Figma Desktop and enable the Dev Mode MCP server.",
        docs_url: "https://help.figma.com/",
      }],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().mcpServers[0]).toMatchObject({
      name: "figma-desktop",
      transport: "http",
      authStatus: "not_logged_in",
      requiresUserAction: true,
      setupHint: "Open Figma Desktop and enable the Dev Mode MCP server.",
      docsUrl: "https://help.figma.com/",
    });
  });

  it("keeps routine MCP connections quiet while surfacing failures", () => {
    useAppStore.setState({ mcpServers: [] });

    expect(handlePeripheralEvent({
      type: "mcp_status",
      servers: [{ name: "docs", status: "connected" }],
    } as unknown as ServerEvent)).toBe(true);
    expect(pushToast).not.toHaveBeenCalled();

    expect(handlePeripheralEvent({
      type: "mcp_status",
      servers: [{ name: "docs", status: "error" }],
    } as unknown as ServerEvent)).toBe(true);
    expect(pushToast).toHaveBeenCalledWith("MCP：docs 出错", "error");
  });
});
