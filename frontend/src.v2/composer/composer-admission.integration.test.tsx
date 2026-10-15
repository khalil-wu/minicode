/* @vitest-environment jsdom */
import { File as NativeFile } from "node:buffer";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import { emptyComposerDraft, flushComposerDrafts, loadComposerDrafts, resetComposerDraftStorageForTests } from "../stores/composer-drafts";
import { registerWebSocketSender, resetPendingCommandResultsForTests } from "../protocol/ws-outbox";
import { resetPendingUserMessagesForTests, sendChatMessage } from "../chat/sendChatMessage";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import { applyUserMessageQueueUpdate } from "../chat/sessionEvents";
import { handleChatStreamEvent } from "../chat/chatStreamEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { handleNoticeEvent } from "../chat/noticeEvents";
import { Composer } from "./Composer";
import { MessageList } from "../chat/MessageList";
import { acknowledgeClientCommand } from "../hooks/useWebSocket";
import { SideChatPanel } from "../panels/SideChatPanel";
import { buildContextNativeAttachments } from "./contextPayload";
import type { ClientCommand, CommandResultEvent, ServerEvent, UserMessageCommand, UserMessageQueueUpdatedEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";

const wire = vi.hoisted(() => ({ commands: [] as UserMessageCommand[], accepted: true, toast: vi.fn() }));
vi.hoisted(() => Object.defineProperty(window, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../hooks/useWebSocket", async load => ({ ...await load<typeof import("../hooks/useWebSocket")>(), getWebSocket: () => ({ sessionId: "draft-admission-session", send: (command: ClientCommand) => {
  if (command.type === "user_message") wire.commands.push(command);
  return wire.accepted;
} }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: wire.toast }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  fetchWithTimeout: vi.fn(async () => new Response('{"plugins":[]}', { status: 200 })) }));
vi.mock("./contextPayload", () => ({
  buildContextPayload: vi.fn(async () => ""),
  buildContextNativeAttachments: vi.fn(async () => ({ attachments: [], attachmentRefs: [], notes: "" })),
}));
vi.mock("../protocol/code-review", () => ({
  fetchCodeReviewOptions: vi.fn(async (_owner: string, cwd: string) => ({ cwd, gitRoot: cwd, currentBranch: "feature", branches: ["main"] })),
  prepareCodeReview: vi.fn(async (_owner: string, cwd: string, mode: "uncommitted" | "base-branch", baseBranch: string | undefined, _sha: string | undefined, requestMessage: string) => ({
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
  wire.commands = []; wire.accepted = true; wire.toast.mockClear(); resetPendingUserMessagesForTests();
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: "main", workingDirectory: root,
    conversations: [{ id: "main", title: "owned", workspaceRoot: root }], messages: [], conversationMessages: {},
    conversationStreaming: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, sideChats: {},
    sideChatPendingContext: null, isConnected: true, isStreaming: false, runtimeSession: null,
    currentProvider: "custom", currentModel: "fixture-model", availableModels: ["fixture-model"],
    availableSkills: [], slashCommands: [], selectedMentions: [], selectedSkills: [], pendingPermissionModeChanges: {},
    pendingConversationSwitchId: null, pendingConversationCreateId: null, pendingApproval: null, approvalQueue: [],
    pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], turnDiffs: {},
    plan: null, todos: [], subagents: [], agentProgress: [], conversationAgentStates: {},
    slashPanelOpen: false, mentionPanelOpen: false, sendShortcut: "enter", followUpBehavior: "queue" });
  registerWebSocketSender(command => {
    if (command.type === "user_message") wire.commands.push(command);
    if (command.type === "conversation.create") handleCommandResultEvent({ type: "command.result", command: "conversation.create",
      client_command_id: command.client_command_id, level: "success", message: "", data: { conversation_id: command.conversation_id } });
    return wire.accepted;
  });
});
afterEach(async () => {
  cleanup();
  await act(async () => resetPendingCommandResultsForTests());
  registerWebSocketSender(null);
  await resetComposerDraftStorageForTests();
  vi.restoreAllMocks(); vi.unstubAllGlobals(); delete window.__MINICODE_RUNTIME__;
});

describe("actual composer durable admission boundary", () => {
  it.each(["enter", "cmdIfMultiline", "mod-enter"] as const)("sends Source's opposite running follow-up from the actual main composer for %s without changing global preferences", async shortcut => {
    const running = { id: "running-answer", role: "assistant" as const, content: "running", timestamp: 1, artifacts: [], isStreaming: true };
    useAppStore.setState({ messages: [running], isStreaming: true, draft: "authored follow-up\nsecond line", sendShortcut: shortcut, followUpBehavior: "queue" });
    render(<Composer />);
    const input = screen.getByRole("textbox", { name: "消息输入" });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, shiftKey: shortcut !== "enter" });
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(command).toMatchObject({ conversation_id: "main", content: "authored follow-up\nsecond line", streaming_behavior: "steer" });
    expect(useAppStore.getState()).toMatchObject({ followUpBehavior: "queue", sendShortcut: shortcut, draft: "authored follow-up\nsecond line" });
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
    fireEvent.keyDown(document, { key: "Escape" });
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

  it.each(["uncommitted", "base-branch"] as const)("review %s keeps attachments and durable mode until its ordinary input is admitted", async mode => {
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

  const received = (command: UserMessageCommand) => acknowledgeClientCommand({ type: "client.command.ack",
    client_command_id: command.client_command_id, command_type: "user_message" } as ServerEvent);

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

  it("allows a second authored input after receipt without waiting for the first model admission", async () => {
    useAppStore.setState({ draft: "同样的话" });
    render(<Composer />); submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
    const first = wire.commands[0]; act(() => received(first));
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

  it.each(["offline", "refused"] as const)("retains %s input and original File bytes through a cold reopen", async mode => {
    const { file, attachment } = fileDraft();
    const quote = { id: "quoted", role: "assistant" as const, content: "原引用" };
    const mention = { kind: "file" as const, name: "source.ts", path: "source.ts", workspaceRoot: root };
    useAppStore.setState({ draft: "尚未持久准入的原输入", attachments: [attachment], quotedMessage: quote, selectedMentions: [mention], isConnected: mode !== "offline" });
    const view = render(<Composer />);
    submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    expect(useAppStore.getState().draft).toBe("尚未持久准入的原输入");
    expect(useAppStore.getState().messages).toEqual([]);
    if (mode === "refused") await act(async () => handleCommandResultEvent(receipt(command, { admitted: false }, "error")));
    view.unmount();
    const records = await coldRecords();
    const restored = records.find(record => record.owner.id === "main")!.draft;
    expect(restored).toMatchObject({ draft: "尚未持久准入的原输入", quotedMessage: quote, selectedMentions: [mention] });
    expect(await restored.attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
    expect(restored.attachments[0].localFile?.name).toBe("中文 空格文件.txt");
  });

  it.each([{ admitted: true }, { admitted: false, queued: true }])("clears only after durable $admitted/$queued and cannot cold resurrect sent text or files", async fact => {
    const { attachment } = fileDraft();
    useAppStore.setState({ draft: "received input", attachments: [attachment], quotedMessage: { id: "q", role: "user", content: "quote" } });
    const view = render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    expect(useAppStore.getState().draft).toBe("received input");
    await act(async () => handleCommandResultEvent(receipt(wire.commands[0], fact)));
    await waitFor(() => expect(useAppStore.getState().draft).toBe(""));
    expect(useAppStore.getState().attachments).toEqual([]);
    view.unmount();
    expect(await coldRecords()).toEqual([]);
  });

  it.each([false, true])("a first CID receipt preserves newer input=%s and binds before immediate item/delta frames", async newer => {
    useAppStore.setState({ conversationId: null, conversations: [], draft: "first submitted", attachments: [] });
    render(<Composer />); submit();
    await waitFor(() => expect(wire.commands).toHaveLength(1));
    const command = wire.commands[0];
    if (newer) fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "newly authored" } });
    act(() => {
      started(command, "first-owner");
      handleCommandResultEvent(receipt(command, { admitted: true, conversation_id: "first-owner" }));
      itemFrames(command, "first-owner");
    });
    expect(useAppStore.getState().conversationId).toBe("first-owner");
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
    const running = { id: "running-answer", role: "assistant" as const, content: "running", timestamp: 1, artifacts: [], isStreaming: true };
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
    expect(useAppStore.getState().messages).toEqual([]);
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
    expect(useAppStore.getState().draft).toBe("unconfirmed input");
    expect(useAppStore.getState().messages).toEqual([]);
    expect(wire.commands).toHaveLength(1);
    expect(wire.toast).toHaveBeenCalledWith(expect.stringContaining("接收尚未确认"), "warning", 6000);
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
    expect(useAppStore.getState().messages).toEqual([]);
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
    expect(useAppStore.getState().messages).toEqual([]);
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
    expect(useAppStore.getState().sideChats.side.messages).toEqual([]);
    view.unmount();
    const recovered = (await coldRecords()).find(record => record.owner.kind === "side" && record.owner.id === "side")!;
    expect(recovered.draft.draft).toBe(completion === "completed" ? "" : "/goal side original");
    expect(recovered.side).toMatchObject({ selectedContext, contextRefs: [contextRef] });
    expect(await recovered.draft.attachments[0].localFile!.arrayBuffer()).toEqual(await file.arrayBuffer());
  });
});
