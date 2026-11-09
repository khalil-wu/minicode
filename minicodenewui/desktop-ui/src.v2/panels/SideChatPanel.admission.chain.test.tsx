/* @vitest-environment jsdom */
import { File as NativeFile } from "node:buffer";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SideChatPanel } from "./SideChatPanel";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import { emptyComposerDraft, flushComposerDrafts, loadComposerDrafts, resetComposerDraftStorageForTests } from "../stores/composer-drafts";
import { registerWebSocketSender, resetPendingCommandResultsForTests } from "../protocol/ws-outbox";
import { resetPendingUserMessagesForTests } from "../chat/sendChatMessage";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { applyUserMessageQueueUpdate } from "../chat/sessionEvents";
import type { ClientCommand, CommandResultEvent, ServerEvent, UserMessageCommand } from "../protocol/events";
import type { CodeSelectionContext, ComposerAttachment, MessageContextRef } from "../stores/types";

const wire = vi.hoisted(() => ({ commands: [] as UserMessageCommand[], toast: vi.fn() }));
vi.hoisted(() => Object.defineProperty(window, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "side-admission-session",
  send: () => true, subscribe: () => () => {},
}) }));
vi.mock("../composer/RichComposerEditor", async () => ({ RichComposerEditor: (await import("../../test/literal-composer-editor")).LiteralComposerEditor }));
vi.mock("../composer/contextPayload", () => ({ buildContextPayload: vi.fn(async () => ""),
  buildContextNativeAttachments: vi.fn(async () => ({ attachments: [], attachmentRefs: [], notes: "" })),
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: wire.toast }));

const root = "C:/side-admission", owner = "side-late";
const selection: CodeSelectionContext = { text: "original selected context", source: "original.ts", workspaceRoot: root };
const reference: MessageContextRef = { kind: "file", name: "original.ts", path: "original.ts", workspaceRoot: root };
const rich = (text: string) => ({ markdown: `**${text}**`, plainTextMode: false,
  document: { type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text, marks: [{ type: "strong" }] }] }] },
});
const attachment = (id: string): ComposerAttachment => ({ id, name: `${id}.png`, type: "image/png", size: 3,
  status: "ready", conversationId: owner, workspaceRoot: root, artifactId: `artifact-${id}`, dataUrl: `blob:${id}`,
  localFile: new File([new Uint8Array([0, 128, 255])], `${id}.png`, { type: "image/png", lastModified: 123 }),
  attachment: { id, artifact_id: `artifact-${id}`, file_name: `${id}.png`, media_type: "image/png", kind: "image" },
});
const result = (command: UserMessageCommand, data: Record<string, unknown>, level = "success"): CommandResultEvent => ({
  type: "command.result", command: "user_message", client_command_id: command.client_command_id,
  level, message: level === "error" ? "actual side rejection" : "", data: { conversation_id: owner,
    user_message_id: command.user_message_id, assistant_message_id: command.assistant_message_id, ...data },
});
const submit = () => fireEvent.click(screen.getByRole("button", { name: /^(发送|将消息加入队列)$/ }));
const prepare = async (streaming = false) => {
  const initialAttachment = attachment("original-side");
  const initialRich = rich("original side input");
  useAppStore.setState({ sideChats: { [owner]: { id: owner, workspaceRoot: root, parentConversationId: "main",
    messages: streaming ? [{ id: "previous-side-run", role: "assistant", content: "existing side work", timestamp: 1, artifacts: [], isStreaming: true }] : [],
    isStreaming: streaming, draft: initialRich.markdown, richDraft: initialRich,
    attachments: [initialAttachment], selectedContext: selection, contextRefs: [reference] } }, activeSideChatId: owner });
  const view = render(<SideChatPanel />);
  await waitFor(() => expect((screen.getByRole("button", { name: /^(发送|将消息加入队列)$/ }) as HTMLButtonElement).disabled).toBe(false));
  submit(); await waitFor(() => expect(wire.commands).toHaveLength(1));
  return { view, initialAttachment, initialRich, command: wire.commands[0] };
};

beforeEach(async () => {
  await waitComposerDraftRecovery();
  vi.stubGlobal("File", NativeFile);
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn(() => "blob:restored-side-preview");
    static revokeObjectURL = vi.fn();
  });
  wire.commands = []; wire.toast.mockClear(); resetPendingUserMessagesForTests();
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: "main", workingDirectory: root,
    conversations: [{ id: "main", title: "Main", workspaceRoot: root }], messages: [], conversationMessages: {},
    conversationStreaming: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, retainedComposerInputs: {},
    activeConversationDraftId: null, conversationDrafts: {}, sideChats: {}, activeSideChatId: null, sideChatPendingContext: null,
    draft: "untouched main draft", isConnected: true, isStreaming: false, composerDraftsReady: true, composerPlainTextMode: true,
    pendingConversationSwitchId: null, pendingConversationCreateId: null, pendingPermissionModeChanges: {},
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [],
    runtimeSession: null, turnDiffs: {}, followUpBehavior: "queue", sendShortcut: "enter" });
  await flushComposerDrafts(); await resetComposerDraftStorageForTests();
  registerWebSocketSender((command: ClientCommand) => {
    if (command.type === "user_message") wire.commands.push(command);
    if (command.type === "conversation.create" || command.type === "conversation.delete") queueMicrotask(() => handleCommandResultEvent({
      type: "command.result", command: command.type, client_command_id: command.client_command_id, level: "success", message: "",
      data: { conversation_id: command.conversation_id, ...(command.type === "conversation.create" ? { side_chat_transcript: [] } : { deleted: true }) },
    }));
    return true;
  });
});
afterEach(async () => {
  cleanup();
  await act(async () => resetPendingCommandResultsForTests());
  registerWebSocketSender(null); resetPendingUserMessagesForTests();
  await flushComposerDrafts(); await resetComposerDraftStorageForTests(true);
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

it("consumes a late accepted side input after unmount while preserving every newer authored field", async () => {
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  const { view, initialAttachment, command } = await prepare();
  const newerRich = rich("newer side input"), newerAttachment = attachment("newer-side");
  const newerSelection = { ...selection, text: "new selected context" };
  const newerReference: MessageContextRef = { ...reference, path: "new.ts", name: "new.ts" };
  act(() => useAppStore.setState(state => ({ sideChats: { ...state.sideChats, [owner]: { ...state.sideChats[owner],
    draft: newerRich.markdown, richDraft: newerRich, selectedContext: newerSelection,
    contextRefs: [reference, newerReference], attachments: [initialAttachment, newerAttachment],
  } } })));
  await act(async () => resetPendingCommandResultsForTests());
  view.unmount();
  act(() => { handleCommandResultEvent(result(command, { admitted: true })); handleCommandResultEvent(result(command, { admitted: true })); });
  expect(useAppStore.getState().sideChats[owner]).toMatchObject({ draft: newerRich.markdown, richDraft: newerRich,
    selectedContext: newerSelection, contextRefs: [newerReference], attachments: [newerAttachment] });
  expect(revoke.mock.calls).toEqual([["blob:original-side"]]);
  expect(useAppStore.getState().draft).toBe("untouched main draft");
  await flushComposerDrafts();
  const saved = (await loadComposerDrafts()).find(record => record.owner.kind === "side" && record.owner.id === owner)!;
  expect(saved.draft.richDraft).toEqual(newerRich);
  expect(new Uint8Array(await saved.draft.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([0, 128, 255]));
});

it("consumes only the original body on late native completion and retains its files and context", async () => {
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  const { view, initialAttachment, command } = await prepare();
  await act(async () => resetPendingCommandResultsForTests()); view.unmount();
  act(() => handleCommandResultEvent(result(command, { admitted: false, native_completion: "completed" })));
  expect(useAppStore.getState().sideChats[owner]).toMatchObject({ draft: "", richDraft: undefined,
    attachments: [initialAttachment], selectedContext: selection, contextRefs: [reference] });
  expect(revoke).not.toHaveBeenCalled();
});

it("keeps a newer rich body when the original native operation completes late", async () => {
  const { command } = await prepare();
  await act(async () => resetPendingCommandResultsForTests());
  const newerRich = rich("newer native body");
  act(() => useAppStore.getState().setSideChatDraft(owner, newerRich.markdown, newerRich));
  act(() => handleCommandResultEvent(result(command, { admitted: false, native_completion: "completed" })));
  expect(useAppStore.getState().sideChats[owner]).toMatchObject({ draft: newerRich.markdown, richDraft: newerRich });
});

it.each(["unknown", "refused"])("keeps the side draft and files when the late native outcome is %s", async completion => {
  const { view, command, initialAttachment, initialRich } = await prepare();
  await act(async () => resetPendingCommandResultsForTests()); view.unmount();
  act(() => handleCommandResultEvent(result(command, { admitted: false, native_completion: completion }, completion === "refused" ? "error" : "warning")));
  expect(useAppStore.getState().sideChats[owner]).toMatchObject({ draft: initialRich.markdown, richDraft: initialRich,
    attachments: [initialAttachment], selectedContext: selection, contextRefs: [reference] });
});

it("a real queue fact consumes the captured input without a result and its late result cannot release the next submission", async () => {
  const { command } = await prepare(true);
  act(() => applyUserMessageQueueUpdate({ type: "user_message.queue.updated", conversation_id: owner, status: "queued",
    message_id: command.assistant_message_id!, user_message_id: command.user_message_id, position: 1 }));
  expect(useAppStore.getState().sideChats[owner].draft).toBe("");
  act(() => useAppStore.getState().setSideChatDraft(owner, "second side input"));
  await waitFor(() => expect((screen.getByRole("button", { name: "将消息加入队列" }) as HTMLButtonElement).disabled).toBe(false));
  submit(); await waitFor(() => expect(wire.commands).toHaveLength(2));
  const second = wire.commands[1];
  expect((screen.getByRole("button", { name: "将消息加入队列" }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => handleCommandResultEvent(result(command, { admitted: false, queued: true })));
  expect((screen.getByRole("button", { name: "将消息加入队列" }) as HTMLButtonElement).disabled).toBe(true);
  act(() => applyUserMessageQueueUpdate({ type: "user_message.queue.updated", conversation_id: owner, status: "queued",
    message_id: second.assistant_message_id!, user_message_id: second.user_message_id, position: 1 }));
  expect(useAppStore.getState().sideChats[owner].draft).toBe("");
});

it("a pre-admission start shows the run but cannot consume an input later cancelled before commit", async () => {
  const { command, initialAttachment, initialRich } = await prepare();
  act(() => handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "side-pre-admission", conversation_id: owner,
    message_id: command.assistant_message_id, turn_id: "side-pre-admission" } as ServerEvent));
  expect(useAppStore.getState().sideChats[owner]).toMatchObject({ isStreaming: true, draft: initialRich.markdown,
    richDraft: initialRich, attachments: [initialAttachment], selectedContext: selection, contextRefs: [reference] });
  await act(async () => handleCommandResultEvent(result(command, { admitted: false, cancelled: true, reason: "user_interrupted" })));
  expect(useAppStore.getState().sideChats[owner]).toMatchObject({ draft: initialRich.markdown, richDraft: initialRich,
    attachments: [initialAttachment], selectedContext: selection, contextRefs: [reference] });
});

it("does not recreate a deleted side owner when its success arrives late", async () => {
  const { view, command } = await prepare();
  await act(async () => resetPendingCommandResultsForTests()); view.unmount();
  act(() => handleCommandResultEvent({ type: "command.result", command: "conversation.delete", level: "success", message: "",
    data: { conversation_id: owner, deleted: true } }));
  act(() => handleCommandResultEvent(result(command, { admitted: true })));
  expect(useAppStore.getState().sideChats[owner]).toBeUndefined();
  expect(useAppStore.getState().conversationMessages[owner]).toBeUndefined();
});
