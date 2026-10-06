import { describe, expect, it, beforeEach, vi } from "vitest";
import { useAppStore } from "./index";
import { sendClientCommand, sendClientCommandAwaitResult, sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { handleSessionEvent } from "../chat/sessionEvents";

vi.mock("../protocol/ws-outbox", () => ({
  createClientCommandId: vi.fn(() => "test-client-command-id"),
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(),
  commandResultSucceeded: (event: { level?: string }) => !["error", "failed"].includes(String(event.level || "")),
  sendConversationDeleteCommand: vi.fn().mockResolvedValue(true),
}));

const applyConversationList = (
  conversations: Array<{ id: string; title: string; updatedAt: string }>,
  activeConversationId?: string,
) => handleSessionEvent({
  type: "conversation.list",
  conversations: conversations.map((conversation) => ({
    id: conversation.id,
    title: conversation.title,
    updated_at: conversation.updatedAt,
  })),
  active_conversation_id: activeConversationId,
} as never, {
  textStreamBuffer: { destroy: vi.fn() },
  thinkingStreamBuffer: { destroy: vi.fn() },
} as never);

const resetChatState = () => {
  useAppStore.setState({
    conversationId: null,
    pendingConversationSwitchId: null,
    pendingConversationCreateId: null,
    conversations: [],
    messages: [],
    conversationMessages: {},
    conversationStreaming: {},
    conversationRecallTruncations: {},
    conversationAgentStates: {},
    conversationWorkbenchStates: {},
    draft: "",
    attachments: [],
    quotedMessage: null,
    selectedMentions: [],
    selectedSkills: [],
    allowedRemoteImageDomains: [],
    remoteImagePolicy: "ask",
    plan: null,
    todos: [],
    subagents: [],
    agentProgress: [],
    diffReview: null,
    pendingApproval: null,
    approvalQueue: [],
    pendingDiffReview: null,
    diffReviewQueue: [],
    pendingAskUser: null,
    askUserQueue: [],
    previewArtifact: null,
    livePreviewUrl: null,
    activeTerminalSessionId: null,
    isStreaming: false,
    workingDirectory: "",
    workspaceGit: null,
    editorTabs: [],
    activeTabPath: null,
    activeEditorPath: null,
    appMode: "code",
    panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
  });
};

describe("conversation deletion store behavior", () => {
  beforeEach(() => {
    resetChatState();
    vi.mocked(sendClientCommand).mockClear();
    vi.mocked(sendClientCommandAwaitResult).mockReset();
    vi.mocked(sendClientCommandAwaitResult).mockImplementation(async (_command, expectedCommand) => ({
      type: "command.result",
      command: expectedCommand,
      level: "success",
      message: "",
      data: {},
    }));
    vi.mocked(sendConversationDeleteCommand).mockClear();
  });

  it("accumulates provider usage totals for the active conversation", () => {
    useAppStore.setState({
      lastUsage: null,
      usageTotals: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, turns: 0 },
    });

    useAppStore.getState().setLastUsage({ input: 80, output: 10, cacheRead: 20, cacheWrite: 5, reasoning: 3 });
    useAppStore.getState().setLastUsage({ input: 120, output: 15, cacheRead: 30, cacheWrite: 0, reasoning: 4 });

    expect(useAppStore.getState().usageTotals).toEqual({
      input: 200,
      ordinaryInput: 145,
      output: 25,
      cacheRead: 50,
      cacheWrite: 5,
      promptCacheTotal: 200,
      reasoning: 7,
      turns: 2,
    });
  });

  it("waits for the canonical list before switching after deletion", async () => {
    const oldMessage = {
      id: "m-old",
      role: "user" as const,
      content: "old",
      artifacts: [],
      timestamp: Date.now(),
    };
    const nextMessage = {
      id: "m-next",
      role: "assistant" as const,
      content: "next transcript",
      artifacts: [],
      timestamp: Date.now(),
    };

    useAppStore.setState({
      conversationId: "conv-old",
      conversations: [
        { id: "conv-old", title: "Old", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      messages: [oldMessage],
      conversationMessages: {
        "conv-old": [oldMessage],
        "conv-next": [nextMessage],
      },
      conversationStreaming: {
        "conv-old": false,
        "conv-next": false,
      },
    });

    await useAppStore.getState().removeConversation("conv-old");

    expect(useAppStore.getState().conversationId).toBe("conv-old");
    applyConversationList([
      { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
    ], "conv-next");

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-next");
    expect(state.messages).toEqual([nextMessage]);
    expect(state.isStreaming).toBe(false);
    expect(state.conversationMessages["conv-old"]).toBeUndefined();
    expect(state.conversations.map((conversation) => conversation.id)).toEqual(["conv-next"]);
    expect(state.conversationMessages["conv-next"]).toEqual([nextMessage]);
  });

  it("keeps the current workspace and editor visible after the canonical final deletion", async () => {
    useAppStore.setState({
      conversationId: "conv-only",
      conversations: [{ id: "conv-only", title: "Only", updatedAt: "2026-05-24T00:00:00.000Z" }],
      messages: [],
      conversationMessages: { "conv-only": [] },
      conversationStreaming: { "conv-only": false },
      workingDirectory: "C:\\Desktop\\MiniCode",
      editorTabs: [{ id: "editor-fixture-1", path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
      activeEditorPath: "README.md",
      appMode: "cowork",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false },
        { id: "editor-readme", kind: "editor", label: "README.md", focused: true },
      ],
    });

    await useAppStore.getState().removeConversation("conv-only");
    applyConversationList([]);

    const state = useAppStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.conversations).toEqual([]);
    expect(state.messages).toEqual([]);
    expect(state.workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(state.editorTabs).toEqual([{ id: "editor-fixture-1", path: "README.md", content: "", original: "", loading: false, error: null }]);
    expect(state.activeTabPath).toBe("README.md");
    expect(state.activeEditorPath).toBe("README.md");
    expect(state.appMode).toBe("code");
    expect(state.panelSlots.find((slot) => slot.kind === "editor")?.focused).toBe(true);
    expect(sendClientCommand).not.toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.create" }));
    expect(state.conversationMessages["conv-only"]).toBeUndefined();
  });

  it("binds the current workspace when an explicit Code workflow creates a conversation", async () => {
    useAppStore.setState({
      workingDirectory: "C:\\Desktop\\MiniCode",
      workspaceGit: { branch: "main", isWorktree: false, currentPath: "C:\\Desktop\\MiniCode" },
    });

    await useAppStore.getState().createConversation({ appMode: "code" });

    const command = vi.mocked(sendClientCommandAwaitResult).mock.calls[0]?.[0] as { workspace_root?: string; type?: string };
    const state = useAppStore.getState();
    expect(command).toMatchObject({ type: "conversation.create" });
    expect(command.workspace_root).toBe("C:\\Desktop\\MiniCode");
    expect(state.workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(state.conversations).toEqual([]);
    expect(state.appMode).toBe("code");
  });

  it("opens a default new conversation in chat while retaining the canonical owner, editor and draft", async () => {
    const editorTabs = [{ id: "kept-editor", path: "README.md", content: "unsaved code", original: "", loading: false, error: null }];
    const panelSlots = [{ id: "main-chat", kind: "chat" as const, label: "Chat", focused: false }, { id: "main-editor", kind: "editor" as const, label: "File", focused: true }];
    useAppStore.setState({
      appMode: "code", conversationId: "conv-active", workingDirectory: "C:\\Desktop\\MiniCode",
      conversations: [{ id: "conv-active", title: "Active", updatedAt: "2026-10-05" }],
      draft: "unfinished prompt", editorTabs, panelSlots, activeTabPath: "README.md", activeEditorPath: "README.md",
    });
    await useAppStore.getState().createConversation();
    const command = vi.mocked(sendClientCommandAwaitResult).mock.calls[0]?.[0] as { workspace_root?: string };
    const state = useAppStore.getState();
    expect(command.workspace_root).toBe("C:\\Desktop\\MiniCode");
    expect(state).toMatchObject({ appMode: "cowork", conversationId: "conv-active", draft: "unfinished prompt", activeTabPath: "README.md", activeEditorPath: "README.md" });
    expect(state.editorTabs).toBe(editorTabs);
    expect(state.panelSlots).toBe(panelSlots);
    expect(state.conversations).toHaveLength(1);
  });

  it.each([
    { activeId: undefined, confirmed: false }, { activeId: "conv-old", confirmed: false },
    { activeId: undefined, confirmed: true }, { activeId: "conv-old", confirmed: true },
  ])("keeps a first create ahead of a delayed startup catalog with $activeId, confirmed=$confirmed", async ({ activeId, confirmed }) => {
    let completeCreate!: () => void;
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce((_command, expectedCommand) => new Promise((resolve) => {
      completeCreate = () => resolve({ type: "command.result", command: expectedCommand, level: "success", message: "", data: {} });
    }));
    const editorTabs = [{ id: "dirty", path: "wu.cpp", content: "unsaved", original: "", loading: false, error: null }];
    useAppStore.setState({ draft: "new task draft", editorTabs, currentModel: "selected-model", effortLevel: "high" });
    const creation = useAppStore.getState().createConversation({ bindWorkspace: false, appMode: "cowork" });
    const createdId = useAppStore.getState().pendingConversationSwitchId!;
    expect(createdId).toBeTruthy();
    expect(vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0]).toMatchObject({
      type: "conversation.create", conversation_id: createdId,
    });
    if (confirmed) handleSessionEvent({ type: "conversation.switched", conversation_id: createdId,
      conversation: { id: createdId, title: "New chat", updated_at: "2026-10-06", messages: [] },
    } as never, { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never);

    handleSessionEvent({ type: "conversation.list", active_conversation_id: activeId,
      conversations: [{ id: "conv-old", title: "Old chat", updated_at: "2026-10-06" }],
      ...(activeId ? { active_conversation: { id: activeId, title: "Old chat", updated_at: "2026-10-06", messages: [] } } : {}),
      session: { active_conversation_id: activeId, selected_model: "startup-model" },
    } as never, { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never);
    expect(useAppStore.getState()).toMatchObject({ conversationId: confirmed ? createdId : null,
      pendingConversationSwitchId: confirmed ? null : createdId, pendingConversationCreateId: createdId,
      draft: confirmed ? "" : "new task draft", currentModel: "selected-model", effortLevel: "high" });
    expect(useAppStore.getState().editorTabs).toBe(editorTabs);
    expect(useAppStore.getState().conversations.map((conversation) => conversation.id)).toEqual(["conv-old"]);
    expect(sendClientCommand).not.toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.switch" }));

    handleSessionEvent({ type: "conversation.switched", conversation_id: createdId,
      conversation: { id: createdId, title: "New chat", updated_at: "2026-10-06", messages: [] },
    } as never, { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never);
    completeCreate();
    expect(await creation).toBe(true);
    expect(useAppStore.getState()).toMatchObject({ conversationId: createdId, pendingConversationSwitchId: null,
      pendingConversationCreateId: null, appMode: "cowork" });
    expect(sendClientCommand).not.toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.switch" }));
  });

  it("keeps newer navigation ahead of delayed startup restore and older switch pages", () => {
    const messages = [{ id: "old-answer", role: "assistant" as const, content: "Current answer", timestamp: 1, artifacts: [] }];
    useAppStore.setState({ conversationId: "conv-current", conversations: [{ id: "conv-current", title: "Current", updatedAt: "2026-10-06" }],
      messages, draft: "keep draft", currentModel: "current-model", effortLevel: "high" });
    useAppStore.getState().requestConversationSwitch("conv-target");
    const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never;
    for (const type of ["session.restored", "session.synced"] as const) {
      handleSessionEvent({ type, active_conversation_id: "conv-old", current_model: "startup-model",
        session: { active_conversation_id: "conv-old", selected_model: "startup-model", active_stream_conversation_ids: [] },
        active_conversation: { id: "conv-old", title: "Old", updated_at: "2026-10-06", messages: [] },
      } as never, buffers);
    }
    handleSessionEvent({ type: "conversation.switched", conversation_id: "conv-old",
      conversation: { id: "conv-old", title: "Old", updated_at: "2026-10-06", messages: [] },
    } as never, buffers);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "conv-current", pendingConversationSwitchId: "conv-target",
      draft: "keep draft", currentModel: "current-model", effortLevel: "high" });
    expect(useAppStore.getState().messages).toBe(messages);
    handleSessionEvent({ type: "conversation.switched", conversation_id: "conv-target",
      conversation: { id: "conv-target", title: "Target", updated_at: "2026-10-06", messages: [] },
    } as never, buffers);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "conv-target", pendingConversationSwitchId: null });
  });

  it.each(["error", "throw"] as const)("releases only its own failed create intent for %s", async (failure) => {
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(async (_command, expectedCommand) => {
      if (failure === "throw") throw new Error("Create failed");
      return { type: "command.result", command: expectedCommand, level: "error", message: "Create failed", data: {} };
    });
    useAppStore.setState({ conversationId: "conv-current", draft: "keep draft" });
    expect(await useAppStore.getState().createConversation({ bindWorkspace: false })).toBe(false);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "conv-current", pendingConversationSwitchId: null, draft: "keep draft" });
  });

  it("does not let an older create failure clear a newer requested owner", async () => {
    let completeCreate!: () => void;
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce((_command, expectedCommand) => new Promise((resolve) => {
      completeCreate = () => resolve({ type: "command.result", command: expectedCommand, level: "error", message: "Old create failed", data: {} });
    }));
    const creation = useAppStore.getState().createConversation({ bindWorkspace: false });
    useAppStore.getState().requestConversationSwitch("conv-later");
    completeCreate();
    expect(await creation).toBe(false);
    expect(useAppStore.getState().pendingConversationSwitchId).toBe("conv-later");
  });

  it("does not let an older successful create overwrite the mode of a newer confirmed navigation", async () => {
    let completeCreate!: () => void;
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce((_command, expectedCommand) => new Promise((resolve) => {
      completeCreate = () => resolve({ type: "command.result", command: expectedCommand, level: "success", message: "", data: {} });
    }));
    const creation = useAppStore.getState().createConversation({ bindWorkspace: false, appMode: "code" });
    useAppStore.getState().requestConversationSwitch("conv-later");
    handleSessionEvent({ type: "conversation.switched", conversation_id: "conv-later",
      conversation: { id: "conv-later", title: "Later", updated_at: "2026-10-06", messages: [] },
    } as never, { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never);
    completeCreate();
    expect(await creation).toBe(true);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "conv-later", pendingConversationSwitchId: null, appMode: "cowork" });
  });

  it("preserves an explicit request for a global conversation", async () => {
    useAppStore.setState({
      appMode: "code",
      workingDirectory: "C:\\Desktop\\MiniCode",
    });

    await useAppStore.getState().createConversation({ bindWorkspace: false });

    const command = vi.mocked(sendClientCommandAwaitResult).mock.calls[0]?.[0] as { workspace_root?: string };
    expect(command.workspace_root).toBeUndefined();
    expect(useAppStore.getState().appMode).toBe("cowork");
  });

  it("includes workspace scope in the canonical create request", async () => {
    useAppStore.setState({ workingDirectory: "C:\\Desktop\\MiniCode" });

    await useAppStore.getState().createConversation({ bindWorkspace: true });

    const command = vi.mocked(sendClientCommandAwaitResult).mock.calls[0]?.[0] as { workspace_root?: string; type?: string };
    const state = useAppStore.getState();
    expect(command).toMatchObject({ type: "conversation.create", workspace_root: "C:\\Desktop\\MiniCode" });
    expect(state.workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(state.conversations).toEqual([]);
  });

  it("can request a workspace-bound Code conversation without changing modes", async () => {
    useAppStore.setState({
      appMode: "code",
      workingDirectory: "C:\\Desktop\\MiniCode",
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
    });

    await useAppStore.getState().createConversation({ appMode: "code", bindWorkspace: true });

    const command = vi.mocked(sendClientCommandAwaitResult).mock.calls[0]?.[0] as { workspace_root?: string; type?: string };
    const state = useAppStore.getState();
    expect(command).toMatchObject({ type: "conversation.create", workspace_root: "C:\\Desktop\\MiniCode" });
    expect(state.appMode).toBe("code");
    expect(state.workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(state.conversations).toEqual([]);
    expect(state.panelSlots.some((slot) => slot.kind === "chat")).toBe(true);
  });

  it("waits for backend confirmation before applying a conversation switch", () => {
    const activeMessage = {
      id: "m-active",
      role: "assistant" as const,
      content: "active",
      artifacts: [],
      timestamp: Date.now(),
    };
    const nextMessage = {
      id: "m-next",
      role: "assistant" as const,
      content: "next",
      artifacts: [],
      timestamp: Date.now(),
    };

    useAppStore.setState({
      appMode: "code",
      draft: "unsubmitted prompt",
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      messages: [activeMessage],
      conversationMessages: {
        "conv-active": [activeMessage],
        "conv-next": [nextMessage],
      },
      conversationStreaming: {
        "conv-active": false,
        "conv-next": false,
      },
    });

    useAppStore.getState().requestConversationSwitch("conv-next");

    const state = useAppStore.getState();
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "conversation.switch", conversation_id: "conv-next" });
    expect(state.conversationId).toBe("conv-active");
    expect(state.pendingConversationSwitchId).toBe("conv-next");
    expect(state.appMode).toBe("cowork");
    expect(state.draft).toBe("unsubmitted prompt");
    expect(state.messages).toEqual([activeMessage]);

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-next" });
    expect(useAppStore.getState().messages).toEqual([nextMessage]);
    expect(useAppStore.getState().pendingConversationSwitchId).toBeNull();
  });

  it("returns to chat when reselecting the canonical active conversation without requesting a switch", () => {
    const editorTabs = [{ id: "kept-editor", path: "README.md", content: "unsaved code", original: "", loading: false, error: null }];
    const panelSlots = [{ id: "main-chat", kind: "chat" as const, label: "Chat", focused: false }, { id: "main-editor", kind: "editor" as const, label: "File", focused: true }];
    useAppStore.setState({
      appMode: "code", conversationId: "conv-active", draft: "unfinished prompt", editorTabs, panelSlots,
      conversations: [{ id: "conv-active", title: "Active", updatedAt: "2026-10-05" }],
    });
    useAppStore.getState().requestConversationSwitch("conv-active");
    const state = useAppStore.getState();
    expect(state).toMatchObject({ appMode: "cowork", conversationId: "conv-active", pendingConversationSwitchId: null, draft: "unfinished prompt" });
    expect(state.editorTabs).toBe(editorTabs);
    expect(state.panelSlots).toBe(panelSlots);
    expect(sendClientCommand).not.toHaveBeenCalled();
  });

  it("keeps the latest visible switch pending across an earlier switch response", () => {
    useAppStore.setState({
      conversationId: "conv-a",
      conversations: ["conv-a", "conv-b", "conv-c"].map((id) => ({ id, title: id, updatedAt: "2026-05-24T00:00:00.000Z" })),
    });

    useAppStore.getState().requestConversationSwitch("conv-b");
    useAppStore.getState().requestConversationSwitch("conv-c");
    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-b" });
    expect(useAppStore.getState().pendingConversationSwitchId).toBe("conv-c");

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-c" });
    expect(useAppStore.getState().pendingConversationSwitchId).toBeNull();
  });

  it("applies backend-confirmed conversation switches from cached messages", () => {
    const activeMessage = {
      id: "m-active",
      role: "assistant" as const,
      content: "active",
      artifacts: [],
      timestamp: Date.now(),
    };
    const nextMessage = {
      id: "m-next",
      role: "assistant" as const,
      content: "next",
      artifacts: [],
      timestamp: Date.now(),
    };

    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      messages: [activeMessage],
      conversationMessages: {
        "conv-active": [activeMessage],
        "conv-next": [nextMessage],
      },
      conversationStreaming: {
        "conv-active": false,
        "conv-next": false,
      },
    });

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-next" });

    const state = useAppStore.getState();
    expect(sendClientCommand).not.toHaveBeenCalled();
    expect(state.conversationId).toBe("conv-next");
    expect(state.messages).toEqual([nextMessage]);
  });

  it("preserves authoritative usage when the same conversation is rehydrated", () => {
    const lastUsage = { input: 80, output: 10, cacheRead: 20, cacheWrite: 5, reasoning: 3 };
    const usageTotals = { input: 200, output: 25, cacheRead: 50, cacheWrite: 5, reasoning: 7, turns: 2 };
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [{ id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" }],
      contextUsage: { used: 0, limit: 128_000 },
      budgetBuckets: [{ name: "turn", used: 40, limit: 100 }],
      totalBudgetPercent: 0.4,
      lastUsage,
      usageTotals,
    });

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-active" });

    expect(useAppStore.getState()).toMatchObject({
      contextUsage: { used: 0, limit: 128_000 },
      budgetBuckets: [{ name: "turn", used: 40, limit: 100 }],
      totalBudgetPercent: 0.4,
      lastUsage,
      usageTotals,
    });
  });

  it("keeps user-chosen browser view and draft when startup acknowledgement arrives before the catalogue", () => {
    useAppStore.setState({ conversationId: "conv-startup", conversations: [], workingDirectory: "C:/project",
      conversationWorkbenchStates: {}, draft: "typed while reconnecting", selectedMentions: [{ kind: "file", path: "src/main.ts", name: "main.ts" }],
      rightPanelOpen: false, rightPanelExpanded: false, rightStackTabLocked: false,
    });
    useAppStore.getState().setRightStackTab("browser");
    useAppStore.getState().setRightPanelExpanded(true);
    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-startup" });
    expect(useAppStore.getState()).toMatchObject({
      conversationId: "conv-startup", workingDirectory: "C:/project", draft: "typed while reconnecting",
      rightStackTab: "browser", rightPanelOpen: true, rightPanelExpanded: true, rightStackTabLocked: true,
      selectedMentions: [{ kind: "file", path: "src/main.ts", name: "main.ts" }],
    });
  });

  it("clears usage when switching to a genuinely different conversation", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      contextUsage: { used: 64_000, limit: 128_000 },
      budgetBuckets: [{ name: "turn", used: 40, limit: 100 }],
      totalBudgetPercent: 0.4,
      lastUsage: { input: 80, output: 10, cacheRead: 20, cacheWrite: 5, reasoning: 3 },
      usageTotals: { input: 200, output: 25, cacheRead: 50, cacheWrite: 5, reasoning: 7, turns: 2 },
    });

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-next" });

    expect(useAppStore.getState()).toMatchObject({
      contextUsage: null,
      budgetBuckets: [],
      totalBudgetPercent: 0,
      lastUsage: null,
      usageTotals: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, turns: 0 },
    });
  });

  it("preserves global prompt queues while showing only the active conversation diff", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      pendingApproval: {
        requestId: "approval-active",
        toolName: "run_command",
        args: { command: "npm run build" },
        conversationId: "conv-active",
      },
      approvalQueue: [
        {
          requestId: "approval-queued",
          toolName: "read_file",
          args: { file_path: "README.md" },
          conversationId: "conv-active",
        },
      ],
      pendingDiffReview: {
        requestId: "diff-active",
        diff: "patch",
        conversationId: "conv-active",
        reviewState: {
          requestId: "diff-active",
          conversationId: "conv-active",
          protocol: "control",
          toolName: "edit_file",
          diff: "patch",
          files: [{ path: "src/demo.ts", additions: 3, deletions: 1 }],
          status: "pending",
          fileDecisions: {},
          lineComments: [],
        },
      },
      diffReview: {
        requestId: "diff-active",
        protocol: "control",
        toolName: "edit_file",
        diff: "patch",
        files: [{ path: "src/demo.ts", additions: 3, deletions: 1 }],
        status: "pending",
        fileDecisions: {},
      },
      pendingAskUser: {
        requestId: "ask-active",
        question: "Continue?",
        conversationId: "conv-active",
      },
    });

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-next" });

    const state = useAppStore.getState();
    expect(state.pendingApproval?.requestId).toBe("approval-active");
    expect(state.approvalQueue.map((item) => item.requestId)).toEqual(["approval-queued"]);
    expect(state.pendingDiffReview?.requestId).toBe("diff-active");
    expect(state.diffReview).toBeNull();
    expect(state.pendingAskUser?.requestId).toBe("ask-active");

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-active" });
    expect(useAppStore.getState().diffReview?.requestId).toBe("diff-active");
  });

  it("creating a conversation does not discard a background approval", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [{ id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" }],
      pendingApproval: {
        requestId: "approval-background",
        toolName: "run_command",
        args: { command: "npm test" },
        conversationId: "conv-active",
      },
    });

    useAppStore.getState().createConversation();

    expect(useAppStore.getState().pendingApproval?.requestId).toBe("approval-background");
  });

  it("canonical deletion removes only prompts owned by that conversation", async () => {
    useAppStore.setState({
      conversationId: "conv-keep",
      conversations: [
        { id: "conv-keep", title: "Keep", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-delete", title: "Delete", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      pendingApproval: {
        requestId: "approval-delete",
        toolName: "run_command",
        args: {},
        conversationId: "conv-delete",
      },
      approvalQueue: [{
        requestId: "approval-keep",
        toolName: "read_file",
        args: {},
        conversationId: "conv-keep",
      }],
      pendingAskUser: {
        requestId: "ask-keep",
        question: "Keep?",
        conversationId: "conv-keep",
      },
      askUserQueue: [{
        requestId: "ask-delete",
        question: "Delete?",
        conversationId: "conv-delete",
      }],
    });

    await useAppStore.getState().removeConversation("conv-delete");
    applyConversationList([
      { id: "conv-keep", title: "Keep", updatedAt: "2026-05-24T00:00:00.000Z" },
    ], "conv-keep");

    const state = useAppStore.getState();
    expect(state.pendingApproval?.requestId).toBe("approval-keep");
    expect(state.approvalQueue).toEqual([]);
    expect(state.pendingAskUser?.requestId).toBe("ask-keep");
    expect(state.askUserQueue).toEqual([]);
  });

  it("restores conversation-scoped plan and todo state when switching sessions", () => {
    const activePlan = {
      planId: "plan-active",
      status: "executing" as const,
      currentStep: 0,
      steps: [{ id: "a", title: "Active step", status: "running" as const }],
    };
    const nextPlan = {
      planId: "plan-next",
      status: "executing" as const,
      currentStep: 0,
      steps: [{ id: "n", title: "Next step", status: "running" as const }],
    };

    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      plan: activePlan,
      todos: [{ id: "todo-active", content: "Active todo", activeForm: "Active todo", status: "in_progress" }],
      conversationAgentStates: {
        "conv-next": {
          plan: nextPlan,
          todos: [{ id: "todo-next", content: "Next todo", activeForm: "Next todo", status: "pending" }],
          subagents: [],
          agentProgress: [],
        },
      },
    });

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-next" });

    let state = useAppStore.getState();
    expect(state.plan?.planId).toBe("plan-next");
    expect(state.todos.map((todo) => todo.id)).toEqual(["todo-next"]);
    expect(state.conversationAgentStates["conv-active"].plan?.planId).toBe("plan-active");

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-active" });

    state = useAppStore.getState();
    expect(state.plan?.planId).toBe("plan-active");
    expect(state.todos.map((todo) => todo.id)).toEqual(["todo-active"]);
  });

  it("restores conversation-scoped right workbench state when switching sessions", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      terminalSessions: [
        { id: "term-active", conversationId: "conv-active", shell: "pwsh", cwd: "C:\\projects\\active" },
        { id: "term-next", conversationId: "conv-next", shell: "pwsh", cwd: "C:\\projects\\next" },
      ],
      activeTerminalSessionId: "term-active",
      rightStackTab: "diff",
      rightPanelOpen: true,
      rightStackTabLocked: true,
      diffReview: {
        requestId: "diff-active",
        toolName: "edit_file",
        diff: "active patch",
        files: [{ path: "active.ts", additions: 1, deletions: 0 }],
        selectedPath: "active.ts",
        status: "viewing",
        mode: "view",
        fileDecisions: {},
        lineComments: [],
      },
      previewArtifact: {
        artifactId: "artifact-active",
        content: "active preview",
        loadedAt: 1,
      },
      livePreviewUrl: "http://localhost:3000",
      conversationWorkbenchStates: {
        "conv-next": {
          diffReview: {
            requestId: "diff-next",
            toolName: "edit_file",
            diff: "next patch",
            files: [{ path: "next.ts", additions: 2, deletions: 1 }],
            selectedPath: "next.ts",
            status: "viewing",
            mode: "view",
            fileDecisions: {},
            lineComments: [],
          },
          previewArtifact: {
            artifactId: "artifact-next",
            content: "next preview",
            loadedAt: 2,
          },
          livePreviewUrl: "http://localhost:4000",
          terminalSessions: [
            { id: "term-next", conversationId: "conv-next", shell: "pwsh", cwd: "C:\\projects\\next" },
          ],
          activeTerminalSessionId: "term-next",
          rightStackTab: "preview",
          rightPanelOpen: true,
          rightStackTabLocked: false,
          draft: "next draft",
          attachments: [],
          quotedMessage: null,
          selectedMentions: [],
          selectedSkills: [],
          allowedRemoteImageDomains: ["next.example"],
        },
      },
    });

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-next" });

    let state = useAppStore.getState();
    expect(state.rightStackTab).toBe("preview");
    expect(state.diffReview?.requestId).toBe("diff-next");
    expect(state.previewArtifact?.artifactId).toBe("artifact-next");
    expect(state.livePreviewUrl).toBe("http://localhost:4000");
    expect(state.terminalSessions.map((session) => session.id)).toEqual(["term-next"]);
    expect(state.activeTerminalSessionId).toBe("term-next");
    expect(state.conversationWorkbenchStates["conv-active"].diffReview?.requestId).toBe("diff-active");

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-active" });

    state = useAppStore.getState();
    expect(state.rightStackTab).toBe("diff");
    expect(state.diffReview?.requestId).toBe("diff-active");
    expect(state.previewArtifact?.artifactId).toBe("artifact-active");
    expect(state.livePreviewUrl).toBe("http://localhost:3000");
    expect(state.terminalSessions.map((session) => session.id)).toEqual(["term-active"]);
    expect(state.activeTerminalSessionId).toBe("term-active");
  });

  it("falls back only when the authoritative terminal list removes the preferred session", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      terminalSessions: [
        { id: "term-first", conversationId: "conv-active", shell: "pwsh", cwd: "C:\\projects\\active" },
        { id: "term-preferred", conversationId: "conv-active", shell: "pwsh", cwd: "C:\\projects\\active" },
      ],
      activeTerminalSessionId: "term-preferred",
      conversationWorkbenchStates: {
        "conv-active": {
          diffReview: null,
          previewArtifact: null,
          livePreviewUrl: null,
          terminalSessions: [],
          activeTerminalSessionId: "term-preferred",
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

    useAppStore.getState().setTerminalSessions([
      { id: "term-first", conversationId: "conv-active", shell: "pwsh", cwd: "C:\\projects\\active" },
    ]);
    expect(useAppStore.getState().activeTerminalSessionId).toBe("term-first");

    useAppStore.getState().setTerminalSessions([]);
    expect(useAppStore.getState().activeTerminalSessionId).toBeNull();
  });

  it("preserves the previous agent state when the backend confirms a new conversation", async () => {
    const activePlan = {
      planId: "plan-active",
      status: "executing" as const,
      currentStep: 0,
      steps: [{ id: "a", title: "Active step", status: "running" as const }],
    };

    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [{ id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" }],
      plan: activePlan,
      todos: [{ id: "todo-active", content: "Active todo", activeForm: "Active todo", status: "in_progress" }],
    });

    await useAppStore.getState().createConversation();
    const command = vi.mocked(sendClientCommandAwaitResult).mock.calls.at(-1)?.[0] as { conversation_id: string };
    const newId = command.conversation_id;
    useAppStore.setState((current) => ({
      conversations: [{ id: newId, title: "New chat", updatedAt: "2026-05-24T00:00:01.000Z" }, ...current.conversations],
    }));
    useAppStore.getState().applyConversationSwitched({ conversationId: newId });

    let state = useAppStore.getState();
    expect(state.conversationId).toBe(newId);
    expect(newId).not.toBe("conv-active");
    expect(state.plan).toBeNull();
    expect(state.todos).toEqual([]);
    expect(state.conversationAgentStates["conv-active"].plan?.planId).toBe("plan-active");
    expect(state.conversationAgentStates["conv-active"].todos.map((todo) => todo.id)).toEqual(["todo-active"]);
    expect(state.conversationAgentStates[newId!].todos).toEqual([]);

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-active" });

    state = useAppStore.getState();
    expect(state.plan?.planId).toBe("plan-active");
    expect(state.todos.map((todo) => todo.id)).toEqual(["todo-active"]);
  });

  it("preserves composer drafts and context independently per conversation", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      draft: "active draft",
      attachments: [{ id: "att-active", name: "active.png", type: "image/png", size: 12, status: "uploading" }],
      quotedMessage: { id: "quoted-active", role: "assistant", content: "active quote" },
      selectedMentions: [{ path: "src/active.ts", name: "active.ts", kind: "file" }],
      selectedSkills: [{ kind: "skill", name: "frontend-dev" }],
      allowedRemoteImageDomains: ["active.example"],
      conversationWorkbenchStates: {
        "conv-next": {
          diffReview: null,
          previewArtifact: null,
          livePreviewUrl: null,
          activeTerminalSessionId: null,
          rightStackTab: "tasks",
          rightPanelOpen: false,
          rightStackTabLocked: false,
          draft: "next draft",
          attachments: [{ id: "att-next", name: "next.txt", type: "text/plain", size: 4, status: "ready" }],
          quotedMessage: null,
          selectedMentions: [{ path: "src/next.ts", name: "next.ts", kind: "file" }],
          selectedSkills: [{ kind: "skill", name: "code-review" }],
          allowedRemoteImageDomains: ["next.example"],
        },
      },
    });

    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-next" });
    let state = useAppStore.getState();
    expect(state.draft).toBe("next draft");
    expect(state.attachments.map((attachment) => attachment.id)).toEqual(["att-next"]);
    expect(state.selectedMentions.map((mention) => mention.path)).toEqual(["src/next.ts"]);
    expect(state.selectedSkills.map((skill) => skill.name)).toEqual(["code-review"]);
    expect(state.quotedMessage).toBeNull();
    expect(state.allowedRemoteImageDomains).toEqual(["next.example"]);

    useAppStore.getState().updateAttachment("att-active", { status: "ready", artifactId: "artifact-active" });
    useAppStore.getState().applyConversationSwitched({ conversationId: "conv-active" });
    state = useAppStore.getState();
    expect(state.draft).toBe("active draft");
    expect(state.attachments[0]).toMatchObject({ id: "att-active", status: "ready", artifactId: "artifact-active" });
    expect(state.selectedMentions.map((mention) => mention.path)).toEqual(["src/active.ts"]);
    expect(state.selectedSkills.map((skill) => skill.name)).toEqual(["frontend-dev"]);
    expect(state.quotedMessage?.id).toBe("quoted-active");
    expect(state.allowedRemoteImageDomains).toEqual(["active.example"]);
  });

  it("clears composer state after canonical deletion of the final conversation", async () => {
    useAppStore.setState({
      conversationId: "conv-only",
      conversations: [{ id: "conv-only", title: "Only", updatedAt: "2026-05-24T00:00:00.000Z" }],
      conversationMessages: { "conv-only": [] },
      conversationStreaming: { "conv-only": false },
      draft: "do not leak",
      attachments: [{ id: "att-only", name: "only.txt", type: "text/plain", size: 4, status: "uploading" }],
      quotedMessage: { id: "quoted-only", role: "user", content: "quoted" },
      selectedMentions: [{ path: "only.ts", name: "only.ts", kind: "file" }],
      selectedSkills: [{ kind: "skill", name: "frontend-dev" }],
    });

    await useAppStore.getState().removeConversation("conv-only");
    applyConversationList([]);

    const state = useAppStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.draft).toBe("");
    expect(state.attachments).toEqual([]);
    expect(state.quotedMessage).toBeNull();
    expect(state.selectedMentions).toEqual([]);
    expect(state.selectedSkills).toEqual([]);
    expect(state.allowedRemoteImageDomains).toEqual([]);
  });

  it("restores the next conversation agent state after canonical deletion", async () => {
    const nextPlan = {
      planId: "plan-next",
      status: "executing" as const,
      currentStep: 0,
      steps: [{ id: "n", title: "Next step", status: "running" as const }],
    };

    useAppStore.setState({
      conversationId: "conv-old",
      conversations: [
        { id: "conv-old", title: "Old", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
      ],
      plan: {
        planId: "plan-old",
        status: "executing",
        currentStep: 0,
        steps: [{ id: "o", title: "Old step", status: "running" }],
      },
      todos: [{ id: "todo-old", content: "Old todo", activeForm: "Old todo", status: "in_progress" }],
      conversationMessages: { "conv-old": [], "conv-next": [] },
      conversationStreaming: { "conv-old": false, "conv-next": false },
      conversationAgentStates: {
        "conv-next": {
          plan: nextPlan,
          todos: [{ id: "todo-next", content: "Next todo", activeForm: "Next todo", status: "pending" }],
          subagents: [],
          agentProgress: [],
        },
      },
    });

    await useAppStore.getState().removeConversation("conv-old");
    applyConversationList([
      { id: "conv-next", title: "Next", updatedAt: "2026-05-24T00:00:01.000Z" },
    ], "conv-next");

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-next");
    expect(state.plan?.planId).toBe("plan-next");
    expect(state.todos.map((todo) => todo.id)).toEqual(["todo-next"]);
    expect(state.conversationAgentStates["conv-old"]).toBeUndefined();
  });
});
