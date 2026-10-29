/* @vitest-environment jsdom */
import { File as NativeFile } from "node:buffer";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
// These cases exercise the supported literal-input mode. Rich editing has its own document/IME suite.
beforeEach(() => useAppStore.setState({ composerPlainTextMode: true }));
vi.mock("./RichComposerEditor", async () => ({ RichComposerEditor: (await import("../../test/literal-composer-editor")).LiteralComposerEditor }));

import { emptyComposerDraft, flushComposerDrafts, loadComposerDrafts, resetComposerDraftStorageForTests } from "../stores/composer-drafts";
import { registerWebSocketSender, resetPendingCommandResultsForTests } from "../protocol/ws-outbox";
import { resetPendingUserMessagesForTests, sendChatMessage } from "../chat/sendChatMessage";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import { applyUserMessageQueueUpdate, handleSessionEvent } from "../chat/sessionEvents";
import { handleChatStreamEvent } from "../chat/chatStreamEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { handleNoticeEvent } from "../chat/noticeEvents";
import { Composer } from "./Composer";
import { MessageList } from "../chat/MessageList";
import { SideChatPanel } from "../panels/SideChatPanel";
import { buildContextNativeAttachments } from "./contextPayload";
import type { ClientCommand, CommandResultEvent, InterruptCommand, ServerEvent, UserMessageCommand, UserMessageQueueUpdatedEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { formatDiffCommentsPrompt, type DiffComment } from "../lib/diff-comments";
import { clearDiffCommentScope, getDiffCommentScope, readDiffCommentState, setDiffComments } from "../lib/diff-comment-storage";
import { hydrateMessages } from "../chat/transcriptHydration";
import { createCodeReview } from "../protocol/code-review";
import { enterComposerGoalMode } from "./goal-state";
import { restoreRetainedComposerInput } from "./retainedComposerInputs";

const wire = vi.hoisted(() => ({ commands: [] as UserMessageCommand[], interrupts: [] as InterruptCommand[], resources: [] as ClientCommand[], heldSwitchOwner: null as string | null, holdCreation: false, accepted: true, toast: vi.fn(), listeners: new Set<(data: unknown) => void>() }));
vi.hoisted(() => Object.defineProperty(window, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "draft-admission-session", send: (command: ClientCommand) => {
  if (command.type === "user_message") wire.commands.push(command);
  return wire.accepted;
}, subscribe: (listener: (data: unknown) => void) => { wire.listeners.add(listener); return () => wire.listeners.delete(listener); } }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: wire.toast }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  fetchWithTimeout: vi.fn(async () => new Response('{"plugins":[]}', { status: 200 })),
  fetchDraftModelSelection: vi.fn(async (root: string) => ({ provider: "custom", current_model: "fixture-model", model: "fixture-model",
    available_models: ["fixture-model"], models_source: "configured", effective_reasoning_effort: "medium",
    available_permission_modes: ["confirm", "auto", "bypass"], config_version: "fixture", config_cwd: root,
    feature_flags: {}, composer_commands: [], default_model_selection: { provider: "custom", model: "fixture-model", reasoning_effort: "medium", conversation_id: "", working_directory: root } })),
}));
let acknowledgeClientCommand: typeof import("../hooks/useWebSocket")["acknowledgeClientCommand"];
beforeAll(async () => {
  ({ acknowledgeClientCommand } = await vi.importActual<typeof import("../hooks/useWebSocket")>("../hooks/useWebSocket"));
});
vi.mock("./contextPayload", () => ({
  buildContextPayload: vi.fn(async () => ""),
  buildContextNativeAttachments: vi.fn(async () => ({ attachments: [], attachmentRefs: [], notes: "" })),
}));
vi.mock("../protocol/code-review", () => ({
  fetchCodeReviewOptions: vi.fn(async (_owner: string, cwd: string) => ({ cwd, gitRoot: cwd, currentBranch: "feature", branches: ["main"] })),
  createCodeReview: vi.fn(async (owner: string, cwd: string, mode: "uncommitted" | "base-branch", baseBranch: string | undefined, _sha: string | undefined, requestMessage: string) => ({
    delivery: "inline", reviewThreadId: owner, sourceConversationId: owner,
    scope: { mode, cwd, gitRoot: cwd, ...(baseBranch ? { baseBranch, mergeBaseSha: "abcdef1234" } : {}) },
    prompt: `## Code review guidelines:\nFull installed review guidelines\n## My request:\n${requestMessage}`,
    requestMessage, diff: "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
  })),
}));

const root = "C:/owned-draft-admission";
const submit = () => fireEvent.click(screen.getByRole("button", { name: /^(发送|连接恢复后发送|将消息加入队列)$/ }));
const receipt = (command: UserMessageCommand, data: Record<string, unknown>, level = "success"): CommandResultEvent => ({
  type: "command.result", command: "user_message", client_command_id: command.client_command_id,
  level, message: level === "error" ? "真实准入拒绝" : "",
  data: { conversation_id: command.conversation_id || "first-owner", user_message_id: command.user_message_id,
    assistant_message_id: command.assistant_message_id, ...data },
});
const nativeReceipt = (command: UserMessageCommand, completion: "completed" | "refused" | "unknown", level = "success") =>
  receipt(command, { admitted: false, native_completed: completion === "completed", native_completion: completion,
    slash_command: command.display_content?.split(/\s/, 1)[0], command_results: [] }, level);
const fileDraft = () => {
  const file = new File(["中文原文件\r\n🙂 and original bytes"], "中文 空格文件.txt", { type: "text/plain", lastModified: 12345 });
  const attachment = { id: "original-file", name: file.name, type: file.type, size: file.size, status: "ready" as const,
    localFile: file, conversationId: "main", artifactId: "artifact-original", attachment: {
      id: "artifact-original", artifact_id: "artifact-original", file_name: file.name, media_type: file.type, kind: "document",
    } };
  return { file, attachment };
};
const coldRecords = async () => {
  await flushComposerDrafts();
  await resetComposerDraftStorageForTests(true);
  return loadComposerDrafts();
};
const buffer: StreamBuffer = {
  push: (chunk, owner, item, _metadata, message) => useAppStore.getState().appendAgentMessageDelta(item || "answer", chunk, owner, message),
  flush() {}, destroy() {},
};
const stream = (event: ServerEvent, owner = "main") => handleChatStreamEvent(event, owner, { textStreamBuffer: buffer, thinkingStreamBuffer: buffer });
const itemFrames = (command: UserMessageCommand, owner = "main") => {
  stream({ type: "item.started", conversation_id: owner, message_id: command.assistant_message_id,
    item: { id: "answer", type: "agent_message", text: "", status: "in_progress" } } as ServerEvent, owner);
  stream({ type: "agent_message.delta", conversation_id: owner, message_id: command.assistant_message_id,
    item_id: "answer", delta: "real answer" } as ServerEvent, owner);
};
const started = (command: UserMessageCommand, owner = "main") => handleRuntimeEvent({ type: "agent.run.started", role: "main",
  conversation_id: owner, message_id: command.assistant_message_id, run_id: `run-${command.assistant_message_id}`,
  turn_id: `run-${command.assistant_message_id}` } as ServerEvent);
const queueFact = (command: UserMessageCommand, status: UserMessageQueueUpdatedEvent["status"], extra = {}): UserMessageQueueUpdatedEvent => ({
  type: "user_message.queue.updated", status, conversation_id: "main", message_id: command.assistant_message_id!,
  user_message_id: command.user_message_id, position: 1, ...extra,
});

beforeEach(async () => {
  await waitComposerDraftRecovery();
  vi.stubGlobal("File", NativeFile);
  wire.commands = []; wire.interrupts = []; wire.resources = []; wire.heldSwitchOwner = null; wire.holdCreation = false; wire.accepted = true; wire.toast.mockClear(); resetPendingUserMessagesForTests();
  wire.listeners.clear();
  clearDiffCommentScope("main"); clearDiffCommentScope("other"); clearDiffCommentScope(getDiffCommentScope(null, root));
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: "main", workingDirectory: root,
    conversations: [{ id: "main", title: "owned", workspaceRoot: root }], messages: [], conversationMessages: {}, preparingComposerMessages: {},
    conversationStreaming: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, retainedComposerInputs: {}, sideChats: {},
    sideChatPendingContext: null, isConnected: true, isStreaming: false, runtimeSession: null, runtimeCapabilities: null,
    currentProvider: "custom", currentModel: "fixture-model", availableModels: ["fixture-model"],
    availableSkills: [], slashCommands: [], selectedMentions: [], selectedSkills: [], pendingPermissionModeChanges: {},
    pendingConversationSwitchId: null, pendingConversationCreateId: null, pendingApproval: null, approvalQueue: [],
    pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], turnDiffs: {},
    plan: null, todos: [], subagents: [], agentProgress: [], conversationAgentStates: {},
    slashPanelOpen: false, mentionPanelOpen: false, sendShortcut: "enter", followUpBehavior: "queue" });
  registerWebSocketSender(command => {
    if (command.type === "user_message") wire.commands.push(command);
    if (command.type === "interrupt") wire.interrupts.push(command);
    const resourceCommand = ["conversation.create", "llm.model.set", "llm.config.set", "conversation.permission_mode.set", "conversation.agent_mode.set", "conversation.switch"].includes(command.type);
    if (resourceCommand) {
      wire.resources.push(command);
      if (command.type === "conversation.create" && wire.holdCreation) return wire.accepted;
      const owner = "conversation_id" in command ? command.conversation_id : undefined;
      if (command.type === "conversation.switch" && owner === wire.heldSwitchOwner) return wire.accepted;
      if (command.type === "conversation.switch") {
        for (const hydrating of [true, false]) {
          const event = { type: "conversation.switched", conversation_id: owner, is_hydrating: hydrating, context_pending: hydrating,
            client_command_id: command.client_command_id, client_command_type: command.type,
            conversation: { id: owner, title: "first created owner", workspace_root: root, execution_cwd: root, messages: [] },
            session: { active_conversation_id: owner, workspace_root: root, active_stream_conversation_ids: [] },
          } as ServerEvent;
          handleSessionEvent(event, { textStreamBuffer: buffer, thinkingStreamBuffer: buffer });
          for (const listener of [...wire.listeners]) listener(event);
        }
        return wire.accepted;
      }
      handleCommandResultEvent({ type: "command.result", command: command.type === "llm.model.set" ? "model.set"
        : command.type === "llm.config.set" ? "effort" : command.type,
        client_command_id: command.client_command_id, client_command_type: command.type, level: "success", message: "",
        data: { conversation_id: owner, execution_cwd: root, ...(command.type === "conversation.create" ? { created: true, activated: false } : {}) } });
    }
    return wire.accepted;
  });
});
afterEach(async () => {
  cleanup();
  useAppStore.setState({ isConnected: false });
  await act(async () => resetPendingCommandResultsForTests());
  registerWebSocketSender(null);
  await resetComposerDraftStorageForTests();
  vi.restoreAllMocks(); vi.unstubAllGlobals(); delete window.__MINICODE_RUNTIME__;
});

describe("actual composer durable admission boundary", () => {
  const comment: DiffComment = { type: "comment", content: [{ content_type: "text", text: "保留这个真实范围\n再改这里" }],
    position: { path: "src/app.ts", side: "right", line: 103, start_line: 100 },
    localDiffHunk: "@@ -50,2 +100,4 @@\n-old\n+new", localWorkspaceRoot: root };
  const secondComment: DiffComment = { ...comment, position: { path: "src/other.ts", side: "left", line: 7 },
    content: [{ content_type: "text", text: "发送期间新写的评论" }] };

  it("sends an ordinary authored input when a saved goal mode is disabled in the current workspace", async () => {
    const goal = { text: "Keep this saved goal", status: "active" };
    useAppStore.setState({ draft: "实际普通消息", goalMode: true, activeGoal: goal,
      runtimeCapabilities: { feature_flags: { goals: { enabled: false } } } as never });
    render(<Composer />);
    expect(document.querySelector(".composer-container")?.getAttribute("data-goal-mode")).toBe("false");
    expect(screen.queryByRole("button", { name: "编辑目标" })).toBeNull();
    submit();
    await waitFor(() => expect(wire.commands, JSON.stringify(wire.toast.mock.calls)).toHaveLength(1));
    const command = wire.commands[0];
    expect(command).toMatchObject({ type: "user_message", conversation_id: "main", content: "实际普通消息" });
    expect(useAppStore.getState()).toMatchObject({ activeGoal: goal, draft: "" });
    act(() => acknowledgeClientCommand({ type: "client.command.ack", client_command_id: command.client_command_id,
      command_type: "user_message" } as ServerEvent));
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: true })));
    expect(useAppStore.getState()).toMatchObject({ activeGoal: goal, draft: "" });
  });

  it("previews a single count pill without modifying input, and removes only the comment context", async () => {
    useAppStore.setState({ draft: "原输入不改" }); setDiffComments("main", [comment]);
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "查看 1 条评论" }));
    expect(screen.getByRole("region", { name: "Diff 评论预览" }).textContent).toContain("100-103");
    expect(screen.getByText("src/app.ts")).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("原输入不改");
    fireEvent.click(screen.getByRole("button", { name: "移除所有 Diff 评论" }));
    expect(readDiffCommentState("main").comments).toEqual([]);
    expect(useAppStore.getState().draft).toBe("原输入不改");
    expect(wire.commands).toEqual([]);
    act(() => setDiffComments("main", [comment]));
    expect(screen.queryByRole("region", { name: "Diff 评论预览" })).toBeNull();
  });

  it.each(["idle", "queue", "steer"] as const)("sends comments-only through normal %s input and restores exact durable metadata", async mode => {
    useAppStore.setState({ draft: "", isStreaming: mode !== "idle", followUpBehavior: mode === "steer" ? "steer" : "queue",
      messages: mode === "idle" ? [] : [{ id: "running", turnId: "running-turn", role: "assistant", content: "running", artifacts: [], timestamp: 1, isStreaming: true }] });
    setDiffComments("main", [comment]);
    render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command.content).toBe(formatDiffCommentsPrompt("", [comment]));
    expect(command.display_content).toBe("");
    expect(command.context_refs).toEqual([{ kind: "diff_comment", name: "src/app.ts", path: "src/app.ts", comment }]);
    if (mode !== "idle") expect(command.streaming_behavior).toBe(mode === "steer" ? "steer" : "follow_up");
    if (mode === "steer") expect(command.expected_turn_id).toBe("running-turn");
    expect(readDiffCommentState("main").comments).toEqual([]);
    act(() => acknowledgeClientCommand({ type: "client.command.ack", client_command_id: command.client_command_id,
      command_type: "user_message" } as ServerEvent));
    expect(readDiffCommentState("main").comments).toEqual([]);
    await act(async () => handleCommandResultEvent(receipt(command, mode === "idle" ? { admitted: true } : { queued: true, admitted: false })));
    const restored = hydrateMessages([{ id: command.user_message_id, role: "user", content: command.content,
      display_content: command.display_content, context_refs: JSON.parse(JSON.stringify(command.context_refs)) }])[0];
    expect(restored.content).toBe(""); expect(restored.backendContent).toBe(command.content);
    expect(restored.contextRefs).toEqual(command.context_refs);
    expect(readDiffCommentState("main").comments).toEqual([]);
  });

  it.each(["untouched", "new-comment", "typed-then-cleared"] as const)("restores a rejected submitted draft only while its claim is %s", async edit => {
    useAppStore.setState({ draft: "原始问题" }); setDiffComments("main", [comment]);
    render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    act(() => acknowledgeClientCommand({ type: "client.command.ack", client_command_id: command.client_command_id,
      command_type: "user_message" } as ServerEvent));
    expect(useAppStore.getState().draft).toBe("");
    if (edit === "new-comment") act(() => setDiffComments("main", [secondComment]));
    if (edit === "typed-then-cleared") act(() => { useAppStore.getState().setDraft("用户新写后撤销的输入"); useAppStore.getState().setDraft(""); });
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: false }, "error")));
    await waitFor(() => expect(useAppStore.getState().draft).toBe(edit === "untouched" ? "原始问题" : ""));
    expect(readDiffCommentState("main").comments).toEqual(edit === "untouched" ? [comment] : edit === "new-comment" ? [secondComment] : []);
  });

  it("keeps comments when preparation fails before local receipt and does not dispatch", async () => {
    useAppStore.setState({ draft: "尚未接收" }); setDiffComments("main", [comment]);
    vi.mocked(buildContextNativeAttachments).mockRejectedValueOnce(new Error("actual context preparation rejection"));
    render(<Composer />); submit();
    await waitFor(() => expect(wire.toast).toHaveBeenCalledWith("actual context preparation rejection", "error", 4500));
    expect(wire.commands).toEqual([]); expect(useAppStore.getState().draft).toBe("尚未接收");
    expect(readDiffCommentState("main").comments).toEqual([comment]);
  });

  it.each(["enter", "button"] as const)("%s releases an existing composer and shows its captured message while context is preparing", async method => {
    const mention = { kind: "file" as const, name: "plan.pdf", path: "plan.pdf", workspaceRoot: root };
    useAppStore.setState({ draft: "first input", selectedMentions: [mention] });
    let finish!: (value: Awaited<ReturnType<typeof buildContextNativeAttachments>>) => void;
    vi.mocked(buildContextNativeAttachments).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<><MessageList /><Composer /></>);
    const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    if (method === "enter") fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    else submit();
    expect(input.value).toBe("");
    expect(view.container.querySelector(".user-cell-bubble")?.textContent).toBe("first input");
    expect(wire.commands).toHaveLength(0);
    fireEvent.change(input, { target: { value: "next unsent input" } });
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await act(async () => finish({ attachments: [], attachmentRefs: [], notes: "", conversationId: "main" }));
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    expect(wire.commands[0]).toMatchObject({ display_content: "first input", context_refs: [mention] });
    expect(useAppStore.getState().draft).toBe("next unsent input");
    expect(view.container.querySelectorAll(".user-cell-bubble")).toHaveLength(1);
    await act(async () => handleCommandResultEvent(receipt(wire.commands[0], { admitted: true })));
    expect(useAppStore.getState().draft).toBe("next unsent input");
  });

  it.each([false, true])("a context preparation failure preserves the submitted input and newer draft=%s", async newer => {
    useAppStore.setState({ draft: "original input" });
    setDiffComments("main", [comment]);
    let fail!: (reason: Error) => void;
    vi.mocked(buildContextNativeAttachments).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    const view = render(<><MessageList /><Composer /></>); submit();
    const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    expect(input.value).toBe("");
    if (newer) {
      fireEvent.change(input, { target: { value: "newer draft" } });
      act(() => setDiffComments("main", [secondComment]));
    }
    await waitFor(() => expect(fail).toBeTypeOf("function"));
    await act(async () => fail(new Error("context could not be prepared")));
    await waitFor(() => expect(wire.toast).toHaveBeenCalledWith("context could not be prepared", "error", 4500));
    expect(wire.commands).toEqual([]);
    expect(useAppStore.getState().draft).toBe(newer ? "newer draft" : "original input");
    if (newer) {
      expect(useAppStore.getState().messages).toEqual([expect.objectContaining({ content: "original input", deliveryStatus: "failed",
        contextRefs: [expect.objectContaining({ kind: "diff_comment", comment })] })]);
      expect(view.container.querySelector(".user-cell-bubble")?.textContent).toBe("original input");
      expect(readDiffCommentState("main").comments).toEqual([secondComment]);
    } else {
      expect(view.container.querySelector(".user-cell-bubble")).toBeNull();
      expect(readDiffCommentState("main").comments).toEqual([comment]);
    }
  });

  it("hands off original file bytes before clearing the composer and keeps its preparing preview alive", async () => {
    const create = vi.fn(() => "blob:owned-submission"), revoke = vi.fn();
    vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
    const file = new File([new Uint8Array([0, 128, 255])], "original.png", { type: "image/png" });
    useAppStore.setState({ draft: "original image input", attachments: [{ id: "original-image", name: file.name, type: file.type,
      size: file.size, status: "ready", localFile: file, dataUrl: "blob:editor-preview", conversationId: "main", artifactId: "image-artifact",
      attachment: { id: "original-image", artifact_id: "image-artifact", file_name: file.name, media_type: file.type, kind: "image" } }] });
    let finish!: (value: Awaited<ReturnType<typeof buildContextNativeAttachments>>) => void;
    vi.mocked(buildContextNativeAttachments).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<><MessageList /><Composer /></>); submit();
    expect((screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement).value).toBe("");
    expect(view.container.querySelector(".user-cell-attachment-image img")?.getAttribute("src")).toBe("blob:owned-submission");
    expect(revoke).toHaveBeenCalledWith("blob:editor-preview");
    expect(revoke).not.toHaveBeenCalledWith("blob:owned-submission");
    act(() => useAppStore.getState().setDraft("newer draft"));
    await flushComposerDrafts();
    const saved = await loadComposerDrafts();
    const pending = saved.find(record => record.retainedInput?.unconfirmed)!;
    expect(new Uint8Array(await pending.draft.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([0, 128, 255]));
    expect(saved.find(record => !record.retainedInput && record.owner.id === "main")?.draft.draft).toBe("newer draft");
    expect(wire.commands).toEqual([]);
    await act(async () => finish({ attachments: [], attachmentRefs: [], notes: "" }));
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    await act(async () => handleCommandResultEvent(receipt(wire.commands[0], { admitted: true })));
    expect(useAppStore.getState().retainedComposerInputs).toEqual({});
    expect(useAppStore.getState().draft).toBe("newer draft");
    expect(revoke).toHaveBeenCalledWith("blob:owned-submission");
  });

  it("restores a preparation failure to its original hidden conversation after the user switches", async () => {
    useAppStore.setState({ draft: "original hidden input", conversations: [
      { id: "main", title: "main", workspaceRoot: root }, { id: "other", title: "other", workspaceRoot: root },
    ] });
    let fail!: (reason: Error) => void;
    vi.mocked(buildContextNativeAttachments).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    render(<><MessageList /><Composer /></>); submit();
    await waitFor(() => expect(fail).toBeTypeOf("function"));
    act(() => {
      useAppStore.getState().applyConversationSwitched({ conversationId: "other" });
      useAppStore.getState().setDraft("other unsent input");
    });
    expect(document.querySelector(".user-cell-bubble")).toBeNull();
    await act(async () => fail(new Error("context preparation failed")));
    expect(useAppStore.getState().draft).toBe("other unsent input");
    expect(useAppStore.getState().conversationWorkbenchStates.main.draft).toBe("original hidden input");
    expect(useAppStore.getState().preparingComposerMessages).toEqual({});
  });

  it("keeps an edited queued message in its editor while its context is being prepared", async () => {
    const original = { id: "queued-user", role: "user" as const, content: "queued original", timestamp: 1,
      artifacts: [], queueState: "queued" as const, queueMessageId: "queued-assistant" };
    useAppStore.setState({ draft: "editing queued input", isStreaming: true,
      queuedEdit: { ownerId: "main", workspaceRoot: root, original, backup: emptyComposerDraft(), backupDiffComments: [] } });
    let fail!: (reason: Error) => void;
    vi.mocked(buildContextNativeAttachments).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    render(<Composer />); submit();
    await waitFor(() => expect(fail).toBeTypeOf("function"));
    expect(useAppStore.getState().draft).toBe("editing queued input");
    await act(async () => fail(new Error("queue context failed")));
    expect(useAppStore.getState().draft).toBe("editing queued input");
    expect(useAppStore.getState().queuedEdit?.original.id).toBe(original.id);
    expect(wire.commands).toEqual([]);
  });

  it("restores a rejected cleared input to its hidden owner while preserving the active chat's newer draft", async () => {
    useAppStore.setState({ draft: "main original", conversations: [
      { id: "main", title: "main", workspaceRoot: root }, { id: "other", title: "other", workspaceRoot: root },
    ] }); setDiffComments("main", [comment]);
    render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    act(() => acknowledgeClientCommand({ type: "client.command.ack", client_command_id: command.client_command_id,
      command_type: "user_message" } as ServerEvent));
    act(() => {
      useAppStore.getState().applyConversationSwitched({ conversationId: "other" });
      useAppStore.getState().setDraft("other new input"); setDiffComments("other", [secondComment]);
    });
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: false }, "error")));
    expect(useAppStore.getState().draft).toBe("other new input");
    expect(readDiffCommentState("other").comments).toEqual([secondComment]);
    expect(useAppStore.getState().conversationWorkbenchStates.main.draft).toBe("main original");
    expect(readDiffCommentState("main").comments).toEqual([comment]);
  });

  it.each(["enter", "cmdIfMultiline", "mod-enter"] as const)("sends Source's opposite running follow-up from the actual main composer for %s without changing global preferences", async shortcut => {
    const running = { id: "running-answer", turnId: "running-answer-turn", role: "assistant" as const, content: "running", timestamp: 1, artifacts: [], isStreaming: true };
    useAppStore.setState({ messages: [running], isStreaming: true, draft: "authored follow-up\nsecond line", sendShortcut: shortcut, followUpBehavior: "queue" });
    render(<Composer />);
    const input = screen.getByRole("textbox", { name: "消息输入" });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, shiftKey: shortcut !== "enter" });
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command).toMatchObject({ conversation_id: "main", content: "authored follow-up\nsecond line", streaming_behavior: "steer", expected_turn_id: running.turnId });
    expect(useAppStore.getState()).toMatchObject({ followUpBehavior: "queue", sendShortcut: shortcut, draft: "" });
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: false, queued: true, turn_mode: "steer" })));
    await waitFor(() => expect(useAppStore.getState().draft).toBe(""));
    expect(useAppStore.getState().messages.filter(message => message.role === "user")).toHaveLength(1);
  });

  it("sends the opposite follow-up for the real side owner while the main conversation is idle and preserves its draft", async () => {
    useAppStore.setState({ draft: "main unsent text", sendShortcut: "cmdIfMultiline", followUpBehavior: "steer",
      sideChats: { side: { id: "side", messages: [], isStreaming: true, workspaceRoot: root,
        draft: "side follow up\nsecond line", attachments: [] } } });
    render(<SideChatPanel />);
    await waitFor(() => expect((screen.getByRole("button", { name: "将消息加入队列" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "侧边对话消息" }), { key: "Enter", ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    expect(wire.commands[0]).toMatchObject({ conversation_id: "side", streaming_behavior: "follow_up", content: "side follow up\nsecond line" });
    await act(async () => handleCommandResultEvent(receipt(wire.commands[0], { admitted: false, queued: true, conversation_id: "side" })));
    await waitFor(() => expect(useAppStore.getState().sideChats.side.draft).toBe(""));
    expect(useAppStore.getState()).toMatchObject({ draft: "main unsent text", isStreaming: false, followUpBehavior: "steer", sendShortcut: "cmdIfMultiline" });
  });

  it("review targets own input menus through selection effects, cancel and owner changes", async () => {
    const description = "审查当前项目并按严重级别列出问题";
    useAppStore.setState({ draft: "/review", slashCommands: [{ name: "review", command: "review", label: "/review", description, type: "template", source: "builtin" }] });
    const view = render(<Composer />);
    submit();
    await screen.findByRole("menuitem", { name: "审查未提交的更改" });
    await waitFor(() => expect(useAppStore.getState().slashPanelOpen).toBe(false));
    expect(screen.queryByText(description)).toBeNull();
    expect(useAppStore.getState().draft).toBe("/review");
    fireEvent.keyDown(screen.getByRole("textbox", { name: "消息输入" }), { key: "Escape" });
    await waitFor(() => expect(view.container.querySelector("[data-code-review-menu]")).toBeNull());
    expect(screen.queryByText(description)).toBeNull();
    expect(useAppStore.getState().draft).toBe("/review");
    expect(wire.commands).toHaveLength(0);
    submit();
    await screen.findByRole("menuitem", { name: "审查未提交的更改" });
    await act(async () => useAppStore.setState({ conversationId: "other", workingDirectory: "C:/other", draft: "different unsaved input" }));
    await waitFor(() => expect(view.container.querySelector("[data-code-review-menu]")).toBeNull());
    expect(useAppStore.getState().draft).toBe("different unsaved input");
    expect(wire.commands).toHaveLength(0);
  });

  it.each([["uncommitted", false], ["base-branch", false], ["uncommitted", true], ["base-branch", true]] as const)("review %s keeps attachments and durable mode until its ordinary input is admitted; late=%s", async (mode, late) => {
    const { attachment } = fileDraft();
    useAppStore.setState({ draft: "/review", agentMode: "plan", attachments: [attachment],
      slashCommands: [{ name: "review", command: "review", label: "/review", description: "Review", type: "template", source: "builtin" }] });
    render(<Composer />);
    submit();
    const target = await screen.findByRole("menuitem", { name: mode === "uncommitted" ? "审查未提交的更改" : "main" });
    fireEvent.click(target);
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command.code_review).toMatchObject({ mode, cwd: root, gitRoot: root });
    if (mode === "base-branch") expect(command.code_review).toMatchObject({ baseBranch: "main", mergeBaseSha: "abcdef1234" });
    expect(command.agent_mode).toBeUndefined();
    expect(command.attachments).toBeUndefined();
    expect(command.display_content).not.toContain("guidelines");
    expect(command.content).toContain("## Code review guidelines:");
    expect(useAppStore.getState().draft).toBe("/review");
    expect(useAppStore.getState().attachments).toHaveLength(1);
    if (late) await act(async () => resetPendingCommandResultsForTests());
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: true })));
    await waitFor(() => expect(useAppStore.getState().diffReview?.codeReview?.mode).toBe(mode));
    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().attachments).toHaveLength(1);
    expect(useAppStore.getState().agentMode).toBe("plan");
    const user = useAppStore.getState().messages.find(message => message.role === "user")!;
    expect(user.codeReview).toEqual(command.code_review);
    expect(user.content).toBe(command.display_content);
    expect(user.backendContent).toBe(command.content);
  });

  it("starts detached review on its own conversation and preserves the original composer draft", async () => {
    wire.heldSwitchOwner = "separate-review";
    const { attachment } = fileDraft();
    useAppStore.setState({ draft: "/review", agentMode: "plan", attachments: [attachment],
      slashCommands: [{ name: "review", command: "review", label: "/review", description: "Review", type: "template", source: "builtin" }] });
    vi.mocked(createCodeReview).mockImplementationOnce(async (source, cwd, mode, _base, _sha, requestMessage) => {
      useAppStore.setState(state => ({ conversations: [...state.conversations,
        { id: "separate-review", title: "Review", workspaceRoot: root, executionCwd: root }] }));
      return { delivery: "detached", reviewThreadId: "separate-review", sourceConversationId: source!,
        scope: { mode, cwd, gitRoot: cwd }, prompt: "## Code review guidelines:\n## My request:\nReview original changes",
        requestMessage: requestMessage!, diff: "" };
    });
    render(<Composer />); submit();
    fireEvent.click(await screen.findByRole("menuitem", { name: "审查未提交的更改" }));
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command.conversation_id).toBe("separate-review");
    expect(command.code_review?.cwd).toBe(root);
    expect(command.attachments).toBeUndefined();
    expect(useAppStore.getState().messages).toHaveLength(0);
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: true })));
    await waitFor(() => expect(useAppStore.getState().pendingConversationSwitchId).toBe("separate-review"));
    expect(useAppStore.getState().conversationId).toBe("main");
    expect(useAppStore.getState().draft).toBe("/review");
    expect(useAppStore.getState().attachments[0].id).toBe(attachment.id);
    expect(useAppStore.getState().messages).toHaveLength(0);
    expect(useAppStore.getState().conversationMessages["separate-review"].some(message => message.role === "user")).toBe(true);
    expect(useAppStore.getState().conversationWorkbenchStates["separate-review"].diffReview?.conversationId).toBe("separate-review");
    const switchCommand = wire.resources.find(command => command.type === "conversation.switch" && command.conversation_id === "separate-review")!;
    act(() => handleSessionEvent({ type: "conversation.switched", conversation_id: "separate-review",
      is_hydrating: false,
      client_command_id: switchCommand.client_command_id, client_command_type: "conversation.switch",
      conversation: { id: "separate-review", title: "Review", workspace_root: root, execution_cwd: root, messages: [] },
      session: { active_conversation_id: "separate-review", workspace_root: root, active_stream_conversation_ids: [] },
    } as ServerEvent, { textStreamBuffer: buffer, thinkingStreamBuffer: buffer }));
    expect(useAppStore.getState()).toMatchObject({ conversationId: "separate-review", pendingConversationSwitchId: null });
    expect(useAppStore.getState().messages.some(message => message.id === command.user_message_id && message.role === "user")).toBe(true);
    expect(useAppStore.getState().conversationWorkbenchStates.main).toMatchObject({ draft: "/review", attachments: [{ id: attachment.id }] });
    expect(useAppStore.getState().conversationMessages.main).toEqual([]);
  });

  const received = (command: UserMessageCommand) => acknowledgeClientCommand({ type: "client.command.ack",
    client_command_id: command.client_command_id, command_type: "user_message" } as ServerEvent);

  it("renders the user's prompt and clears the accepted local input before receipt while waiting for the real assistant start", async () => {
    useAppStore.setState({ draft: "show before server receipt", subagents: [], backgroundTasks: [] });
    const view = render(<><MessageList /><Composer /></>);
    submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(view.container.querySelector(".user-cell-bubble")?.textContent).toBe("show before server receipt");
    expect(useAppStore.getState().messages.map(message => message.role)).toEqual(["user"]);
    expect(useAppStore.getState().isStreaming).toBe(false);
    expect(useAppStore.getState().draft).toBe("");
    expect((screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement).value).toBe("");
    act(() => received(command));
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().draft).toBe("");
    act(() => started(command));
    const assistant = useAppStore.getState().messages[1];
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: true })));
    expect(useAppStore.getState().messages.map(message => message.id)).toEqual([command.user_message_id, command.assistant_message_id]);
    expect(useAppStore.getState().messages[1]).toBe(assistant);
  });

  it("keeps newly typed identical text through a late receipt and admission of the earlier local send", async () => {
    useAppStore.setState({ draft: "same authored text" });
    render(<><MessageList /><Composer /></>);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "消息输入" }), { key: "Enter", code: "Enter" });
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(useAppStore.getState().draft).toBe("");
    expect(document.querySelectorAll(".user-cell-bubble")).toHaveLength(1);
    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "same authored text" } });
    act(() => received(command));
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: true })));
    expect(useAppStore.getState().draft).toBe("same authored text");
    expect(useAppStore.getState().messages.filter(message => message.role === "user")).toHaveLength(1);
  });

  it("retains the captured input when the local sender refuses it before any receipt", async () => {
    const { file, attachment } = fileDraft();
    useAppStore.setState({ draft: "local transport refused", attachments: [attachment] });
    wire.accepted = false;
    render(<Composer />);
    submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    await act(async () => {});
    expect(useAppStore.getState().draft).toBe("local transport refused");
    expect(await useAppStore.getState().attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
  });

  it("restores the captured input when the server rejects the transport receipt", async () => {
    useAppStore.setState({ draft: "server rejected transport receipt" });
    render(<Composer />);
    submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(useAppStore.getState().draft).toBe("");
    act(() => acknowledgeClientCommand({ type: "client.command.ack", client_command_id: command.client_command_id,
      command_type: "user_message", accepted: false, reason: "transport admission rejected" } as ServerEvent));
    await waitFor(() => expect(useAppStore.getState().draft).toBe("server rejected transport receipt"));
    expect((screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement).value).toBe("server rejected transport receipt");
  });

  it.each(["enter", "button"])("%s consumes the first draft before creation and carries one user identity into the real conversation", async method => {
    useAppStore.setState({ conversationId: null, activeConversationDraftId: null, conversations: [], messages: [] });
    const draftId = useAppStore.getState().startConversationDraft({ workspaceRoot: root, bindWorkspace: true });
    await useAppStore.getState().refreshConversationDraftSelection(draftId);
    useAppStore.getState().setDraft("first prompt before owner creation");
    wire.holdCreation = true;
    const view = render(<><MessageList /><Composer /></>);
    const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    if (method === "enter") fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    else submit();
    expect(useAppStore.getState().draft).toBe("");
    expect(input.value).toBe("");
    expect(view.container.querySelector(".user-cell-bubble")?.textContent).toBe("first prompt before owner creation");
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(wire.resources.some(command => command.type === "conversation.create")).toBe(true));
    expect(view.container.querySelector(".user-cell-bubble")?.textContent).toBe("first prompt before owner creation");
    expect(useAppStore.getState().messages).toEqual([]);
    expect(wire.commands).toEqual([]);
    expect(useAppStore.getState().isStreaming).toBe(false);
    const localId = useAppStore.getState().conversationDrafts[draftId].pendingUserMessage!.id;
    expect((await coldRecords()).find(record => record.owner.id === draftId)?.draftContext?.pendingUserMessage).toBeUndefined();
    const created = wire.resources.find(command => command.type === "conversation.create")!;
    expect(wire.resources.filter(command => command.type === "conversation.create")).toHaveLength(1);
    await act(async () => handleCommandResultEvent({ type: "command.result", command: created.type,
      client_command_id: created.client_command_id, client_command_type: created.type, level: "success", message: "",
      data: { conversation_id: (created as { conversation_id: string }).conversation_id, execution_cwd: root, created: true, activated: false } }));
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command.user_message_id).toBe(localId);
    expect(view.container.querySelectorAll(".user-cell-bubble")).toHaveLength(1);
    await act(async () => { started(command, command.conversation_id); handleCommandResultEvent(receipt(command, { admitted: true })); });
    expect(useAppStore.getState().messages.filter(message => message.id === localId)).toHaveLength(1);
    expect(useAppStore.getState().conversationDrafts[draftId].pendingUserMessage).toBeUndefined();
    expect(useAppStore.getState().draft).toBe("");
    expect((view.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement).value).toBe("");
  });

  it.each(["untouched", "new-text", "hidden"])("restores a first input after creation fails while the draft is %s", async editing => {
    useAppStore.setState({ conversationId: null, activeConversationDraftId: null, conversations: [], messages: [] });
    const draftId = useAppStore.getState().startConversationDraft({ workspaceRoot: root, bindWorkspace: true });
    await useAppStore.getState().refreshConversationDraftSelection(draftId);
    useAppStore.getState().setDraft("first creation failed");
    wire.holdCreation = true;
    render(<Composer />);
    const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    expect(input.value).toBe("");
    await waitFor(() => expect(wire.resources.some(command => command.type === "conversation.create")).toBe(true));
    if (editing === "new-text") fireEvent.change(input, { target: { value: "new unsent input" } });
    if (editing === "hidden") act(() => {
      useAppStore.getState().startConversationDraft({ workspaceRoot: "C:/another-project", bindWorkspace: true });
      useAppStore.getState().setDraft("other unsent input");
    });
    const created = wire.resources.find(command => command.type === "conversation.create")!;
    await act(async () => handleCommandResultEvent({ type: "command.result", command: created.type,
      client_command_id: created.client_command_id, client_command_type: created.type, level: "error", message: "Creation rejected", data: { created: false } }));
    await waitFor(() => expect(useAppStore.getState().conversationDrafts[draftId].materializing).toBe(false));
    expect(wire.commands).toHaveLength(0);
    expect(useAppStore.getState().draft).toBe(editing === "untouched" ? "first creation failed" : editing === "new-text" ? "new unsent input" : "other unsent input");
    if (editing === "hidden") expect(useAppStore.getState().workspaceComposerDrafts[draftId].draft).toBe("first creation failed");
  });

  it("clears a first message after the implicit draft is materialized", async () => {
    useAppStore.setState({ conversationId: null, activeConversationDraftId: null, conversations: [], messages: [], draft: "hi" });
    render(<><MessageList /><Composer /></>);
    submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    expect(useAppStore.getState().conversationId).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("");
    expect((screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement).value).toBe("");
    expect(useAppStore.getState().conversationWorkbenchStates[useAppStore.getState().conversationId!]?.draft || "").toBe("");
  });

  it("uses the existing send receipt lock for mode edits and releases it after the first input is durably received", async () => {
    useAppStore.setState({ draft: "captured build mode", agentMode: "build", runtimeCapabilities: { feature_flags: { goals: { enabled: true } } } });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0]; expect(command.agent_mode).toBe("build");
    expect(await useAppStore.getState().setAgentMode("plan")).toBe(false);
    expect(enterComposerGoalMode()).toBe(false);
    expect(useAppStore.getState()).toMatchObject({ agentMode: "build", goalMode: false });
    fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
    expect((screen.getByRole("menuitem", { name: /^计划模式/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("menuitem", { name: /^目标/ }) as HTMLButtonElement).disabled).toBe(true);
    act(() => received(command));
    await waitFor(() => expect((screen.getByRole("menuitem", { name: /^计划模式/ }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("menuitem", { name: /^计划模式/ }));
    await waitFor(() => expect(useAppStore.getState().pendingAgentModeChanges).toEqual({}));
    expect(useAppStore.getState().agentMode).toBe("plan");
    expect(wire.resources.find(command => command.type === "conversation.agent_mode.set")).toMatchObject({ conversation_id: "main", agent_mode: "plan" });
    expect(command.agent_mode).toBe("build");
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: true })));
    expect(useAppStore.getState().conversationId).toBe("main");
    expect(useAppStore.getState().agentMode).toBe("plan");
  });

  it.each(["send", "menu"] as const)("submits compact through %s, clears on receipt and renders its actual lifecycle while preserving new input", async method => {
    const { attachment } = fileDraft();
    const quote = { id: "compact-quote", role: "assistant" as const, content: "保留这个引用" };
    const mention = { kind: "file" as const, name: "source.ts", path: "source.ts" };
    useAppStore.setState({ draft: method === "send" ? "/compact" : "", attachments: [{ ...attachment, status: "uploading", attachment: undefined }],
      quotedMessage: quote, selectedMentions: [mention], contextUsage: { used: 120, limit: 1000 },
      slashCommands: [{ name: "compact", command: "compact", label: "/compact", description: "压缩上下文", type: "local" }] });
    render(<><MessageList /><Composer /></>);
    const input = screen.getByRole("textbox", { name: "消息输入" });
    if (method === "send") submit();
    else {
      fireEvent.change(input, { target: { value: "/compact" } });
      await waitFor(() => expect(screen.getByRole("listbox")).toBeTruthy());
      fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    }
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command).toMatchObject({ content: "/compact", display_content: "/compact", context_refs: [], quoted_message: null });
    expect(command.attachments).toBeUndefined();
    expect(useAppStore.getState().draft).toBe("/compact");
    act(() => received(command));
    expect(useAppStore.getState().draft).toBe("");
    act(() => handleNoticeEvent({ type: "conversation.compaction.updated", conversation_id: "main", state: "compacting",
      summary: "正在压缩上下文。" } as ServerEvent));
    expect(screen.getByText("正在压缩上下文").closest(".status-notice-cell")?.getAttribute("data-processing")).toBe("true");
    expect(screen.queryByText("系统通知")).toBeNull();
    expect(useAppStore.getState().contextUsage?.used).toBe(120);
    // The late completion must not erase another draft, even with identical text.
    fireEvent.change(input, { target: { value: "/compact" } });
    act(() => handleNoticeEvent({ type: "conversation.compaction.updated", conversation_id: "main", state: "compacted",
      summary: "实际摘要" } as ServerEvent));
    act(() => handleRuntimeEvent({ type: "context_compacted", conversation_id: "main", summary: "实际摘要",
      before_tokens: 120, after_tokens: 50 } as ServerEvent));
    await act(async () => handleCommandResultEvent(nativeReceipt(command, "completed")));
    expect(useAppStore.getState().draft).toBe("/compact");
    expect(screen.queryByText("正在压缩上下文")).toBeNull();
    expect(screen.getAllByText("上下文已压缩")).toHaveLength(1);
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().attachments[0].id).toBe(attachment.id);
    expect(useAppStore.getState().quotedMessage).toEqual(quote);
    expect(useAppStore.getState().selectedMentions).toEqual([mention]);
  });

  it("keeps an unfenced compact failure in its own notice without failing the previous answer", () => {
    const answer = { id: "previous-answer", role: "assistant" as const, content: "上次的完整答案", blocks: [], artifacts: [], timestamp: 1, terminalStatus: "completed" as const };
    useAppStore.setState({ messages: [answer], draft: "继续写的新草稿" });
    handleNoticeEvent({ type: "conversation.compaction.updated", conversation_id: "main", state: "compacting", summary: "正在压缩上下文。" } as ServerEvent);
    handleNoticeEvent({ type: "conversation.compaction.updated", conversation_id: "main", state: "failed", summary: "Compact failed: HTTP 400" } as ServerEvent);
    stream({ type: "error", conversation_id: "main", error_code: "context.compact_failed", message: "Compact failed: HTTP 400" } as ServerEvent);
    render(<MessageList />);
    expect(screen.getByRole("alert").textContent).toContain("HTTP 400");
    expect(screen.getByText("压缩失败")).toBeTruthy();
    expect(useAppStore.getState().messages[0]).toBe(answer);
    expect(useAppStore.getState().draft).toBe("继续写的新草稿");
    expect(useAppStore.getState().messages[1].systemNoticeProcessing).toBe(false);
  });

  it("renders the captured user and clears Enter on durable receipt before any real work starts", async () => {
    useAppStore.setState({ draft: "先显示这一条用户消息" });
    render(<><MessageList /><Composer /></>);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "消息输入" }), { key: "Enter", code: "Enter" });
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    act(() => received(command));
    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().messages.map(message => message.role)).toEqual(["user"]);
    expect(screen.getByText("先显示这一条用户消息")).toBeTruthy();
    expect(screen.queryByLabelText("Agent 处理进度")).toBeNull();
    act(() => handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "started-real",
      conversation_id: "main", message_id: command.assistant_message_id, turn_id: "started-real" } as ServerEvent));
    expect(useAppStore.getState().messages.map(message => message.id)).toEqual([command.user_message_id, command.assistant_message_id]);
    expect(screen.getByLabelText("Agent 处理进度")).toBeTruthy();
    act(() => handleCommandResultEvent(receipt(command, { admitted: true })));
  });

  it("a rejection after receipt restores the submitted draft and original file bytes", async () => {
    const { file, attachment } = fileDraft();
    useAppStore.setState({ draft: "第一次发送", attachments: [attachment] });
    render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    act(() => received(command));
    expect(useAppStore.getState().draft).toBe("");
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: false }, "error")));
    expect(useAppStore.getState().draft).toBe("第一次发送");
    expect(await useAppStore.getState().attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
  });

  it.each(["quote", "mention", "skill"] as const)("an older rejection preserves a newly selected %s with empty text through cold recovery", async kind => {
    useAppStore.setState({ draft: "已经提交的旧文字" });
    render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    act(() => received(command));
    const context = kind === "quote" ? { quotedMessage: { id: "new-quote", role: "assistant" as const, content: "新选引用" } }
      : kind === "mention" ? { selectedMentions: [{ kind: "file" as const, name: "new.ts", path: "new.ts", workspaceRoot: root }] }
        : { selectedSkills: [{ name: "new-skill", path: "skills/new/SKILL.md" }] };
    act(() => useAppStore.setState(context));
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: false }, "error")));
    expect(useAppStore.getState()).toMatchObject({ draft: "", ...context });
    const restored = (await coldRecords()).find(record => record.owner.kind === "conversation" && record.owner.id === "main")?.draft;
    expect(restored).toMatchObject({ draft: "", ...context });
  });

  it("allows a second authored input before the first receipt without waiting for model admission", async () => {
    useAppStore.setState({ draft: "同样的话" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const first = wire.commands[0];
    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "同样的话" } });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "消息输入" }), { key: "Enter", code: "Enter" });
    await waitFor(() => expect(wire.commands).toHaveLength(2));
    const second = wire.commands[1]; act(() => received(second));
    expect(first.user_message_id).not.toBe(second.user_message_id);
    expect(useAppStore.getState().messages.filter(message => message.role === "user")).toHaveLength(2);
    await act(async () => handleCommandResultEvent(receipt(first, { admitted: false }, "error")));
    expect(useAppStore.getState().draft).toBe("");
    await act(async () => handleCommandResultEvent(receipt(second, { admitted: false, queued: true })));
  });

  it("releases each locally submitted composer before receipt and preserves another owner's new input through late outcomes", async () => {
    useAppStore.setState({ draft: "A original input" });
    render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const first = wire.commands[0];
    let send = screen.getByRole("button", { name: "发送" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect(send.getAttribute("aria-busy")).toBeNull();
    expect(send.querySelector("svg.animate-spin")).toBeNull();
    expect(useAppStore.getState().draft).toBe("");
    fireEvent.click(send);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "消息输入" }), { key: "Enter", code: "Enter" });
    expect(wire.commands).toHaveLength(1);

    act(() => useAppStore.setState({ conversationId: "other", draft: "B original input", messages: [], isStreaming: false }));
    send = screen.getByRole("button", { name: "发送" }) as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    expect(send.getAttribute("aria-busy")).toBeNull();
    submit(); await waitFor(() => expect(wire.commands).toHaveLength(2));
    const second = wire.commands[1];
    expect(second.conversation_id).toBe("other");
    expect(screen.getByRole("button", { name: "发送" }).getAttribute("aria-busy")).toBeNull();
    expect(useAppStore.getState().draft).toBe("");
    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "B newer input" } });

    act(() => received(first));
    expect(screen.getByRole("button", { name: "发送" }).getAttribute("aria-busy")).toBeNull();
    expect(useAppStore.getState().draft).toBe("B newer input");
    await act(async () => handleCommandResultEvent(receipt(first, { admitted: false }, "error")));
    expect(screen.getByRole("button", { name: "发送" }).getAttribute("aria-busy")).toBeNull();
    expect(useAppStore.getState().draft).toBe("B newer input");
    act(() => received(second));
    expect(screen.getByRole("button", { name: "发送" }).getAttribute("aria-busy")).toBeNull();
    await act(async () => handleCommandResultEvent(receipt(second, { admitted: true })));
  });

  it("keeps fenced Stop waiting on its original conversation through ACK, hot switches and a late refusal", async () => {
    const a = { id: "stop-message-A", turnId: "stop-turn-A", role: "assistant" as const, content: "Actual A work", artifacts: [], timestamp: 1, isStreaming: true };
    const b = { ...a, id: "stop-message-B", turnId: "stop-turn-B", content: "Actual B work" };
    useAppStore.setState({ messages: [a], isStreaming: true, conversationStreaming: { main: true, other: true }, conversationMessages: { other: [b] } });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "停止当前回复" }));
    const first = wire.interrupts[0];
    expect(first).toMatchObject({ type: "interrupt", conversation_id: "main", turn_id: a.turnId, message_id: a.id, client_command_id: expect.any(String) });
    let stop = screen.getByRole("button", { name: "停止当前回复" }) as HTMLButtonElement;
    expect(stop.disabled).toBe(true);
    expect(stop.getAttribute("aria-busy")).toBe("true");
    expect(stop.querySelector("svg.animate-spin")).not.toBeNull();
    act(() => acknowledgeClientCommand({ type: "client.command.ack", client_command_id: first.client_command_id, command_type: "interrupt" } as ServerEvent));
    expect(stop.getAttribute("aria-busy")).toBe("true");
    fireEvent.click(stop);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "消息输入" }), { key: "Escape" });
    expect(wire.interrupts).toHaveLength(1);
    expect(useAppStore.getState().isStreaming).toBe(true);

    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "A unsent follow-up" } });
    expect((screen.getByRole("button", { name: "将消息加入队列" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("button", { name: "停止当前回复" }).getAttribute("aria-busy")).toBe("true");
    act(() => useAppStore.setState({ conversationId: "other", draft: "", messages: [b], conversationMessages: { main: [a] }, isStreaming: true }));
    stop = screen.getByRole("button", { name: "停止当前回复" }) as HTMLButtonElement;
    expect(stop.disabled).toBe(false);
    expect(stop.getAttribute("aria-busy")).toBeNull();
    fireEvent.click(stop);
    const second = wire.interrupts[1];
    expect(second).toMatchObject({ conversation_id: "other", turn_id: b.turnId, message_id: b.id });

    act(() => useAppStore.setState({ conversationId: "main", draft: "A unsent follow-up", messages: [a], conversationMessages: { other: [b] }, isStreaming: true }));
    expect(screen.getByRole("button", { name: "停止当前回复" }).getAttribute("aria-busy")).toBe("true");
    await act(async () => handleCommandResultEvent({ type: "command.result", command: "interrupt", client_command_id: first.client_command_id,
      level: "success", message: "", data: { conversation_id: "main" } }));
    expect(screen.getByRole("button", { name: "停止当前回复" }).getAttribute("aria-busy")).toBeNull();
    expect(useAppStore.getState()).toMatchObject({ isStreaming: true, draft: "A unsent follow-up" });
    act(() => useAppStore.setState({ conversationId: "other", draft: "B unsent follow-up", messages: [b], conversationMessages: { main: [a] }, isStreaming: true }));
    expect(screen.getByRole("button", { name: "停止当前回复" }).getAttribute("aria-busy")).toBe("true");
    await act(async () => handleCommandResultEvent({ type: "command.result", command: "interrupt", client_command_id: second.client_command_id,
      level: "error", message: "Actual B cancellation refused", data: { conversation_id: "other" } }));
    expect(screen.getByRole("button", { name: "停止当前回复" }).getAttribute("aria-busy")).toBeNull();
    expect(wire.toast).toHaveBeenCalledWith("Actual B cancellation refused", "error", 4500);
    expect(useAppStore.getState()).toMatchObject({ isStreaming: true, draft: "B unsent follow-up" });
  });

  it("uses actual terminal state when done arrives before the original Stop receipt", async () => {
    const message = { id: "stop-before-receipt", turnId: "stop-before-receipt-turn", role: "assistant" as const, content: "Actual work", artifacts: [], timestamp: 1, isStreaming: true };
    useAppStore.setState({ messages: [message], isStreaming: true, conversationStreaming: { main: true } });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "停止当前回复" }));
    const command = wire.interrupts[0];
    act(() => stream({ type: "done", conversation_id: "main", message_id: message.id, turn_id: message.turnId,
      status: "interrupted", usage: {} } as ServerEvent));
    expect(useAppStore.getState().isStreaming).toBe(false);
    expect(screen.queryByRole("button", { name: "停止当前回复" })).toBeNull();
    expect(screen.getByRole("button", { name: "发送" }).getAttribute("aria-busy")).toBeNull();
    await act(async () => handleCommandResultEvent({ type: "command.result", command: "interrupt", client_command_id: command.client_command_id,
      level: "success", message: "", data: { conversation_id: "main" } }));
    expect(useAppStore.getState().messages[0].terminalStatus).toBe("interrupted");
  });

  it.each(["offline", "refused"] as const)("retains locally rejected or server-%s input and original File bytes through a cold reopen", async mode => {
    const { file, attachment } = fileDraft();
    const quote = { id: "quoted", role: "assistant" as const, content: "原引用" };
    const mention = { kind: "file" as const, name: "source.ts", path: "source.ts", workspaceRoot: root };
    useAppStore.setState({ draft: "尚未持久准入的原输入", attachments: [attachment], quotedMessage: quote, selectedMentions: [mention], isConnected: mode !== "offline" });
    if (mode === "offline") wire.accepted = false;
    const view = render(<Composer />);
    submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(useAppStore.getState().draft).toBe(mode === "offline" ? "尚未持久准入的原输入" : "");
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().messages[0]).toMatchObject({ role: "user", deliveryStatus: mode === "offline" ? "failed" : "sending" });
    if (mode === "refused") await act(async () => handleCommandResultEvent(receipt(command, { admitted: false }, "error")));
    view.unmount();
    const records = await coldRecords();
    const restored = records.find(record => record.owner.id === "main")!.draft;
    expect(restored).toMatchObject({ draft: "尚未持久准入的原输入", quotedMessage: quote, selectedMentions: [mention] });
    expect(await restored.attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
    expect(restored.attachments[0].localFile?.name).toBe("中文 空格文件.txt");
  });

  it.each([{ admitted: true }, { admitted: false, queued: true }])("keeps locally consumed input empty through durable $admitted/$queued and cannot cold resurrect sent text or files", async fact => {
    const { attachment } = fileDraft();
    useAppStore.setState({ draft: "received input", attachments: [attachment], quotedMessage: { id: "q", role: "user", content: "quote" } });
    const view = render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    expect(useAppStore.getState().draft).toBe("");
    await act(async () => handleCommandResultEvent(receipt(wire.commands[0], fact)));
    await waitFor(() => expect(useAppStore.getState().draft).toBe(""));
    expect(useAppStore.getState().attachments).toEqual([]);
    view.unmount();
    expect(await coldRecords()).toEqual([]);
  });

  it.each([false, true])("a first real draft owner preserves newer input=%s through admission and immediate item/delta frames", async newer => {
    useAppStore.setState({ conversationId: null, conversations: [], draft: "first submitted", attachments: [] });
    render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    const owner = command.conversation_id!;
    expect(owner).toBeTruthy();
    expect(useAppStore.getState().conversationId).toBe(owner);
    expect(wire.resources.map(command => command.type)).toEqual(["conversation.create", "llm.model.set", "conversation.switch"]);
    expect(wire.resources[0]).toMatchObject({ conversation_id: owner, activate: false });
    expect(wire.resources[2]).toMatchObject({ conversation_id: owner, defer_runtime_capabilities: true });
    expect(wire.resources.every(command => "conversation_id" in command && command.conversation_id === owner)).toBe(true);
    if (newer) fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "newly authored" } });
    act(() => {
      started(command, owner);
      handleCommandResultEvent(receipt(command, { admitted: true }));
      itemFrames(command, owner);
    });
    expect(useAppStore.getState().conversationId).toBe(owner);
    expect(useAppStore.getState().messages.map(message => message.id)).toEqual([command.user_message_id, command.assistant_message_id]);
    expect(useAppStore.getState().messages[1].blocks).toEqual(expect.arrayContaining([expect.objectContaining({ content: "real answer" })]));
    await waitFor(() => expect(useAppStore.getState().draft).toBe(newer ? "newly authored" : ""));
  });

  it("keeps the exact native assistant already started before admission and inserts the captured user once", async () => {
    useAppStore.setState({ draft: "received input" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    act(() => handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "native-run", conversation_id: "main",
      message_id: command.assistant_message_id, turn_id: "native-run" } as ServerEvent));
    const native = useAppStore.getState().messages.find(message => message.id === command.assistant_message_id);
    act(() => handleCommandResultEvent(receipt(command, { admitted: true })));
    expect(useAppStore.getState().messages.map(message => message.id)).toEqual([command.user_message_id, command.assistant_message_id]);
    expect(useAppStore.getState().messages[1]).toBe(native);
    await waitFor(() => expect(useAppStore.getState().draft).toBe(""));
  });

  it("a captured owner's receipt clears its unchanged hidden snapshot and preserves the newly opened draft", async () => {
    const { attachment } = fileDraft();
    useAppStore.setState({ draft: "main submitted", attachments: [attachment] });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    act(() => {
      useAppStore.getState().snapshotWorkbenchState("main");
      useAppStore.setState({ conversationId: "other", workingDirectory: "C:/other-owner", draft: "new owner authored", attachments: [] });
      handleCommandResultEvent(receipt(wire.commands[0], { admitted: true }));
    });
    await waitFor(() => expect(useAppStore.getState().conversationWorkbenchStates.main.draft).toBe(""));
    expect(useAppStore.getState()).toMatchObject({ conversationId: "other", draft: "new owner authored", attachments: [] });
    expect((await coldRecords()).some(record => record.owner.id === "main")).toBe(false);
  });

  it.each(["receipt-first", "dequeue-first"] as const)("keeps real queue state and continuous deltas with %s", async order => {
    const submission = sendChatMessage({ displayContent: "queued input", conversationId: "main", allowWhileStreaming: true, awaitAdmission: true });
    const command = wire.commands[0];
    act(() => {
      applyUserMessageQueueUpdate(queueFact(command, "queued"));
      if (order === "receipt-first") handleCommandResultEvent(receipt(command, { admitted: false, queued: true }));
      applyUserMessageQueueUpdate(queueFact(command, "dequeued"));
      expect(useAppStore.getState().messages.find(message => message.id === command.assistant_message_id)?.isStreaming).toBe(false);
      started(command);
      itemFrames(command);
      if (order === "dequeue-first") handleCommandResultEvent(receipt(command, { admitted: false, queued: true }));
    });
    expect(await submission).toBe(true);
    const messages = useAppStore.getState().messages;
    expect(messages.map(message => message.id)).toEqual([command.user_message_id, command.assistant_message_id]);
    expect(messages.some(message => message.queueState === "queued")).toBe(false);
    expect(messages[1].blocks).toEqual(expect.arrayContaining([expect.objectContaining({ content: "real answer" })]));
  });

  it("a steer fact before a late receipt keeps one existing answer and its original user identity", async () => {
    const running = { id: "running-answer", turnId: "running-answer-turn", role: "assistant" as const, content: "running", timestamp: 1, artifacts: [], isStreaming: true };
    useAppStore.setState({ messages: [running], isStreaming: true });
    const submission = sendChatMessage({ displayContent: "steer input", conversationId: "main", allowWhileStreaming: true, busyBehavior: "steer", awaitAdmission: true });
    const command = wire.commands[0];
    act(() => {
      applyUserMessageQueueUpdate(queueFact(command, "dequeued", { reason: "steered_current_turn", turn_mode: "steer", target_message_id: running.id }));
      handleCommandResultEvent(receipt(command, { admitted: false, queued: true, turn_mode: "steer" }));
    });
    expect(await submission).toBe(true);
    expect(useAppStore.getState().messages.map(message => message.id)).toEqual([running.id, command.user_message_id]);
    expect(useAppStore.getState().messages[1]).toMatchObject({ steeredIntoMessageId: running.id, queueState: undefined });
  });

  it("a late receipt does not reapply an earlier dequeue to a completed native answer", async () => {
    const submission = sendChatMessage({ displayContent: "queued input", conversationId: "main", allowWhileStreaming: true, awaitAdmission: true });
    const command = wire.commands[0];
    act(() => {
      applyUserMessageQueueUpdate(queueFact(command, "dequeued"));
      started(command);
      itemFrames(command);
      stream({ type: "item.completed", conversation_id: "main", message_id: command.assistant_message_id,
        item: { id: "answer", type: "agent_message", text: "real answer", source: "model_final", status: "completed" } } as ServerEvent);
      stream({ type: "done", conversation_id: "main", message_id: command.assistant_message_id, finish_reason: "stop" } as ServerEvent);
      handleCommandResultEvent(receipt(command, { admitted: false, queued: true }));
    });
    expect(await submission).toBe(true);
    expect(useAppStore.getState().messages.filter(message => message.role === "assistant")).toHaveLength(1);
    expect(useAppStore.getState().messages[1]).toMatchObject({ isStreaming: false, terminalStatus: "completed" });
    expect(useAppStore.getState().isStreaming).toBe(false);
  });

  it("keeps user-cancelled input without an error toast or fabricated failed turn", async () => {
    useAppStore.setState({ draft: "cancelled before first step" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    await act(async () => handleCommandResultEvent(receipt(wire.commands[0], { admitted: false, cancelled: true, reason: "user_interrupted" })));
    expect(useAppStore.getState().draft).toBe("cancelled before first step");
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().messages[0]).toMatchObject({ role: "user", deliveryStatus: "failed" });
    expect(wire.toast.mock.calls.some(([, level]) => level === "error")).toBe(false);
  });

  it("a real pre-step cancellation seals only its native assistant as interrupted and keeps the unadmitted input", async () => {
    useAppStore.setState({ draft: "cancelled native start" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    act(() => handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "pre-step-run", conversation_id: "main",
      message_id: command.assistant_message_id, turn_id: "pre-step-run" } as ServerEvent));
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: false, cancelled: true, reason: "user_interrupted" })));
    expect(useAppStore.getState().draft).toBe("cancelled native start");
    expect(useAppStore.getState().messages.find(message => message.id === command.assistant_message_id)).toMatchObject({ isStreaming: false, terminalStatus: "interrupted" });
    expect(wire.toast.mock.calls.some(([, level]) => level === "error")).toBe(false);
  });

  it("a lost receipt preserves the real state and presents unknown reception without automatic resubmission", async () => {
    useAppStore.setState({ draft: "unconfirmed input" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    await act(async () => resetPendingCommandResultsForTests());
    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().messages[0]).toMatchObject({ role: "user" });
    expect(wire.commands).toHaveLength(1);
    expect(wire.toast).toHaveBeenCalledWith(expect.stringContaining("接收尚未确认"), "warning", 6000);
    const command = wire.commands[0];
    act(() => { started(command); handleCommandResultEvent(receipt(command, { admitted: true })); });
    expect(useAppStore.getState().messages.map(message => message.id)).toEqual([command.user_message_id, command.assistant_message_id]);
    expect(useAppStore.getState().messages[0].deliveryStatus).toBe("sent");
    expect(wire.commands).toHaveLength(1);
  });

  it.each([true, false])("delivers the definitive admitted=%s outcome once after the actual wait timer expires", async admitted => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const onAdmission = vi.fn(), onRejected = vi.fn();
    try {
      const waiting = sendChatMessage({ displayContent: "delayed outcome", conversationId: "main", awaitAdmission: true,
        onAdmission, onRejected });
      const command = wire.commands[0];
      await act(async () => { await vi.advanceTimersByTimeAsync(60_001); });
      expect(await waiting).toBe(false);
      expect(onAdmission).not.toHaveBeenCalled(); expect(onRejected).not.toHaveBeenCalled();
      expect(useAppStore.getState().messages[0].deliveryStatus).toBe("sending");
      const event = receipt(command, { admitted }, admitted ? "success" : "error");
      act(() => { handleCommandResultEvent(event); handleCommandResultEvent(event); });
      expect(admitted ? onAdmission : onRejected).toHaveBeenCalledOnce();
      expect(admitted ? onRejected : onAdmission).not.toHaveBeenCalled();
      expect(useAppStore.getState().messages[0].deliveryStatus).toBe(admitted ? "sent" : "failed");
      expect(wire.commands).toHaveLength(1);
    } finally { vi.useRealTimers(); }
  });

  it.each(["admission", "transport"])("persists a late %s rejection with rich text and original bytes after its composer has unmounted", async kind => {
    const { file, attachment } = fileDraft();
    const richDraft = { markdown: "**Original pending input**", plainTextMode: false, document: { type: "doc", content: [
      { type: "paragraph", content: [{ type: "text", text: "Original pending input", marks: [{ type: "strong" }] }] }] } };
    useAppStore.setState({ draft: richDraft.markdown, richDraft, attachments: [attachment] });
    setDiffComments("main", [comment]);
    const view = render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "newer draft during wait" } });
    act(() => setDiffComments("main", [secondComment]));
    await act(async () => resetPendingCommandResultsForTests());
    expect(useAppStore.getState().retainedComposerInputs[command.user_message_id!]).toMatchObject({ unconfirmed: true });
    view.unmount();
    await act(async () => {
      if (kind === "admission") handleCommandResultEvent(receipt(command, { admitted: false }, "error"));
      else acknowledgeClientCommand({ type: "client.command.ack", client_command_id: command.client_command_id,
        accepted: false, reason: "真实准入拒绝", command_type: "user_message" } as ServerEvent);
    });
    expect(useAppStore.getState().draft).toBe("newer draft during wait");
    expect(readDiffCommentState("main").comments).toEqual([secondComment]);
    const saved = await coldRecords();
    const retained = saved.find(record => record.retainedInput?.message.id === command.user_message_id)!;
    expect(retained.draft.richDraft).toEqual(richDraft);
    expect(retained.retainedInput).toMatchObject({ comments: [comment], message: { failureMessage: "真实准入拒绝" } });
    expect(await retained.draft.attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
    expect(saved.find(record => !record.retainedInput && record.owner.id === "main")!.draft.draft).toBe("newer draft during wait");
  });

  it("records a late transport acceptance without claiming model admission or losing the later rejection", async () => {
    useAppStore.setState({ draft: "late transport receipt" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    await act(async () => resetPendingCommandResultsForTests());
    act(() => acknowledgeClientCommand({ type: "client.command.ack", client_command_id: command.client_command_id,
      accepted: true, command_type: "user_message" } as ServerEvent));
    expect(useAppStore.getState().messages).toEqual([expect.objectContaining({ id: command.user_message_id, deliveryStatus: "sent" })]);
    expect(useAppStore.getState().isStreaming).toBe(false);
    expect(useAppStore.getState().retainedComposerInputs[command.user_message_id!]).toMatchObject({ unconfirmed: true });
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: false }, "error")));
    expect(useAppStore.getState().retainedComposerInputs[command.user_message_id!].draft.draft).toBe("late transport receipt");
    expect(useAppStore.getState().messages[0].deliveryStatus).toBe("failed");
  });

  it("retires the pending input when its owner is confirmed deleted before a late successful receipt", async () => {
    useAppStore.setState({ draft: "input for deleted owner" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    await act(async () => resetPendingCommandResultsForTests());
    act(() => {
      handleCommandResultEvent({ type: "command.result", command: "conversation.delete", level: "success", message: "",
        data: { conversation_id: "main", deleted: true } });
      useAppStore.setState({ conversationId: "other", draft: "new owner draft", messages: [], conversationMessages: {}, conversationStreaming: {} });
    });
    await act(async () => {
      handleCommandResultEvent(receipt(command, { admitted: true }));
      started(command);
      applyUserMessageQueueUpdate(queueFact(command, "queued"));
    });
    expect(useAppStore.getState()).toMatchObject({ conversationId: "other", draft: "new owner draft", messages: [],
      conversationMessages: {}, retainedComposerInputs: {} });
  });

  it.each(["result", "committed-item", "queued"] as const)("retires a previously failed input when its edited retry is accepted by %s after the wait has ended", async kind => {
    useAppStore.setState({ draft: "original failed input" });
    const view = render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "preserved newer draft" } });
    await act(async () => handleCommandResultEvent(receipt(wire.commands[0], { admitted: false }, "error")));
    const originalId = wire.commands[0].user_message_id!;
    act(() => { restoreRetainedComposerInput(originalId); });
    submit(); await waitFor(() => expect(wire.commands).toHaveLength(2));
    const retry = wire.commands[1];
    await act(async () => resetPendingCommandResultsForTests());
    expect(useAppStore.getState().retainedComposerInputs[originalId]).toBeDefined();
    await act(async () => {
      if (kind === "result") handleCommandResultEvent(receipt(retry, { admitted: true }));
      else if (kind === "committed-item") {
        started(retry);
        stream({ type: "item.completed", conversation_id: "main", message_id: retry.assistant_message_id,
          item: { type: "user_message", id: retry.user_message_id, message: { id: retry.user_message_id, role: "user", content: retry.content } } } as ServerEvent);
      }
      else applyUserMessageQueueUpdate(queueFact(retry, "queued"));
    });
    expect(useAppStore.getState()).toMatchObject({ draft: "preserved newer draft", retainedInputEdit: undefined, retainedComposerInputs: {} });
    expect(useAppStore.getState().messages.filter(message => message.role === "user").map(message => message.id)).toEqual([retry.user_message_id]);
    expect((await coldRecords()).some(record => record.retainedInput)).toBe(false);
    view.unmount();
  });

  it.each(["native", "template"] as const)("consumes a late accepted %s slash input using its original owner and attachment semantics", async kind => {
    const { attachment } = fileDraft();
    const original = kind === "native" ? "/goal late native task" : "/review original template task";
    useAppStore.setState({ draft: original, attachments: [attachment],
      slashCommands: kind === "native" ? [] : [{ name: "review", command: "review", label: "/review", description: "Review", type: "template" }] });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    await act(async () => resetPendingCommandResultsForTests());
    expect(useAppStore.getState().draft).toBe(original);
    await act(async () => handleCommandResultEvent(kind === "native" ? nativeReceipt(command, "completed") : receipt(command, { admitted: true })));
    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().attachments).toEqual(kind === "native" ? [attachment] : []);
    expect(wire.commands).toHaveLength(1);
  });

  it("a result without admission facts preserves the real started turn without restoring its received draft", async () => {
    useAppStore.setState({ draft: "unknown semantic receipt" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    act(() => handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "unknown-run", conversation_id: "main",
      message_id: command.assistant_message_id, turn_id: "unknown-run" } as ServerEvent));
    await act(async () => handleCommandResultEvent(receipt(command, {})));
    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().messages.find(message => message.id === command.assistant_message_id)).toMatchObject({ isStreaming: true });
    expect(useAppStore.getState().messages.find(message => message.id === command.assistant_message_id)?.terminalStatus).toBeUndefined();
    expect(wire.commands).toHaveLength(1);
    expect(wire.toast).toHaveBeenCalledWith(expect.stringContaining("准入结果尚未确认"), "warning", 6000);
    expect(wire.toast.mock.calls.some(([, level]) => level === "error")).toBe(false);
  });

  it("a confirmed side submission clears its retained hidden draft/selection while preserving another owner's input", async () => {
    const selectedContext = { text: "selected source", source: "source.ts", workspaceRoot: root,
      range: { startLineNumber: 1, startColumn: 1, endLineNumber: 2, endColumn: 1 } };
    useAppStore.setState({ draft: "main untouched", sideChats: { side: { id: "side", messages: [], isStreaming: false,
      workspaceRoot: root, draft: "side submitted", attachments: [], selectedContext } } });
    const view = render(<SideChatPanel />);
    await waitFor(() => expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false));
    submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    view.unmount();
    expect(useAppStore.getState().sideChats.side.draft).toBe("side submitted");
    await act(async () => handleCommandResultEvent(receipt(wire.commands[0], { admitted: true })));
    expect(useAppStore.getState().sideChats.side).toMatchObject({ draft: "", selectedContext: undefined });
    expect(useAppStore.getState().draft).toBe("main untouched");
    expect((await coldRecords()).some(record => record.owner.kind === "side")).toBe(false);
  });

  it.each(["refused", "disconnect-during-preparation"] as const)("retains a side owner's original file and selection through cold recovery after %s", async mode => {
    const { file, attachment } = fileDraft();
    const selectedContext = { text: "original side selection", source: "source.ts", workspaceRoot: root,
      range: { startLineNumber: 1, startColumn: 1, endLineNumber: 2, endColumn: 1 } };
    useAppStore.setState({ sideChats: { side: { id: "side", messages: [], isStreaming: false, workspaceRoot: root,
      draft: "side unconfirmed", attachments: [{ ...attachment, conversationId: "side" }], selectedContext } } });
    const view = render(<SideChatPanel />);
    await waitFor(() => expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false));
    if (mode === "disconnect-during-preparation") vi.mocked(buildContextNativeAttachments).mockImplementationOnce(async () => {
      useAppStore.setState({ isConnected: false });
      return { attachments: [], attachmentRefs: [], notes: "" };
    });
    submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    if (mode === "refused") await act(async () => handleCommandResultEvent(receipt(wire.commands[0], { admitted: false }, "error")));
    view.unmount();
    const recovered = (await coldRecords()).find(record => record.owner.kind === "side" && record.owner.id === "side")!;
    expect(recovered.draft.draft).toBe("side unconfirmed");
    expect(recovered.side?.selectedContext).toEqual(selectedContext);
    expect(await recovered.draft.attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
  });

  it.each(["offline", "lost", "error", "warning", "unknown"] as const)("retains native slash input and all original cold context after %s", async mode => {
    const { file, attachment } = fileDraft();
    const quote = { id: "native-quote", role: "assistant" as const, content: "原引用仍未交给模型" };
    const mention = { kind: "file" as const, name: "source.ts", path: "source.ts", workspaceRoot: root };
    useAppStore.setState({ draft: "/goal 原生目标 中文参数", attachments: [attachment], quotedMessage: quote, selectedMentions: [mention],
      slashCommands: [{ name: "goal", command: "goal", label: "/goal", description: "Goal", type: "local" }], isConnected: mode !== "offline" });
    if (mode === "offline") wire.accepted = false;
    const view = render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command.user_message_id).toBeTruthy(); expect(command.assistant_message_id).toBeTruthy();
    expect(useAppStore.getState().messages).toEqual([]);
    if (mode === "lost") await act(async () => resetPendingCommandResultsForTests());
    else if (mode !== "offline") await act(async () => handleCommandResultEvent(nativeReceipt(command,
      mode === "unknown" ? "unknown" : "refused", mode === "error" ? "error" : "warning")));
    view.unmount();
    const recovered = (await coldRecords()).find(record => record.owner.id === "main")!.draft;
    expect(recovered).toMatchObject({ draft: "/goal 原生目标 中文参数", quotedMessage: quote, selectedMentions: [mention] });
    expect(await recovered.attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
    expect(useAppStore.getState().messages).toEqual([]);
    expect(wire.commands).toHaveLength(1);
  });

  it.each([false, true])("native completion clears only its unchanged input; newer text=%s, original files/quotes/mentions remain cold", async newer => {
    const { file, attachment } = fileDraft();
    const quote = { id: "not-consumed", role: "assistant" as const, content: "保留引用" };
    const mention = { kind: "file" as const, name: "source.ts", path: "source.ts", workspaceRoot: root };
    useAppStore.setState({ draft: "/goal 只消费文字", attachments: [attachment], quotedMessage: quote, selectedMentions: [mention] });
    const view = render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    if (newer) fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "新写的文字" } });
    await act(async () => handleCommandResultEvent(nativeReceipt(wire.commands[0], "completed")));
    expect(useAppStore.getState().draft).toBe(newer ? "新写的文字" : "");
    expect(useAppStore.getState().messages).toEqual([]);
    view.unmount();
    const recovered = (await coldRecords()).find(record => record.owner.id === "main")!.draft;
    expect(recovered).toMatchObject({ draft: newer ? "新写的文字" : "", quotedMessage: quote, selectedMentions: [mention] });
    expect(await recovered.attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
  });

  it("native completion clears the original hidden input without touching its attachments or the active owner's text", async () => {
    const { attachment } = fileDraft();
    useAppStore.setState({ draft: "/goal hidden origin", attachments: [attachment],
      conversations: [{ id: "main", title: "old", workspaceRoot: root }, { id: "other", title: "new", workspaceRoot: "C:/other" }] });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    act(() => useAppStore.getState().switchConversation("other"));
    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "other authored draft" } });
    await act(async () => handleCommandResultEvent(nativeReceipt(wire.commands[0], "completed")));
    expect(useAppStore.getState().draft).toBe("other authored draft");
    expect(useAppStore.getState().conversationWorkbenchStates.main).toMatchObject({ draft: "", attachments: [attachment] });
    expect(useAppStore.getState().messages).toEqual([]);
  });

  it("a local catalog command waits past permission results then projects real model admission and clears consumed context", async () => {
    const { attachment } = fileDraft();
    useAppStore.setState({ draft: "/plan inspect real source", attachments: [attachment],
      quotedMessage: { id: "model-quote", role: "user", content: "actual quoted input" },
      slashCommands: [{ name: "plan", command: "plan", label: "/plan", description: "Plan", type: "local" }] });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command).toMatchObject({ attachments: [expect.objectContaining({ artifact_id: "artifact-original" })],
      quoted_message: { id: "model-quote", content: "actual quoted input" } });
    await act(async () => handleCommandResultEvent({ type: "command.result", command: "conversation.permission_mode.set",
      client_command_id: command.client_command_id, client_command_type: "user_message", level: "success", message: "",
      data: { conversation_id: "main", mode: "plan" } } as CommandResultEvent));
    expect(useAppStore.getState().draft).toBe("/plan inspect real source");
    expect(useAppStore.getState().messages).toEqual([]);
    await act(async () => { started(command); handleCommandResultEvent(receipt(command, { admitted: true })); });
    expect(useAppStore.getState().messages.map(message => message.id)).toEqual([command.user_message_id, command.assistant_message_id]);
    expect(useAppStore.getState().draft).toBe(""); expect(useAppStore.getState().attachments).toEqual([]);
    expect(await coldRecords()).toEqual([]);
  });

  it("a template catalog classification cannot cause native completion to consume its original attachments", async () => {
    const { attachment } = fileDraft();
    useAppStore.setState({ draft: "/review actual server-native override", attachments: [attachment],
      slashCommands: [{ name: "review", command: "review", label: "/review", description: "Review", type: "template" }] });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    await act(async () => handleCommandResultEvent(nativeReceipt(wire.commands[0], "completed")));
    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().attachments).toEqual([attachment]);
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().messages[0]).toMatchObject({ role: "user", deliveryStatus: "sent" });
  });

  it("a template prepared-prompt result retains the draft until the actual model input is admitted", async () => {
    useAppStore.setState({ draft: "/review real model task", slashCommands: [
      { name: "review", command: "review", label: "/review", description: "Review", type: "template" },
    ] });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    await act(async () => handleCommandResultEvent({ type: "command.result", command: "review", client_command_id: command.client_command_id,
      client_command_type: "user_message", level: "info", message: "Prepared template prompt for '/review'.",
      data: { conversation_id: "main" } } as CommandResultEvent));
    expect(useAppStore.getState().draft).toBe("/review real model task");
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().messages[0]).toMatchObject({ role: "user", deliveryStatus: "sending" });
    await act(async () => handleCommandResultEvent(receipt(command, { admitted: true })));
    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().messages[0]).toMatchObject({ id: command.user_message_id, role: "user" });
  });

  it.each(["completed", "refused", "unknown"] as const)("side slash %s preserves the unconsumed File/mention/selection and clears only confirmed text", async completion => {
    const { file, attachment } = fileDraft();
    const selectedContext = { text: "side original source", source: "source.ts", workspaceRoot: root,
      range: { startLineNumber: 1, startColumn: 1, endLineNumber: 2, endColumn: 1 } };
    const contextRef = { kind: "file" as const, name: "source.ts", path: "source.ts", workspaceRoot: root };
    useAppStore.setState({ draft: "main untouched", sideChats: { side: { id: "side", messages: [], isStreaming: false,
      workspaceRoot: root, draft: "/goal side original", attachments: [{ ...attachment, conversationId: "side" }],
      selectedContext, contextRefs: [contextRef] } } });
    const view = render(<SideChatPanel />);
    await waitFor(() => expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false));
    submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    await act(async () => handleCommandResultEvent(nativeReceipt(wire.commands[0], completion, completion === "completed" ? "success" : "warning")));
    expect(useAppStore.getState().draft).toBe("main untouched");
    expect(useAppStore.getState().sideChats.side.messages).toHaveLength(1);
    expect(useAppStore.getState().sideChats.side.messages[0]).toMatchObject({ role: "user" });
    view.unmount();
    const recovered = (await coldRecords()).find(record => record.owner.kind === "side" && record.owner.id === "side")!;
    expect(recovered.draft.draft).toBe(completion === "completed" ? "" : "/goal side original");
    expect(recovered.side).toMatchObject({ selectedContext, contextRefs: [contextRef] });
    expect(await recovered.draft.attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
  });
});
