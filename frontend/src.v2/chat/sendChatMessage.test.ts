/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getChatSendBlockReason, handleUserMessageAdmission, resetPendingUserMessagesForTests, sendChatMessage } from "./sendChatMessage";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { ClientCommand, CommandResultEvent, ServerEvent } from "../protocol/events";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleSessionEvent } from "./sessionEvents";
import { latestProjectionCommandIds } from "../protocol/command-projection";
import { composerWorkspaceKey } from "../stores/composer-drafts";
import { captureSteerTurnId } from "../lib/steer-command";
import { pushToast } from "../overlays/ToastContainer";

const wsMock = vi.hoisted(() => ({
  sent: [] as unknown[],
  clientCommands: [] as unknown[],
  throwOnSend: false,
  acceptSend: true,
  listeners: new Set<(event: unknown) => void>(),
}));
const sent = wsMock.sent;
const clientCommands = wsMock.clientCommands;

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({
    send: (command: unknown) => {
      if (wsMock.throwOnSend) {
        throw new Error("socket closed");
      }
      if (!wsMock.acceptSend) return false;
      sent.push(command);
      return true;
    },
    sessionId: "session-test",
    subscribe: (listener: (event: unknown) => void) => {
      wsMock.listeners.add(listener);
      return () => { wsMock.listeners.delete(listener); };
    },
  }),
}));

vi.mock("../protocol/api", async load => ({
  ...await load<typeof import("../protocol/api")>(),
  fetchDraftModelSelection: vi.fn(async (workspaceRoot: string, model: string, effort: string, _signal?: AbortSignal, configCwd = workspaceRoot) => ({
    provider: "custom", current_model: model || "fixture", model: model || "fixture", models_source: "configured",
    available_models: ["fixture"], available_model_options: [{ model: "fixture", reasoning_effort_levels: ["low", "medium", "high"], default_reasoning_effort: "medium" }],
    effective_reasoning_effort: effort || "medium", model_selection_mode: model ? "model" : "default",
    default_model_selection: { provider: "custom", model: "fixture", reasoning_effort: "medium", conversation_id: "", working_directory: workspaceRoot },
    available_permission_modes: ["confirm", "auto", "bypass"], feature_flags: {}, composer_commands: [], config_version: "fixture-config", config_cwd: configCwd,
  })),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/ws-outbox", async load => ({
  ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: (command: unknown) => {
    clientCommands.push(command);
    return true;
  },
  sendClientCommandAwaitResult: vi.fn(async (command: unknown, expectedCommand: string) => {
    clientCommands.push(command);
    return { type: "command.result", command: expectedCommand, level: "success", message: "", data: {} };
  }),
  commandResultSucceeded: (event: { level?: string }) => !["error", "failed"].includes(String(event.level || "")),
}));

const canonicalBuffers = { textStreamBuffer: { destroy: vi.fn(), flush: vi.fn(), push: vi.fn() },
  thinkingStreamBuffer: { destroy: vi.fn(), flush: vi.fn(), push: vi.fn() } } as never;
const generatedCwd = "C:/fixture-created-task";
const receiveCanonicalSwitch = (command: ClientCommand, hydrating: boolean) => {
  if (command.type !== "conversation.switch") throw new Error("Expected the actual switch command");
  const event = { type: "conversation.switched", client_command_id: command.client_command_id,
    client_command_type: command.type, conversation_id: command.conversation_id, is_hydrating: hydrating, context_pending: hydrating,
    conversation: { id: command.conversation_id, title: "Created fixture", workspace_root: "", execution_cwd: generatedCwd,
      revision: 2, messages: [], app_mode: "cowork", agent_mode: "build" },
    session: { active_conversation_id: command.conversation_id, workspace_root: generatedCwd, active_stream_conversation_ids: [] },
  } as ServerEvent;
  handleSessionEvent(event, canonicalBuffers);
  for (const listener of [...wsMock.listeners]) listener(event);
};
const defaultCommandResult = async (command: ClientCommand, expectedCommand: string): Promise<CommandResultEvent> => {
  clientCommands.push(command);
  if (command.type === "conversation.switch") throw new Error("A successful switch is canonical events, not command.result");
  return { type: "command.result", command: expectedCommand, level: "success", message: "",
    data: command.type === "conversation.create" ? { conversation_id: command.conversation_id, execution_cwd: generatedCwd, created: true, activated: false } : {} };
};

describe("sendChatMessage attachment feedback", () => {
  it("queues an owner-scoped selection with durable range metadata while the main draft stays intact", () => {
    const range = { startLineNumber: 40, startColumn: 3, endLineNumber: 56, endColumn: 1 };
    const contextRefs = [{ kind: "file" as const, name: "app.ts", path: "src/app.ts", workspaceRoot: "/side", range, text: "selected" }];
    useAppStore.setState({ draft: "main stays", sideChats: { side: { id: "side", draft: "follow up", workspaceRoot: "/side", isStreaming: true, messages: [
      { id: "running-side", role: "assistant", content: "running", isStreaming: true, artifacts: [], timestamp: 1 },
    ] } } });
    expect(sendChatMessage({ conversationId: "side", displayContent: "follow up", contextRefs, allowWhileStreaming: true })).toBe(true);
    expect(sent[0]).toMatchObject({ conversation_id: "side", workspace_root: "/side", queue_if_busy: true, context_refs: contextRefs });
    expect(useAppStore.getState().sideChats.side.messages.find((message) => message.role === "user")).toMatchObject({ contextRefs, queueState: "queued" });
    expect(useAppStore.getState().draft).toBe("main stays");
  });
  beforeEach(async () => {
    await waitComposerDraftRecovery();
    resetPendingUserMessagesForTests();
    latestProjectionCommandIds.clear();
    wsMock.listeners.clear();
    vi.mocked(sendClientCommandAwaitResult).mockReset().mockImplementation(defaultCommandResult);
    sent.length = 0;
    clientCommands.length = 0;
    wsMock.throwOnSend = false;
    wsMock.acceptSend = true;
    useAppStore.setState({
      conversationId: "conv-test",
      activeConversationDraftId: null,
      conversationDrafts: {}, homeConversationDraftIds: {}, workspaceComposerDrafts: {}, composerDraftsReady: true,
      pendingConversationSwitchId: null,
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      conversationRecallTruncations: {},
      isConnected: true,
      isStreaming: false,
      pendingApproval: null,
      pendingDiffReview: null,
      pendingAskUser: null,
      pendingPermissionModeChanges: {},
      runtimeSession: null,
      agentMode: "build",
      activeTabPath: null,
      activeEditorPath: null,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([false, true])("blocks the unowned current composer queue=%s during navigation without touching its draft", (allowWhileStreaming) => {
    const attachment = { id: "keep", name: "note.txt", type: "text/plain", size: 4, status: "ready" as const };
    const messages = allowWhileStreaming
      ? [{ id: "running", role: "assistant" as const, content: "working", timestamp: 1, isStreaming: true }]
      : [];
    useAppStore.setState({ pendingConversationSwitchId: "new-owner", draft: "old owner draft", attachments: [attachment],
      isStreaming: allowWhileStreaming, messages });
    expect(getChatSendBlockReason()).toBe("正在切换会话，请等待加载完成后发送。");
    expect(sendChatMessage({ displayContent: "old owner draft", allowWhileStreaming })).toBe(false);
    expect(sent).toHaveLength(0);
    expect(useAppStore.getState()).toMatchObject({ draft: "old owner draft", attachments: [attachment], messages, isStreaming: allowWhileStreaming });

    useAppStore.setState({ conversationId: "new-owner", pendingConversationSwitchId: null, draft: "new owner draft", messages: [], isStreaming: false });
    expect(sendChatMessage({ displayContent: "new owner draft" })).toBe(true);
    expect(sent[0]).toMatchObject({ conversation_id: "new-owner", content: "new owner draft" });
  });

  it.each([false, true])("continues an explicit background owner queue=%s while the main composer navigates", (allowWhileStreaming) => {
    const attachment = { id: "keep", name: "note.txt", type: "text/plain", size: 4, status: "ready" as const };
    const backgroundMessages = allowWhileStreaming
      ? [{ id: "running", role: "assistant" as const, content: "working", artifacts: [], timestamp: 1, isStreaming: true }]
      : [];
    useAppStore.setState({ conversationId: "new-owner", pendingConversationSwitchId: "new-owner", draft: "new owner draft", attachments: [attachment],
      conversations: [{ id: "conv-test", title: "Original owner", workspaceRoot: "C:/owner" }, { id: "new-owner", title: "New owner", workspaceRoot: "C:/new-owner" }],
      workingDirectory: "C:/new-owner", messages: [], isStreaming: false, sideChats: {},
      conversationMessages: { "conv-test": backgroundMessages }, conversationStreaming: { "conv-test": allowWhileStreaming } });
    expect(getChatSendBlockReason()).toBe("正在切换会话，请等待加载完成后发送。");
    expect(getChatSendBlockReason("conv-test")).toBe(allowWhileStreaming ? "当前对话仍在运行，请等待完成或先停止；你也可以在新对话中继续。" : null);
    expect(sendChatMessage({ conversationId: "conv-test", displayContent: "original owner follow up", allowWhileStreaming })).toBe(true);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ conversation_id: "conv-test", workspace_root: "C:/owner", content: "original owner follow up" });
    expect(useAppStore.getState()).toMatchObject({ conversationId: "new-owner", pendingConversationSwitchId: "new-owner", draft: "new owner draft", attachments: [attachment], messages: [], isStreaming: false });
    expect(useAppStore.getState().conversationMessages["conv-test"].some(message => message.role === "user" && message.content === "original owner follow up")).toBe(true);
  });

  it("allows an explicitly owned side chat while the main conversation switches", () => {
    useAppStore.setState({ pendingConversationSwitchId: "new-owner", draft: "main draft", sideChats: {
      side: { id: "side", draft: "side draft", messages: [], isStreaming: false, workspaceRoot: "C:/side" },
    } });
    expect(sendChatMessage({ conversationId: "side", displayContent: "side draft" })).toBe(true);
    expect(sent[0]).toMatchObject({ conversation_id: "side", workspace_root: "C:/side" });
    expect(useAppStore.getState().draft).toBe("main draft");
  });

  it("continues the captured owner after its permission ACK while later navigation stays pending", async () => {
    let confirm!: (result: CommandResultEvent) => void;
    vi.mocked(sendClientCommandAwaitResult).mockReturnValueOnce(new Promise((resolve) => { confirm = resolve; }));
    useAppStore.setState({ workingDirectory: "C:/owner", draft: "keep draft",
      conversations: [{ id: "conv-test", title: "Owner", workspaceRoot: "C:/owner" }] });
    const modeChange = useAppStore.getState().setPermissionMode("bypass");
    const submission = sendChatMessage({ displayContent: "keep draft", allowWhileStreaming: true });
    useAppStore.setState({ pendingConversationSwitchId: "new-owner" });
    confirm({ type: "command.result", command: "conversation.permission_mode.set", level: "info", message: "", data: {} });
    expect(await modeChange).toBe(true);
    expect(await submission).toBe(true);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ conversation_id: "conv-test", workspace_root: "C:/owner", content: "keep draft" });
    expect(sent[0]).not.toHaveProperty("permission_mode");
    expect(useAppStore.getState().draft).toBe("keep draft");
    expect(useAppStore.getState().pendingConversationSwitchId).toBe("new-owner");
  });

  it("waits for the owner permission ACK before sending without overriding the stored mode", async () => {
    let confirm!: (result: CommandResultEvent) => void;
    vi.mocked(sendClientCommandAwaitResult).mockReturnValueOnce(new Promise((resolve) => { confirm = resolve; }));
    useAppStore.setState({ permissionMode: "confirm", workingDirectory: "C:/owner", draft: "weather research",
      conversations: [{ id: "conv-test", title: "Owner", workspaceRoot: "C:/owner" }] });
    const modeChange = useAppStore.getState().setPermissionMode("bypass");
    const submission = sendChatMessage({ displayContent: "weather research" });

    expect(sent).toHaveLength(0);
    expect(useAppStore.getState().messages).toHaveLength(0);
    expect(useAppStore.getState().permissionMode).toBe("confirm");
    confirm({ type: "command.result", command: "conversation.permission_mode.set", level: "info", message: "",
      data: { mode: "bypass", conversation_id: "conv-test", revision: 2 } });

    expect(await modeChange).toBe(true);
    expect(await submission).toBe(true);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ conversation_id: "conv-test", workspace_root: "C:/owner" });
    expect(sent[0]).not.toHaveProperty("permission_mode");
    expect(useAppStore.getState().permissionMode).toBe("bypass");
    expect(useAppStore.getState().pendingPermissionModeChanges).toEqual({});
  });

  it.each(["refused", "disconnected"] as const)("preserves draft and attachments when the permission change is %s", async (failure) => {
    let confirm!: (result: CommandResultEvent) => void;
    let reject!: (error: Error) => void;
    vi.mocked(sendClientCommandAwaitResult).mockReturnValueOnce(new Promise((resolve, rejectPromise) => { confirm = resolve; reject = rejectPromise; }));
    const attachment = { id: "keep", name: "note.txt", type: "text/plain", size: 4, status: "ready" as const };
    useAppStore.setState({ permissionMode: "confirm", workingDirectory: "C:/owner", draft: "keep this draft", attachments: [attachment] });
    const modeChange = useAppStore.getState().setPermissionMode("bypass");
    const submission = sendChatMessage({ displayContent: "keep this draft" });
    if (failure === "refused") confirm({ type: "command.result", command: "conversation.permission_mode.set", level: "error", message: "Permission change rejected", data: {} });
    else reject(new Error("Connection closed"));

    expect(await modeChange).toBe(false);
    expect(await submission).toBe(false);
    expect(sent).toHaveLength(0);
    expect(useAppStore.getState().messages).toHaveLength(0);
    expect(useAppStore.getState()).toMatchObject({ draft: "keep this draft", attachments: [attachment], permissionMode: "confirm" });
  });

  it("keeps a delayed permission ACK and submission bound to the original conversation", async () => {
    let confirm!: (result: CommandResultEvent) => void;
    vi.mocked(sendClientCommandAwaitResult).mockReturnValueOnce(new Promise((resolve) => { confirm = resolve; }));
    useAppStore.setState({ permissionMode: "confirm", workingDirectory: "C:/owner-a", draft: "A request", attachments: [],
      conversations: [{ id: "conv-test", title: "A", workspaceRoot: "C:/owner-a" }, { id: "owner-b", title: "B", workspaceRoot: "C:/owner-b" }] });
    const modeChange = useAppStore.getState().setPermissionMode("bypass");
    const submission = sendChatMessage({ displayContent: "A request" });
    useAppStore.getState().snapshotWorkbenchState("conv-test");
    const newerAttachment = { id: "new-b", name: "B.txt", type: "text/plain", size: 1, status: "ready" as const };
    useAppStore.setState({ conversationId: "owner-b", workingDirectory: "C:/owner-b", permissionMode: "auto", draft: "B unsent draft", attachments: [newerAttachment], messages: [] });
    confirm({ type: "command.result", command: "conversation.permission_mode.set", level: "info", message: "", data: { mode: "bypass", conversation_id: "conv-test" } });

    expect(await modeChange).toBe(true);
    expect(await submission).toBe(true);
    expect(sent[0]).toMatchObject({ conversation_id: "conv-test", content: "A request", workspace_root: "C:/owner-a" });
    expect(sent[0]).not.toHaveProperty("permission_mode");
    expect(useAppStore.getState()).toMatchObject({ conversationId: "owner-b", permissionMode: "auto", draft: "B unsent draft", attachments: [newerAttachment], messages: [] });
    expect(useAppStore.getState().conversationWorkbenchStates["conv-test"].draft).toBe("A request");
  });

  it("does not submit an old workspace draft after its owner workspace changes during permission confirmation", async () => {
    let confirm!: (result: CommandResultEvent) => void;
    vi.mocked(sendClientCommandAwaitResult).mockReturnValueOnce(new Promise((resolve) => { confirm = resolve; }));
    useAppStore.setState({ permissionMode: "confirm", workingDirectory: "C:/old-owner", draft: "old workspace request" });
    const modeChange = useAppStore.getState().setPermissionMode("bypass");
    const submission = sendChatMessage({ displayContent: "old workspace request" });
    useAppStore.setState({ workingDirectory: "C:/new-owner", draft: "new workspace draft", permissionMode: "auto" });
    confirm({ type: "command.result", command: "conversation.permission_mode.set", level: "info", message: "", data: {} });

    expect(await modeChange).toBe(true);
    expect(await submission).toBe(false);
    expect(sent).toHaveLength(0);
    expect(useAppStore.getState()).toMatchObject({ workingDirectory: "C:/new-owner", draft: "new workspace draft", permissionMode: "auto" });
  });

  it("waits for a newer permission selection made while its original ACK is pending", async () => {
    let firstAck!: (result: CommandResultEvent) => void;
    let secondAck!: (result: CommandResultEvent) => void;
    vi.mocked(sendClientCommandAwaitResult)
      .mockReturnValueOnce(new Promise((resolve) => { firstAck = resolve; }))
      .mockReturnValueOnce(new Promise((resolve) => { secondAck = resolve; }));
    useAppStore.setState({ permissionMode: "confirm", workingDirectory: "C:/owner",
      conversations: [{ id: "conv-test", title: "Owner", workspaceRoot: "C:/owner" }] });
    const first = useAppStore.getState().setPermissionMode("bypass");
    const submission = sendChatMessage({ displayContent: "Read the files" });
    const second = useAppStore.getState().setPermissionMode("auto");
    firstAck({ type: "command.result", command: "conversation.permission_mode.set", level: "info", message: "", data: {} });
    expect(await first).toBe(true);
    expect(sent).toHaveLength(0);
    secondAck({ type: "command.result", command: "conversation.permission_mode.set", level: "info", message: "", data: {} });
    expect(await second).toBe(true);
    expect(await submission).toBe(true);
    expect(sent).toHaveLength(1);
    expect(sent[0]).not.toHaveProperty("permission_mode");
    expect(useAppStore.getState().permissionMode).toBe("auto");
  });

  it("does not send after the original owner is removed while permission is pending", async () => {
    let confirm!: (result: CommandResultEvent) => void;
    vi.mocked(sendClientCommandAwaitResult).mockReturnValueOnce(new Promise((resolve) => { confirm = resolve; }));
    useAppStore.setState({ workingDirectory: "C:/owner", sideChats: {},
      conversations: [{ id: "conv-test", title: "Owner", workspaceRoot: "C:/owner" }] });
    const modeChange = useAppStore.getState().setPermissionMode("bypass");
    const submission = sendChatMessage({ displayContent: "Read the files" });
    useAppStore.setState({ conversationId: "other", conversations: [], workingDirectory: "C:/other", draft: "keep" });
    confirm({ type: "command.result", command: "conversation.permission_mode.set", level: "info", message: "", data: {} });
    expect(await modeChange).toBe(true);
    expect(await submission).toBe(false);
    expect(sent).toHaveLength(0);
    expect(useAppStore.getState().draft).toBe("keep");
  });

  it("keeps a live side-chat turn live when another message is rejected", () => {
    useAppStore.setState({ sideChats: {
      side: { id: "side", draft: "", isStreaming: true, messages: [{ id: "side-answer", role: "assistant", content: "working", isStreaming: true, artifacts: [], timestamp: 1 }] },
    } });
    expect(sendChatMessage({ displayContent: "follow up", conversationId: "side" })).toBe(false);
    expect(sent).toHaveLength(0);
    expect(useAppStore.getState().sideChats.side.messages[0].isStreaming).toBe(true);
  });

  it("keeps the main plan and editor context out of a side-chat submission", () => {
    const mainPlan = { title: "Main task", steps: [], status: "in_progress" as const };
    useAppStore.setState({
      plan: mainPlan, activeTabPath: "main-only.ts",
      conversations: [{ id: "side", title: "Side", workspaceRoot: "C:/side" }],
      sideChats: { side: { id: "side", draft: "", isStreaming: false, messages: [] } },
    });
    expect(sendChatMessage({ displayContent: "side task", conversationId: "side" })).toBe(true);
    expect(useAppStore.getState().plan).toEqual(mainPlan);
    expect(useAppStore.getState().sideChats.side.messages).toHaveLength(2);
    expect(sent[0]).not.toHaveProperty("primary_file");
    expect(sent[0]).not.toHaveProperty("active_tab_path");
  });

  it("keeps uploaded file refs on the local user message", () => {
    const ok = sendChatMessage({
      displayContent: "please inspect this",
      backendContent: "please inspect this",
      attachments: [{
        id: "att-1",
        file_name: "report.pdf",
        kind: "document",
        media_type: "application/pdf",
        artifact_id: "artifact-1",
        doc_id: "doc-1",
        size_bytes: 2048,
        data: "JVBERi0xLjQ=".repeat(10_000),
      }],
      attachmentRefs: [{
        id: "att-1",
        name: "report.pdf",
        kind: "document",
        mediaType: "application/pdf",
        artifactId: "artifact-1",
        docId: "doc-1",
        sizeBytes: 2048,
      }],
    });

    expect(ok).toBe(true);
    expect(sent).toHaveLength(1);
    const sentCommand = sent[0] as { attachments?: Record<string, unknown>[] };
    expect(sentCommand.attachments?.[0]).not.toHaveProperty("data");
    expect(JSON.stringify(sent[0]).length).toBeLessThan(2_000);
    expect(useAppStore.getState().messages[0]).toMatchObject({
      role: "user",
      content: "please inspect this",
      attachmentRefs: [{
        name: "report.pdf",
        artifactId: "artifact-1",
      }],
    });
  });

  it("does not deduplicate the same text when the selected plugin changes", () => {
    const first = sendChatMessage({
      displayContent: "check this",
      backendContent: "check this",
      contextRefs: [{
        kind: "plugin",
        name: "docs",
        configName: "docs",
        path: "plugin://docs",
      }],
      skipLocalAppend: true,
    });
    const second = sendChatMessage({
      displayContent: "check this",
      backendContent: "check this",
      contextRefs: [{
        kind: "plugin",
        name: "review",
        configName: "review",
        path: "plugin://review",
      }],
      skipLocalAppend: true,
    });

    expect(first).toBe(true);
    expect(second).toBe(true);
    expect(sent).toHaveLength(2);
    expect(sent[0]).toMatchObject({
      plugins: [{ config_name: "docs", path: "plugin://docs" }],
    });
    expect(sent[1]).toMatchObject({
      plugins: [{ config_name: "review", path: "plugin://review" }],
    });
  });

  it("sends caller-owned ids for a side-chat placeholder", () => {
    const ok = sendChatMessage({
      displayContent: "side question",
      backendContent: "side question",
      conversationId: "side-1",
      skipLocalAppend: true,
      assistantMessageId: "m-side-assistant",
      userMessageId: "m-side-user",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      conversation_id: "side-1",
      assistant_message_id: "m-side-assistant",
      user_message_id: "m-side-user",
    });
  });

  it("keeps artifact identity but drops temporary blob URLs from sent image messages", () => {
    const ok = sendChatMessage({
      displayContent: "what is this?",
      attachments: [{
        id: "att-image",
        file_name: "image.png",
        kind: "image",
        media_type: "image/png",
        artifact_id: "artifact-image",
      }],
      attachmentRefs: [{
        id: "att-image",
        name: "image.png",
        kind: "image",
        mediaType: "image/png",
        sizeBytes: 1024,
        artifactId: "artifact-image",
        dataUrl: "blob:http://localhost/temporary-image",
      }],
    });

    expect(ok).toBe(true);
    expect(useAppStore.getState().messages[0]?.attachmentRefs).toEqual([
      expect.objectContaining({ artifactId: "artifact-image", name: "image.png" }),
    ]);
    expect(useAppStore.getState().messages[0]?.attachmentRefs?.[0]).not.toHaveProperty("dataUrl");
  });

  it("sends the local streaming assistant id so backend events bind to the same turn", () => {
    const ok = sendChatMessage({
      displayContent: "换个游戏吧 我要完cf",
      backendContent: "换个游戏吧 我要完cf",
    });

    expect(ok).toBe(true);
    const assistant = useAppStore.getState().messages.find((message) => message.role === "assistant");
    expect(assistant?.isStreaming).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "换个游戏吧 我要完cf",
      assistant_message_id: assistant?.id,
    });
  });

  it("sends attachment-only pasted text without inventing a separate prompt", () => {
    const attachment = {
      id: "artifact-paste",
      file_name: "pasted-3.txt",
      kind: "document",
      media_type: "text/plain",
      artifact_id: "artifact-paste",
      input_source: "pasted_text",
      source_char_count: 22_000,
    };

    const ok = sendChatMessage({
      displayContent: "",
      backendContent: "",
      attachments: [attachment],
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "",
      attachments: [attachment],
    });
    expect(useAppStore.getState().messages[0]).toMatchObject({
      role: "user",
      content: "",
      attachmentRefs: [{
        name: "pasted-3.txt",
        artifactId: "artifact-paste",
        inputSource: "pasted_text",
        sourceCharCount: 22_000,
      }],
    });
  });

  it("queues a follow-up without replacing the active turn state", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      isStreaming: true,
      conversationStreaming: { "conv-test": true },
      messages: [{
        id: "assistant-running",
        turnId: "real-active-turn",
        role: "assistant",
        content: "working",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      plan: {
        threadId: "conv-test",
        turnId: "turn-active",
        plan: [{ step: "Keep working", status: "in_progress" }],
      },
    });

    const ok = sendChatMessage({
      displayContent: "do this next",
      backendContent: "do this next",
      allowWhileStreaming: true,
    });

    const state = useAppStore.getState();
    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "do this next",
      queue_if_busy: true,
      streaming_behavior: "follow_up",
      user_message_id: expect.any(String),
      assistant_message_id: expect.any(String),
    });
    expect(state.plan?.turnId).toBe("turn-active");
    expect(state.messages.at(-2)).toMatchObject({ role: "user", queueState: "queued" });
    expect(state.messages.at(-1)).toMatchObject({ role: "assistant", queueState: "queued", isStreaming: false });
  });

  it("allows sending after the server confirms a freshly created conversation", async () => {
    useAppStore.setState({
      conversationId: "conv-old",
      conversations: [{ id: "conv-old", title: "Old", updatedAt: "2026-01-01T00:00:00.000Z" }],
      messages: [
        { id: "old-user", role: "user", content: "old", artifacts: [], timestamp: 1 },
        {
          id: "old-assistant",
          role: "assistant",
          content: "",
          blocks: [{ type: "thinking", content: "still working" }],
          artifacts: [],
          timestamp: 2,
          isStreaming: true,
          isThinkingStreaming: true,
        },
      ],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: true,
    });

    await useAppStore.getState().createConversation();
    const createCommand = clientCommands.find((command) => (command as { type?: string }).type === "conversation.create") as { conversation_id: string };
    const newConversationId = createCommand.conversation_id;
    useAppStore.setState((state) => ({
      conversations: [{ id: newConversationId, title: "New chat", updatedAt: "2026-01-01T00:00:01.000Z" }, ...state.conversations],
    }));
    useAppStore.getState().applyConversationSwitched({ conversationId: newConversationId });
    expect(newConversationId).toBeTruthy();
    expect(newConversationId).not.toBe("conv-old");
    expect(useAppStore.getState().isStreaming).toBe(false);
    expect(useAppStore.getState().conversationStreaming["conv-old"]).toBe(true);

    const ok = sendChatMessage({
      displayContent: "new task",
      backendContent: "new task",
    });

    expect(ok).toBe(true);
    expect(sent.at(-1)).toMatchObject({
      type: "user_message",
      conversation_id: newConversationId,
      content: "new task",
    });
    expect(useAppStore.getState().messages.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(useAppStore.getState().conversationStreaming["conv-old"]).toBe(true);
  });

  it("recalls uploaded file refs into the composer attachments", async () => {
    useAppStore.getState().sendMessage("check this", {
      assistant: false,
      attachmentRefs: [{
        id: "att-2",
        name: "diagram.png",
        kind: "image",
        mediaType: "image/png",
        artifactId: "artifact-2",
        docId: "doc-2",
        sizeBytes: 4096,
      }],
    });

    const userMessage = useAppStore.getState().messages[0];
    await useAppStore.getState().recallMessage(userMessage.id);

    expect(useAppStore.getState().draft).toBe("check this");
    expect(useAppStore.getState().attachments[0]).toMatchObject({
      name: "diagram.png",
      type: "image/png",
      status: "ready",
      conversationId: "conv-test",
      artifactId: "artifact-2",
      attachment: {
        artifact_id: "artifact-2",
        file_name: "diagram.png",
      },
    });
    expect(useAppStore.getState().actionChip).toBeNull();
  });

  it("keeps the recalled attachment owner and durable handle on the second send", async () => {
    useAppStore.getState().sendMessage("first request", {
      assistant: false,
      attachmentRefs: [{
        id: "att-recall-resend",
        name: "requirements.txt",
        kind: "document",
        mediaType: "text/plain",
        artifactId: "artifact-recall-resend",
        docId: "doc-recall-resend",
        sizeBytes: 128,
      }],
    });

    const userMessage = useAppStore.getState().messages[0];
    expect(await useAppStore.getState().recallMessage(userMessage.id)).toBe(true);
    const restored = useAppStore.getState().attachments[0];

    expect(restored).toMatchObject({
      status: "ready",
      conversationId: "conv-test",
      artifactId: "artifact-recall-resend",
    });
    expect(sendChatMessage({
      displayContent: "updated request",
      backendContent: "updated request",
      attachments: [restored.attachment!],
      attachmentRefs: [{
        id: "att-recall-resend",
        name: restored.name,
        kind: "document",
        mediaType: restored.type,
        artifactId: restored.artifactId,
        docId: restored.docId,
        sizeBytes: restored.size,
      }],
      conversationId: restored.conversationId,
    })).toBe(true);

    expect(sent.at(-1)).toMatchObject({
      type: "user_message",
      conversation_id: "conv-test",
      content: "updated request",
      attachments: [{
        artifact_id: "artifact-recall-resend",
        doc_id: "doc-recall-resend",
        file_name: "requirements.txt",
      }],
    });
  });

  it("does not mark a recalled attachment without a durable handle as ready", async () => {
    useAppStore.getState().sendMessage("legacy attachment", {
      assistant: false,
      attachmentRefs: [{
        id: "legacy-only-id",
        name: "legacy.txt",
        kind: "document",
        mediaType: "text/plain",
        sizeBytes: 12,
      }],
    });

    const userMessage = useAppStore.getState().messages[0];
    expect(await useAppStore.getState().recallMessage(userMessage.id)).toBe(true);

    expect(useAppStore.getState().attachments[0]).toMatchObject({
      name: "legacy.txt",
      status: "error",
      conversationId: "conv-test",
      error: "原附件缺少可验证的持久化引用，请重新上传。",
    });
    expect(useAppStore.getState().attachments[0]).not.toHaveProperty("attachment");
  });

  it("recalls from a user message and removes the later transcript", async () => {
    useAppStore.setState({
      messages: [
        {
          id: "user-1",
          role: "user",
          content: "help me optimize this project",
          artifacts: [],
          timestamp: 1,
        },
        {
          id: "assistant-working",
          role: "assistant",
          content: "",
          blocks: [{ type: "thinking", content: "Choosing the next step" }],
          artifacts: [],
          timestamp: 2,
        },
        {
          id: "assistant-recall",
          role: "assistant",
          content: "Recalling previous edits",
          blocks: [
            {
              type: "tool_call",
              record: {
                id: "tool-1",
                name: "recall",
                status: "completed",
                input: { query: "project optimization" },
                output: "Found 3 prior changes",
              },
            },
            { type: "text", content: "Recalling previous edits" },
          ],
          artifacts: [],
          timestamp: 3,
        },
        {
          id: "assistant-final",
          role: "assistant",
          content: "I found the previous changes and can continue from there.",
          artifacts: [],
          timestamp: 4,
        },
        {
          id: "user-2",
          role: "user",
          content: "next turn",
          artifacts: [],
          timestamp: 5,
        },
      ],
      isStreaming: false,
      conversationId: "conv-test",
    });

    await useAppStore.getState().recallMessage("user-1");

    expect(useAppStore.getState().draft).toBe("help me optimize this project");
    expect(useAppStore.getState().messages.map((message) => message.id)).toEqual([]);
    expect(clientCommands).toContainEqual({
      type: "conversation.truncate",
      conversation_id: "conv-test",
      truncate_before_message_id: "user-1",
      retained_message_ids: [],
    });
    expect(clientCommands).toContainEqual({
      type: "session.usage.inspect",
      conversation_id: "conv-test",
      source: "conversation_recall",
      silent: true,
    });
  });

  it("does not resurrect recalled-away messages when the old transcript hydrates again", async () => {
    const transcript = [
      {
        id: "user-1",
        role: "user" as const,
        content: "help me optimize this project",
        artifacts: [],
        timestamp: 1,
      },
      {
        id: "assistant-old",
        role: "assistant" as const,
        content: "old branch answer",
        artifacts: [],
        timestamp: 2,
      },
      {
        id: "user-old-tail",
        role: "user" as const,
        content: "old tail",
        artifacts: [],
        timestamp: 3,
      },
    ];
    useAppStore.setState({
      conversationId: "conv-test",
      messages: transcript,
      conversationMessages: { "conv-test": transcript },
      conversationStreaming: { "conv-test": false },
      conversationRecallTruncations: {},
      isStreaming: false,
    });

    await useAppStore.getState().recallMessage("user-1");
    useAppStore.getState().hydrateConversationMessages("conv-test", transcript, { activate: true, isStreaming: false });

    const state = useAppStore.getState();
    expect(state.messages).toEqual([]);
    expect(state.conversationMessages["conv-test"]).toEqual([]);
    expect(state.conversationRecallTruncations["conv-test"].removedIds).toEqual([
      "user-1",
      "assistant-old",
      "user-old-tail",
    ]);
  });

  it("recalls an assistant reply by returning to its user turn", async () => {
    useAppStore.setState({
      messages: [
        {
          id: "user-1",
          role: "user",
          content: "first turn",
          artifacts: [],
          timestamp: 1,
        },
        {
          id: "assistant-1",
          role: "assistant",
          content: "first answer",
          artifacts: [],
          timestamp: 2,
        },
        {
          id: "user-2",
          role: "user",
          content: "second turn",
          artifacts: [],
          timestamp: 3,
        },
        {
          id: "assistant-2",
          role: "assistant",
          content: "second answer",
          artifacts: [],
          timestamp: 4,
        },
      ],
      isStreaming: false,
      conversationId: "conv-test",
    });

    await useAppStore.getState().recallMessage("assistant-1");

    expect(useAppStore.getState().draft).toBe("first turn");
    expect(useAppStore.getState().messages.map((message) => message.id)).toEqual([]);
  });

  it("promotes a busy follow-up into the active turn when steer is selected", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      isStreaming: true,
      conversationStreaming: { "conv-test": true },
      messages: [{
        id: "assistant-running",
        turnId: "real-active-turn",
        role: "assistant",
        content: "working",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
    });

    expect(sendChatMessage({
      displayContent: "change direction",
      backendContent: "change direction",
      allowWhileStreaming: true,
      busyBehavior: "steer",
    })).toBe(true);

    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      queue_if_busy: true,
      streaming_behavior: "steer",
      expected_turn_id: "real-active-turn",
    });
  });

  it.each(["side", "background"])("captures the owner's actual turn for %s steering", location => {
    const messages = [{ id: "assistant-a", turnId: "actual-a-turn", role: "assistant" as const, content: "working", artifacts: [], timestamp: 1, isStreaming: true }];
    useAppStore.setState({ conversationId: "reader-b", isStreaming: true,
      messages: [{ ...messages[0], id: "assistant-b", turnId: "actual-b-turn" }], draft: "keep B draft",
      sideChats: location === "side" ? { a: { id: "a", draft: "keep side draft", messages, isStreaming: true } } : {},
      conversationMessages: { a: messages }, conversationStreaming: { a: true } });
    expect(sendChatMessage({ conversationId: "a", displayContent: "steer A", allowWhileStreaming: true, busyBehavior: "steer" })).toBe(true);
    expect(sent[0]).toMatchObject({ conversation_id: "a", streaming_behavior: "steer", expected_turn_id: "actual-a-turn" });
    expect(useAppStore.getState()).toMatchObject({ conversationId: "reader-b", draft: "keep B draft" });
  });

  it("keeps the draft when a busy optimistic assistant has no real turn identity", () => {
    const messages = [{ id: "assistant-is-not-a-turn", role: "assistant" as const, content: "", artifacts: [], timestamp: 1, isStreaming: true }];
    useAppStore.setState({ conversationId: "conv-test", messages, isStreaming: true, sideChats: {}, draft: "keep missing-target draft" });
    expect(sendChatMessage({ displayContent: "keep missing-target draft", allowWhileStreaming: true, busyBehavior: "steer" })).toBe(false);
    expect(sent).toEqual([]);
    expect(useAppStore.getState()).toMatchObject({ draft: "keep missing-target draft", messages });
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("回合标识"), "warning");
  });

  it("keeps the initially captured steer target through permission confirmation and a newer real turn", async () => {
    let release!: (accepted: boolean) => void;
    const confirmation = new Promise<boolean>(resolve => { release = resolve; });
    useAppStore.setState({ conversationId: "conv-test", workingDirectory: "C:/owner", isStreaming: true, sideChats: {},
      conversations: [{ id: "conv-test", title: "Owner", workspaceRoot: "C:/owner" }],
      messages: [{ id: "old-assistant", turnId: "old-real-turn", role: "assistant", content: "working", artifacts: [], timestamp: 1, isStreaming: true }],
      pendingPermissionModeChanges: { "conv-test": { workspaceRoot: "C:/owner", confirmation } } });
    const submission = sendChatMessage({ displayContent: "old turn direction", allowWhileStreaming: true, busyBehavior: "steer" });
    useAppStore.setState({ pendingPermissionModeChanges: {}, messages: [{ id: "new-assistant", turnId: "new-real-turn", role: "assistant", content: "new work", artifacts: [], timestamp: 2, isStreaming: true }] });
    release(true);
    expect(await submission).toBe(true);
    expect(sent[0]).toMatchObject({ streaming_behavior: "steer", expected_turn_id: "old-real-turn" });
  });

  it("does not turn a normal send into a steer after asynchronous context preparation", () => {
    useAppStore.setState({ isStreaming: false, sideChats: {} });
    const expectedTurnId = captureSteerTurnId(useAppStore.getState());
    useAppStore.setState({ isStreaming: true, messages: [{ id: "later-assistant", turnId: "later-turn", role: "assistant", content: "working", artifacts: [], timestamp: 2, isStreaming: true }] });
    expect(sendChatMessage({ displayContent: "normal original send", allowWhileStreaming: true, busyBehavior: "steer", expectedTurnId })).toBe(true);
    expect(sent[0]).toMatchObject({ streaming_behavior: "follow_up", queue_if_busy: true });
    expect(sent[0]).not.toHaveProperty("expected_turn_id");
  });

  it("submits an old captured target for explicit rejection after that turn became idle", async () => {
    useAppStore.setState({ isStreaming: false, messages: [], sideChats: {}, draft: "preserve authored direction" });
    const onRejected = vi.fn();
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({ type: "command.result", command: "user_message", level: "error",
      message: "The selected turn has already ended.", data: { conversation_id: "conv-test", reason: "steer_target_unavailable", admitted: false, queued: false,
        expected_turn_id: "ended-real-turn", active_turn_id: "" } });
    expect(await sendChatMessage({ displayContent: "preserve authored direction", allowWhileStreaming: true, busyBehavior: "steer",
      expectedTurnId: "ended-real-turn", awaitAdmission: true, onRejected })).toBe(false);
    expect(vi.mocked(sendClientCommandAwaitResult).mock.lastCall?.[0]).toMatchObject({ streaming_behavior: "steer", expected_turn_id: "ended-real-turn" });
    expect(onRejected).toHaveBeenCalledOnce();
    expect(useAppStore.getState().draft).toBe("preserve authored direction");
  });

  it("withdraws only the ACKed rejected steer's local queue display while preserving receipt evidence and a real neighboring queue", async () => {
    const owner = "rejected-steer-owner";
    const attachmentRefs = [{ id: "kept-attachment", name: "notes.txt", kind: "document" as const, artifactId: "real-artifact" }];
    const live = { id: "current-answer", turnId: "new-real-turn", role: "assistant" as const, content: "current work", artifacts: [], timestamp: 1, isStreaming: true };
    const realQueue = { id: "actual-queued-user", role: "user" as const, content: "already accepted queue", artifacts: [], timestamp: 2,
      queueState: "queued" as const, queueMessageId: "actual-queued-answer", queuePosition: 1 };
    useAppStore.setState({ conversationId: "reader-b", messages: [], isStreaming: false, sideChats: {}, draft: "keep B draft", workingDirectory: "C:/b",
      conversationMessages: { [owner]: [live, realQueue] }, conversationStreaming: { [owner]: true } });
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(async (command, _type, options) => {
      options?.onReceived?.();
      expect(useAppStore.getState().conversationMessages[owner].find(message => message.id === "rejected-user")?.queueState).toBe("queued");
      return { type: "command.result", command: "user_message", client_command_id: command.client_command_id, level: "error",
        message: "The expected turn already ended; input was not queued.", data: { conversation_id: owner, admitted: false, queued: false,
          reason: "steer_target_mismatch", expected_turn_id: "old-real-turn", active_turn_id: "new-real-turn" } };
    });
    const onRejected = vi.fn();
    expect(await sendChatMessage({ conversationId: owner, displayContent: "keep rejected direction", attachmentRefs,
      allowWhileStreaming: true, busyBehavior: "steer", expectedTurnId: "old-real-turn", awaitAdmission: true,
      userMessageId: "rejected-user", assistantMessageId: "rejected-answer", onRejected })).toBe(false);
    const state = useAppStore.getState();
    const rejected = state.conversationMessages[owner].find(message => message.id === "rejected-user")!;
    expect(rejected).toMatchObject({ content: "keep rejected direction", attachmentRefs });
    expect(rejected.queueState).toBeUndefined(); expect(rejected.queueMessageId).toBeUndefined();
    expect(state.conversationMessages[owner].find(message => message.id === realQueue.id)).toBe(realQueue);
    expect(state.conversationMessages[owner].find(message => message.id === live.id)).toBe(live);
    expect(state.conversationStreaming[owner]).toBe(true);
    expect(state).toMatchObject({ conversationId: "reader-b", draft: "keep B draft", isStreaming: false });
    expect(onRejected).toHaveBeenCalledOnce();
    expect(state.conversationMessages[owner].some(message => message.role === "system" && message.content.includes("expected turn already ended"))).toBe(true);
  });

  it("withdraws a rejected delivery's queue display when its definitive result arrives after the UI wait timed out", async () => {
    let deliveryId = "";
    useAppStore.setState({ conversationId: "conv-test", sideChats: {}, draft: "keep draft", isStreaming: true,
      messages: [{ id: "live-answer", turnId: "real-live-turn", role: "assistant", content: "working", artifacts: [], timestamp: 1, isStreaming: true }] });
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(async (command, _type, options) => {
      deliveryId = command.client_command_id!;
      options?.onReceived?.();
      throw new Error("result timeout after durable receipt");
    });
    expect(await sendChatMessage({ displayContent: "saved direction", allowWhileStreaming: true, busyBehavior: "steer", awaitAdmission: true,
      userMessageId: "late-user", assistantMessageId: "late-answer" })).toBe(false);
    expect(useAppStore.getState().messages.find(message => message.id === "late-user")?.queueState).toBe("queued");
    handleUserMessageAdmission({ type: "command.result", command: "user_message", client_command_id: deliveryId, level: "error",
      message: "The original turn is no longer active.", data: { conversation_id: "conv-test", admitted: false, queued: false,
        reason: "steer_target_unavailable", expected_turn_id: "real-live-turn", active_turn_id: "" } });
    const saved = useAppStore.getState().messages.find(message => message.id === "late-user")!;
    expect(saved).toMatchObject({ content: "saved direction" }); expect(saved.queueState).toBeUndefined();
    expect(useAppStore.getState()).toMatchObject({ draft: "keep draft", isStreaming: true });
  });

  it("restores structured plugin and browser context when recalling a user turn", async () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [{
        id: "user-context",
        role: "user",
        content: "inspect this page",
        backendContent: "Browser annotation: https://example.test/login\n\ninspect this page",
        contextRefs: [
          { kind: "plugin", name: "Docs", configName: "docs", path: "plugin://docs" },
          {
            kind: "browser_annotation",
            name: "Login button",
            path: "https://example.test/login",
            url: "https://example.test/login",
            note: "Button does nothing",
          },
          { kind: "skill", name: "browser-debug", path: "C:/skills/browser-debug/SKILL.md" },
        ],
        artifacts: [],
        timestamp: 1,
      }],
      selectedMentions: [],
      selectedSkills: [],
      isStreaming: false,
    });

    await useAppStore.getState().recallMessage("user-context");

    expect(useAppStore.getState().selectedMentions).toEqual([
      { kind: "plugin", name: "Docs", configName: "docs", path: "plugin://docs" },
      {
        kind: "browser_annotation",
        name: "Login button",
        path: "https://example.test/login",
        url: "https://example.test/login",
        note: "Button does nothing",
      },
    ]);
    expect(useAppStore.getState().selectedSkills).toEqual([
      { kind: "skill", name: "browser-debug", path: "C:/skills/browser-debug/SKILL.md" },
    ]);
    expect(useAppStore.getState().draft).toBe("inspect this page");
  });

  it("resumes an assistant message and seals it when streaming finishes", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
    });

    useAppStore.getState().resumeStreaming("conv-test", [
      { id: "tool-1", name: "recall", args: { query: "previous edits" } },
    ]);

    let resumed = useAppStore.getState().messages[0];
    expect(resumed?.isStreaming).toBe(true);

    useAppStore.getState().finishStreaming("conv-test");

    resumed = useAppStore.getState().messages[0];
    expect(resumed?.isStreaming).toBe(false);
    expect(resumed?.completedAt).toEqual(expect.any(Number));
  });

  it("tracks streaming state per conversation instead of only on the active thread", () => {
    useAppStore.setState({
      conversationId: "conv-a",
      messages: [],
      conversationMessages: {
        "conv-b": [{
          id: "assistant-b",
          role: "assistant",
          content: "",
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
        }],
      },
      conversationStreaming: {
        "conv-b": true,
      },
      isStreaming: false,
    });

    expect(useAppStore.getState().conversationStreaming["conv-b"]).toBe(true);
    expect(useAppStore.getState().conversationMessages["conv-b"]?.[0]?.isStreaming).toBe(true);
    expect(useAppStore.getState().isStreaming).toBe(false);
  });

  it("allows sending to an idle side conversation while the active conversation is streaming", () => {
    useAppStore.setState({
      conversationId: "conv-main",
      messages: [{
        id: "assistant-main",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      sideChats: {
        "side-1": {
          id: "side-1",
          messages: [],
          draft: "",
          isStreaming: false,
          inheritedContext: "",
        },
      },
      conversationMessages: {},
      conversationStreaming: { "conv-main": true },
      isStreaming: true,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "side question",
      backendContent: "side question",
      conversationId: "side-1",
      skipLocalAppend: true,
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "side question",
      conversation_id: "side-1",
    });
  });

  it("clears stale turn plan and todos when starting a new user turn", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      isConnected: true,
      isStreaming: false,
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      plan: {
        threadId: "conv-test",
        turnId: "turn-angry-birds",
        plan: [
          { step: "用单文件 HTML 实现愤怒的小鸟游戏", status: "in_progress" },
          { step: "验证游戏文件写入正确", status: "pending" },
        ],
      },
      todos: [
        {
          id: "todo-angry-birds",
          content: "正在编写愤怒的小鸟 HTML 游戏",
          activeForm: "正在编写愤怒的小鸟 HTML 游戏",
          status: "in_progress",
        },
      ],
      agentProgress: [{
        type: "progress",
        id: "plan:angry-birds-plan",
        stage: "planning",
        status: "running",
        message: "执行上一轮计划",
        timestamp: 1,
        conversationId: "conv-test",
      }],
      conversationAgentStates: {
        "conv-test": {
          plan: null,
          todos: [],
          subagents: [],
          agentProgress: [],
        },
      },
    });

    const ok = sendChatMessage({
      displayContent: "换个游戏吧 我要完cf",
      backendContent: "换个游戏吧 我要完cf",
    });

    expect(ok).toBe(true);
    expect(useAppStore.getState().plan?.turnId).toBe("turn-angry-birds");
    handleRuntimeEvent({ type: "agent.run.started", role: "main", conversation_id: "conv-test",
      message_id: (sent[0] as { assistant_message_id: string }).assistant_message_id, run_id: "next-real-run" });
    const state = useAppStore.getState();
    expect(state.plan).toBeNull();
    expect(state.todos).toEqual([]);
    expect(state.agentProgress).toEqual([]);
    expect(state.conversationAgentStates["conv-test"]).toMatchObject({
      plan: null,
      todos: [],
      agentProgress: [],
    });
    expect(state.messages.at(-2)).toMatchObject({
      role: "user",
      content: "换个游戏吧 我要完cf",
    });
  });

  it("allows sending to an idle side conversation while the active conversation has a local pending question", () => {
    useAppStore.setState({
      conversationId: "conv-main",
      pendingAskUser: {
        requestId: "ask-main",
        conversationId: "conv-main",
        question: "Continue main?",
      },
      sideChats: {
        "side-1": {
          id: "side-1",
          messages: [],
          draft: "",
          isStreaming: false,
          inheritedContext: "",
        },
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "side question while main waits",
      backendContent: "side question while main waits",
      conversationId: "side-1",
      skipLocalAppend: true,
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "side question while main waits",
      conversation_id: "side-1",
    });
  });

  it("does not block the current conversation with a local pending prompt from another conversation", () => {
    useAppStore.setState({
      conversationId: "conv-other",
      pendingAskUser: {
        requestId: "ask-main",
        conversationId: "conv-main",
        question: "Continue main?",
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "new active prompt",
      backendContent: "new active prompt",
      skipLocalAppend: true,
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "new active prompt",
      conversation_id: "conv-other",
    });
  });

  it("blocks explicit sends to the conversation that owns a local pending prompt", () => {
    useAppStore.setState({
      conversationId: "conv-other",
      pendingAskUser: {
        requestId: "ask-main",
        conversationId: "conv-main",
        question: "Continue main?",
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "new main prompt",
      backendContent: "new main prompt",
      conversationId: "conv-main",
      skipLocalAppend: true,
    });

    expect(ok).toBe(false);
    expect(sent).toHaveLength(0);
  });

  it("blocks sending when the target conversation has a restored pending user action", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      runtimeSession: {
        active_conversation_id: "conv-active",
        pending_approval_count: 1,
        pending_approvals: [{
          request_id: "ask-active",
          type: "ask_user",
          conversation_id: "conv-active",
        }],
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "new prompt",
      backendContent: "new prompt",
    });

    expect(ok).toBe(false);
    expect(sent).toHaveLength(0);
  });

  it("allows sending when only another conversation has a restored pending user action", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      runtimeSession: {
        active_conversation_id: "conv-active",
        pending_approval_count: 1,
        pending_approvals: [{
          request_id: "ask-other",
          type: "ask_user",
          conversation_id: "conv-other",
        }],
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "new prompt",
      backendContent: "new prompt",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "new prompt",
      conversation_id: "conv-active",
    });
  });

  it("includes the active conversation id on normal sends", () => {
    useAppStore.setState({
      conversationId: "conv-active-send",
      conversations: [{ id: "conv-active-send", title: "Active", updatedAt: "2026-06-06T00:00:00.000Z" }],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "continue",
      backendContent: "continue",
      skipLocalAppend: true,
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "continue",
      conversation_id: "conv-active-send",
    });
  });

  it("creates a streaming placeholder for an explicit background conversation", () => {
    useAppStore.setState({
      conversationId: "conv-a",
      messages: [{
        id: "assistant-a",
        role: "assistant",
        content: "active thread",
        artifacts: [],
        timestamp: 1,
      }],
      conversationMessages: {
        "conv-b": [],
      },
      conversationStreaming: {
        "conv-a": false,
        "conv-b": false,
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "background question",
      backendContent: "background question",
      conversationId: "conv-b",
    });

    const state = useAppStore.getState();
    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "background question",
      conversation_id: "conv-b",
    });
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].content).toBe("active thread");
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-b"]).toBe(true);
    expect(state.conversationMessages["conv-b"]).toEqual([
      expect.objectContaining({ role: "user", content: "background question" }),
      expect.objectContaining({ role: "assistant", content: "", isStreaming: true }),
    ]);
  });

  it("does not invent a terminal transition when a nonqueue caller sees a busy conversation", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [
        {
          id: "assistant-stale",
          role: "assistant",
          content: "Previous run failed.",
          artifacts: [],
          timestamp: 1,
          isStreaming: false,
        },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "continue",
      backendContent: "continue",
    });

    expect(ok).toBe(false);
    expect(sent).toHaveLength(0);
    expect(useAppStore.getState().messages.some((message) => message.content === "continue")).toBe(false);
  });

  it("allows a short repeated reply after the previous turn has finished", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-06-05T00:00:00.000Z"));
    useAppStore.setState({
      conversationId: "conv-repeat",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
    });

    expect(sendChatMessage({
      displayContent: "继续",
      backendContent: "继续",
    })).toBe(true);
    useAppStore.getState().finishStreaming("conv-repeat");

    vi.advanceTimersByTime(300);

    expect(sendChatMessage({
      displayContent: "继续",
      backendContent: "继续",
    })).toBe(true);
    expect(sent.filter((command) => (command as { content?: string }).content === "继续")).toHaveLength(2);
  });

  it("uses stored conversation permissions and sends the current agent mode", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      conversations: [{ id: "conv-test", title: "Project", updatedAt: "2026-06-06T00:00:00.000Z", workspaceRoot: "C:\\Desktop\\PDFTranslate" }],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
      permissionMode: "bypass",
      agentMode: "review",
      workingDirectory: "C:\\Desktop\\PDFTranslate",
    });

    const ok = sendChatMessage({
      displayContent: "write the README",
      backendContent: "write the README",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "write the README",
      agent_mode: "review",
      workspace_root: "C:\\Desktop\\PDFTranslate",
    });
    expect(sent[0]).not.toHaveProperty("permission_mode");
  });

  it("sends the active editor file as primary workspace grounding", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      conversations: [{ id: "conv-test", title: "Project", updatedAt: "2026-06-06T00:00:00.000Z", workspaceRoot: "C:\\Desktop\\MiniCode" }],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
      workingDirectory: "C:\\Desktop\\MiniCode",
      activeTabPath: "index.html",
    });

    const ok = sendChatMessage({
      displayContent: "看看我的文件",
      backendContent: "看看我的文件",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "看看我的文件",
      workspace_root: "C:\\Desktop\\MiniCode",
      primary_file: "index.html",
      active_tab_path: "index.html",
    });
  });

  it("materializes a real projectless owner and waits for its final canonical frame before the first global message", async () => {
    useAppStore.setState({
      conversationId: null,
      conversations: [],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
      permissionMode: "bypass",
      appMode: "cowork", draft: "old workspace draft", attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null,
      workingDirectory: "C:\\Desktop\\MiniCode",
      activeTabPath: "README.md",
    });
    const draftId = useAppStore.getState().startConversationDraft({ bindWorkspace: false, appMode: "cowork" });
    const oldWorkspaceKey = composerWorkspaceKey("C:\\Desktop\\MiniCode");
    expect(useAppStore.getState().workspaceComposerDrafts[oldWorkspaceKey].draft).toBe("old workspace draft");
    expect(useAppStore.getState().draft).toBe("");
    useAppStore.getState().setDraft("hello");
    expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: draftId, draft: "hello" });

    const submission = sendChatMessage({
      displayContent: "hello",
      backendContent: "hello",
    });

    expect(sent).toHaveLength(0);
    await vi.waitFor(() => expect(clientCommands.some(command => (command as ClientCommand).type === "conversation.switch")).toBe(true));
    const create = clientCommands.find(command => (command as ClientCommand).type === "conversation.create") as Extract<ClientCommand, { type: "conversation.create" }>;
    const switched = clientCommands.find(command => (command as ClientCommand).type === "conversation.switch") as Extract<ClientCommand, { type: "conversation.switch" }>;
    const owner = switched.conversation_id;
    expect(create).toMatchObject({ activate: false, conversation_id: owner, permission_mode: "bypass" });
    expect(create.workspace_root).toBeUndefined();
    expect(clientCommands.filter(command => ["conversation.create", "llm.model.set", "llm.config.set", "conversation.permission_mode.set", "conversation.switch"].includes((command as ClientCommand).type))
      .map(command => (command as ClientCommand).type)).toEqual(["conversation.create", "llm.model.set", "conversation.switch"]);
    expect(sent).toHaveLength(0);
    expect(wsMock.listeners.size).toBe(1);
    receiveCanonicalSwitch(switched, true);
    await Promise.resolve();
    expect(useAppStore.getState().conversationId).toBe(owner);
    expect(useAppStore.getState().conversationDrafts[draftId].materializing).toBe(true);
    expect(sent).toHaveLength(0);
    receiveCanonicalSwitch(switched, false);
    expect(await submission).toBe(true);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "hello",
      conversation_id: owner, workspace_root: generatedCwd,
    });
    expect(sent[0]).not.toHaveProperty("permission_mode");
    expect(sent[0]).not.toHaveProperty("primary_file");
    expect(useAppStore.getState().workingDirectory).toBe(generatedCwd);
    expect(useAppStore.getState().draft).toBe("hello");
    expect(useAppStore.getState().conversationWorkbenchStates[owner].draft).toBe("hello");
    expect(useAppStore.getState().workspaceComposerDrafts[oldWorkspaceKey].draft).toBe("old workspace draft");
    expect(vi.mocked(sendClientCommandAwaitResult).mock.calls.some(([command]) => command.type === "conversation.switch")).toBe(false);
    expect(wsMock.listeners.size).toBe(0);
    receiveCanonicalSwitch(switched, false);
    expect(sent).toHaveLength(1);
  });

  it("marks the local streaming turn failed when websocket send throws", () => {
    wsMock.throwOnSend = true;
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "hello",
      backendContent: "hello",
    });

    const state = useAppStore.getState();
    expect(ok).toBe(false);
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-test"]).toBe(false);
    expect(state.messages.some((message) => message.role === "system" && /socket closed/i.test(message.content))).toBe(true);
  });

  it("preserves pasted-text semantics when an attachment-only message is recalled", async () => {
    useAppStore.getState().sendMessage("", {
      assistant: false,
      attachmentRefs: [{
        id: "artifact-paste-recall",
        name: "pasted-9.txt",
        kind: "document",
        mediaType: "text/plain",
        artifactId: "artifact-paste-recall",
        sizeBytes: 30_000,
        inputSource: "pasted_text",
        sourceCharCount: 30_000,
      }],
    });

    const userMessage = useAppStore.getState().messages[0];
    await useAppStore.getState().recallMessage(userMessage.id);

    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().attachments[0]).toMatchObject({
      inputSource: "pasted_text",
      sourceCharCount: 30_000,
      attachment: {
        input_source: "pasted_text",
        source_char_count: 30_000,
      },
    });
  });

  it("treats a rejected websocket send as a failed local turn", () => {
    wsMock.acceptSend = false;

    const ok = sendChatMessage({
      displayContent: "rejected transport",
      backendContent: "rejected transport",
    });

    const state = useAppStore.getState();
    expect(ok).toBe(false);
    expect(state.isStreaming).toBe(false);
    expect(state.messages.some((message) => message.role === "system" && /后端连接尚未就绪/.test(message.content))).toBe(true);
  });

  it("preserves another thread's permission policy and the selected main panel", () => {
    useAppStore.setState({
      conversationId: "main-owner", conversations: [], isConnected: true,
      permissionMode: "bypass", rightStackTabLocked: true, runtimeSession: null,
      sideChats: { "side-owner": { id: "side-owner", draft: "", messages: [], isStreaming: false } },
    });
    const ok = sendChatMessage({ conversationId: "side-owner", backendContent: "side request", skipLocalAppend: true });
    expect(ok).toBe(true);
    expect(sent.at(-1)).not.toHaveProperty("permission_mode");
    expect(sent.at(-1)).not.toHaveProperty("workspace_root");
    expect(useAppStore.getState().rightStackTabLocked).toBe(true);
  });

  it("keeps a side-chat send failure in the side-chat transcript", () => {
    wsMock.throwOnSend = true;
    useAppStore.setState({
      conversationId: "conv-main",
      messages: [{ id: "main-message", role: "user", content: "main", artifacts: [], timestamp: 1 }],
      conversationMessages: { "side-1": [] },
      conversationStreaming: { "side-1": false },
      sideChats: { "side-1": { id: "side-1", messages: [], isStreaming: false, draft: "" } },
      isStreaming: false,
      isConnected: true,
    });

    expect(sendChatMessage({
      displayContent: "side question",
      backendContent: "side question",
      conversationId: "side-1",
    })).toBe(false);

    const state = useAppStore.getState();
    expect(state.messages.some((message) => message.role === "system")).toBe(false);
    expect(state.sideChats["side-1"]?.messages.some((message) => (
      message.role === "system" && /socket closed/i.test(message.content)
    ))).toBe(true);
  });

  it("hands explicit messages to the websocket queue while reconnecting", () => {
    useAppStore.setState({ isConnected: false });

    const ok = sendChatMessage({
      displayContent: "continue when connected",
      backendContent: "continue when connected",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "continue when connected",
    });
  });

  it("rolls back a failed queued send without stopping the current turn", () => {
    wsMock.throwOnSend = true;
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [{
        id: "assistant-running",
        role: "assistant",
        content: "Current answer",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "queue this",
      backendContent: "queue this",
      allowWhileStreaming: true,
    });

    const state = useAppStore.getState();
    expect(ok).toBe(false);
    expect(state.isStreaming).toBe(true);
    expect(state.conversationStreaming["conv-test"]).toBe(true);
    expect(state.messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "assistant-running", isStreaming: true }),
    ]));
    expect(state.messages.some((message) => message.content === "queue this")).toBe(false);
  });

  it("interrupt clears message-level streaming state immediately", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [
        {
          id: "assistant-running",
          role: "assistant",
          content: "",
          artifacts: [],
          blocks: [{
            type: "progress",
            id: "agent:model:1",
            stage: "planning",
            status: "running",
            message: "Choosing the next step",
            timestamp: 1,
          }],
          timestamp: 1,
          isStreaming: true,
          isThinkingStreaming: true,
        },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    useAppStore.getState().interrupt();

    const state = useAppStore.getState();
    const assistant = state.messages[0];
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-test"]).toBe(false);
    expect(assistant.isStreaming).toBe(false);
    expect(assistant.isThinkingStreaming).toBe(false);
    expect(assistant.blocks?.[0]).toMatchObject({
      status: "partial",
      label: "已中断",
      summary: "用户已中断",
    });
  });

  it("finishStreaming clears thinking-only assistant state for targeted done events", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [
        {
          id: "assistant-thinking-only",
          role: "assistant",
          content: "Hi!",
          artifacts: [],
          blocks: [
            {
              type: "thinking",
              source: "provider",
              content: "正在整理回复",
            },
            {
              type: "text",
              content: "Hi!",
              source: "model_final",
              visibility: "final",
              phase: "final",
            },
          ],
          timestamp: 1,
          isStreaming: false,
          isThinkingStreaming: true,
        },
      ],
      conversationMessages: {
        "conv-test": [
          {
            id: "assistant-thinking-only",
            role: "assistant",
            content: "Hi!",
            artifacts: [],
            blocks: [
              {
                type: "thinking",
                source: "provider",
                content: "正在整理回复",
              },
              {
                type: "text",
                content: "Hi!",
                source: "model_final",
                visibility: "final",
                phase: "final",
              },
            ],
            timestamp: 1,
            isStreaming: false,
            isThinkingStreaming: true,
          },
        ],
      },
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    useAppStore.getState().finishStreaming("conv-test", undefined, "completed", "assistant-thinking-only");

    const state = useAppStore.getState();
    const assistant = state.messages[0];
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-test"]).toBe(false);
    expect(assistant.isStreaming).toBe(false);
    expect(assistant.isThinkingStreaming).toBe(false);
    expect(assistant.terminalStatus).toBe("completed");
    expect(assistant.blocks).toEqual([
      expect.objectContaining({ type: "text", content: "Hi!" }),
    ]);
  });

  it("finishStreaming retains a provider reasoning summary", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [{
        id: "assistant-summary",
        role: "assistant",
        content: "Done.",
        artifacts: [],
        blocks: [
          {
            type: "thinking",
            source: "provider",
            providerReasoningType: "reasoning_summary_text",
            content: "Checked the implementation.",
          },
          { type: "text", content: "Done.", source: "model_final" },
        ],
        timestamp: 1,
        isStreaming: true,
        isThinkingStreaming: true,
      }],
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    useAppStore.getState().finishStreaming("conv-test", undefined, "completed", "assistant-summary");

    expect(useAppStore.getState().messages[0]?.blocks).toEqual([
      expect.objectContaining({
        type: "thinking",
        content: "Checked the implementation.",
        providerReasoningType: "reasoning_summary_text",
      }),
      expect.objectContaining({ type: "text", content: "Done." }),
    ]);
  });

  it("marks unfinished tools cancelled when the user interrupts a turn", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [{
        id: "assistant-running-tool",
        role: "assistant",
        content: "",
        artifacts: [],
        blocks: [{
          type: "tool_call",
          record: {
            id: "tool-running",
            name: "run_command",
            args: {},
            status: "running",
            startedAt: 1,
          },
        }],
        timestamp: 1,
        isStreaming: true,
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    useAppStore.getState().finishStreaming("conv-test", undefined, "interrupted", "assistant-running-tool");

    const block = useAppStore.getState().messages[0].blocks?.[0];
    expect(block?.type).toBe("tool_call");
    if (block?.type === "tool_call") expect(block.record.status).toBe("cancelled");
  });

  it("retries atomically while streaming without queueing behind the old run", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [
        { id: "user-retry", role: "user", content: "try again", artifacts: [], timestamp: 1 },
        { id: "assistant-old", role: "assistant", content: "old", artifacts: [], timestamp: 2 },
        { id: "assistant-running", role: "assistant", content: "newer", artifacts: [], timestamp: 3, isStreaming: true },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      pendingApproval: { requestId: "approval-old", conversationId: "conv-test", toolName: "run_command", args: {} },
    });

    const ok = sendChatMessage({
      displayContent: "try again",
      retryFromMessageId: "user-retry",
    });

    expect(ok).toBe(true);
    expect(sent).toEqual([expect.objectContaining({
      type: "user_message",
      content: "try again",
      conversation_id: "conv-test",
      retry_from_message_id: "user-retry",
    })]);
    expect(sent[0]).not.toHaveProperty("queue_if_busy");
    expect(useAppStore.getState().messages.map((message) => message.content)).toEqual(["try again", "old", "newer"]);
    handleUserMessageAdmission({ type: "command.result", command: "user_message", level: "success", message: "admitted",
      client_command_id: (sent[0] as { client_command_id: string }).client_command_id,
      data: { admitted: true, conversation_id: "conv-test" } });
    expect(useAppStore.getState().messages.map((message) => message.content)).toEqual(["try again"]);
    expect(useAppStore.getState().isStreaming).toBe(false);
  });

  it("keeps paused queued messages and attachment identities when a retry is admitted without another queue event", () => {
    const attachmentRefs = [{ id: "image-1", name: "queue.png", kind: "image" as const,
      mediaType: "image/png", artifactId: "uploaded-image" }];
    const queued = [
      { id: "q1-user", role: "user" as const, content: "queued one", artifacts: [], timestamp: 3,
        attachmentRefs, queueState: "queued" as const, queueMessageId: "q1-answer", queuePosition: 1, queuePaused: true },
      { id: "q1-answer", role: "assistant" as const, content: "", artifacts: [], timestamp: 3,
        queueState: "queued" as const, queueMessageId: "q1-answer", queuePosition: 1, queuePaused: true },
      { id: "q2-user", role: "user" as const, content: "queued two", artifacts: [], timestamp: 4,
        queueState: "queued" as const, queueMessageId: "q2-answer", queuePosition: 2, queuePaused: true },
      { id: "q2-answer", role: "assistant" as const, content: "", artifacts: [], timestamp: 4,
        queueState: "queued" as const, queueMessageId: "q2-answer", queuePosition: 2, queuePaused: true },
    ];
    useAppStore.setState({ conversationId: "conv-test", draft: "unsent draft", messages: [
      { id: "user-retry", role: "user", content: "try again", artifacts: [], timestamp: 1 },
      { id: "assistant-old", role: "assistant", content: "old", artifacts: [], timestamp: 2 },
      ...queued,
    ], conversationMessages: {}, conversationStreaming: {}, isStreaming: false, isConnected: true });
    expect(sendChatMessage({ displayContent: "try again", retryFromMessageId: "user-retry" })).toBe(true);
    handleUserMessageAdmission({ type: "command.result", command: "user_message", level: "success", message: "admitted",
      client_command_id: (sent[0] as { client_command_id: string }).client_command_id,
      data: { admitted: true, conversation_id: "conv-test" } });
    expect(useAppStore.getState().messages.filter(message => message.queueState === "queued")).toEqual(queued);
    expect(useAppStore.getState().draft).toBe("unsent draft");
    expect(useAppStore.getState().conversationMessages["conv-test"].filter(message => message.queueState === "queued")).toEqual(queued);
  });

  it("does not rewind the local transcript when an atomic retry cannot be sent", () => {
    const originalMessages = [
      { id: "user-retry", role: "user" as const, content: "try again", artifacts: [], timestamp: 1 },
      { id: "assistant-old", role: "assistant" as const, content: "old", artifacts: [], timestamp: 2 },
    ];
    useAppStore.setState({ messages: originalMessages, isStreaming: false });
    wsMock.acceptSend = false;

    expect(sendChatMessage({
      displayContent: "try again",
      retryFromMessageId: "user-retry",
    })).toBe(false);

    expect(useAppStore.getState().messages.slice(0, originalMessages.length)).toEqual(originalMessages);
    expect(useAppStore.getState().messages.at(-1)).toEqual(expect.objectContaining({
      role: "system",
      content: "错误：后端连接尚未就绪，请稍后重试。",
    }));
  });

});
