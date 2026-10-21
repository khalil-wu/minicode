/* @vitest-environment jsdom */
import { File as NativeFile } from "node:buffer";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import { registerWebSocketSender, resolveClientCommandResult, resetPendingCommandResultsForTests } from "../protocol/ws-outbox";
import type { ClientCommand, CommandResultEvent } from "../protocol/events";
import type { ChatMessage } from "../stores/types";
import { applyUserMessageQueueUpdate } from "../chat/sessionEvents";
import { sendChatMessage } from "../chat/sendChatMessage";
import { handleCommandCatalogEvent } from "../chat/commandCatalogEvents";
import { beginQueuedMessageEdit, cancelQueuedMessageEdit, deleteQueuedMessage, installQueuedEditRecovery } from "./queued-message-edit";
import { dispatchQueuedMessageHistory, resetQueuedMessageHistoryForTests, waitForQueuedMessageHistory } from "./queued-message-undo";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";
import { ComposerTextarea } from "./ComposerTextarea";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { CommandPalette } from "../overlays/CommandPalette";
import { QueuedMessageList } from "./QueuedMessageList";
import { collectComposerDrafts, emptyComposerDraft, flushComposerDrafts, loadComposerDrafts, persistComposerDrafts } from "../stores/composer-drafts";
import { clearDiffCommentScope, getDiffCommentScope, readDiffCommentState, setDiffComments } from "../lib/diff-comment-storage";

const wire = vi.hoisted(() => ({ commands: [] as ClientCommand[], toast: vi.fn() }));
vi.hoisted(() => Object.defineProperty(window, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: wire.toast }));
const owner = "queue-edit-owner";
const root = "C:/queue-edit-workspace";
const first: ChatMessage = { id: "queued-user-one", role: "user", content: "queued request", backendContent: "original serialized request",
  timestamp: 1, artifacts: [], queueState: "queued", queueMessageId: "queued-assistant-one", queuePosition: 1,
  quotedMessage: { id: "quoted", role: "assistant", content: "original quote" },
  attachmentRefs: [{ id: "attachment-one", name: "文件.txt", kind: "document", mediaType: "text/plain", artifactId: "original-artifact", sizeBytes: 10 }],
  contextRefs: [{ kind: "skill", name: "original-skill", path: "C:/skills/original/SKILL.md" }],
};
const second: ChatMessage = { id: "queued-user-two", role: "user", content: "second request", timestamp: 2, artifacts: [],
  queueState: "queued", queueMessageId: "queued-assistant-two", queuePosition: 2 };
let queue: ChatMessage[];
let pending: (() => void)[];
let hold = false;
const acknowledge = (command: ClientCommand, data: Record<string, unknown>, level = "success") => resolveClientCommandResult({
  type: "command.result", command: command.type, client_command_id: command.client_command_id,
  level, message: level === "warning" ? "Queued message is no longer available" : "", data: { conversation_id: owner, ...data },
} as CommandResultEvent);

const publish = () => queue.forEach((message, index) => applyUserMessageQueueUpdate({
  type: "user_message.queue.updated", status: "queued", conversation_id: owner,
  message_id: message.queueMessageId!, user_message_id: message.id, position: index + 1,
  input: { content: message.backendContent ?? message.content, display_content: message.content,
    attachments: message.attachmentRefs?.map(ref => ({ id: ref.id, file_name: ref.name, media_type: ref.mediaType,
      kind: ref.kind, artifact_id: ref.artifactId, size_bytes: ref.sizeBytes ?? 0 })),
    context_refs: message.contextRefs, quoted_message: message.quotedMessage },
}));

beforeEach(async () => {
  await waitComposerDraftRecovery();
  vi.stubGlobal("File", NativeFile);
  wire.commands = []; wire.toast.mockClear(); hold = false; pending = [];
  resetQueuedMessageHistoryForTests();
  queue = [{ ...first }, { ...second }];
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: owner, workingDirectory: root,
    conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, composerDraftsReady: true,
    messages: [{ id: "actual-active-assistant", role: "assistant", content: "working", artifacts: [], timestamp: 0,
      isStreaming: true, turnId: "actual-active-turn" }, ...queue.map(message => ({ ...message }))],
    conversationMessages: {}, pendingConversationSwitchId: null,
    isStreaming: true, isConnected: true, conversations: [{ id: owner, title: "Original", updatedAt: "now", workspaceRoot: root }] });
  useAppStore.setState({ shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS }, settingsOpen: false, shortcutsHelpOpen: false,
    commandPaletteOpen: false, quickOpenVisible: false, liveArtifactsOpen: false, agentEditorOpen: false });
  clearDiffCommentScope(getDiffCommentScope(owner, root));
  registerWebSocketSender(command => {
    wire.commands.push(command);
    if (!["user_message.queue.cancel", "user_message.queue.add", "user_message.queue.reorder"].includes(command.type)) return true;
    const execute = () => {
      if (command.type === "user_message.queue.cancel") {
        if (!queue.some(message => message.queueMessageId === command.message_id)) { acknowledge(command, { cancelled: false }, "warning"); return; }
        queue = queue.filter(message => message.queueMessageId !== command.message_id);
        applyUserMessageQueueUpdate({ type: "user_message.queue.updated", status: "cancelled", conversation_id: owner,
          message_id: command.message_id, user_message_id: command.user_message_id });
        acknowledge(command, { cancelled: true });
      } else if (command.type === "user_message.queue.add") {
        queue.push({ id: command.user_message_id, role: "user", content: command.display_content ?? command.content,
          backendContent: command.content, timestamp: 3, artifacts: [], queueState: "queued", queueMessageId: command.message_id,
          contextRefs: command.context_refs, quotedMessage: command.quoted_message,
          attachmentRefs: command.attachments?.map(ref => ({ id: String(ref.id), name: String(ref.file_name), kind: "document",
            mediaType: String(ref.media_type), artifactId: String(ref.artifact_id), sizeBytes: Number(ref.size_bytes ?? 0) })) });
        publish(); acknowledge(command, { queued: true, message_id: command.message_id, assistant_message_id: command.message_id, user_message_id: command.user_message_id });
      } else if (command.type === "user_message.queue.reorder") {
        expect([...command.message_ids].sort()).toEqual(queue.map(message => message.queueMessageId).sort());
        queue = command.message_ids.map(id => queue.find(message => message.queueMessageId === id)!);
        publish(); acknowledge(command, {});
      }
    };
    if (hold) pending.push(execute); else queueMicrotask(execute);
    return true;
  });
});

afterEach(async () => {
  cleanup(); registerWebSocketSender(null); resetPendingCommandResultsForTests();
  resetQueuedMessageHistoryForTests();
  useAppStore.setState({ queuedEdit: undefined });
  await flushComposerDrafts();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("moves a queued input into the original composer and Escape restores its position and the complete prior draft", async () => {
  const localFile = new File(["unsaved original bytes"], "未发送.txt", { type: "text/plain" });
  const backup = { ...emptyComposerDraft(), draft: "existing unsent draft", attachments: [{ id: "local-file", name: localFile.name,
    type: localFile.type, size: localFile.size, status: "uploading" as const, localFile }],
    selectedMentions: [{ kind: "file" as const, name: "before.ts", path: "before.ts" }] };
  useAppStore.setState(backup);
  const scope = getDiffCommentScope(owner, root);
  setDiffComments(scope, [{ type: "comment", position: { path: "before.ts", side: "right", line: 1 },
    content: [{ content_type: "text", text: "prior unsent comment" }] }]);
  await beginQueuedMessageEdit(owner, first);
  expect(queue.map(message => message.queueMessageId)).toEqual([second.queueMessageId]);
  expect(useAppStore.getState()).toMatchObject({ draft: first.content, queuedEdit: { ownerId: owner },
    attachments: [{ artifactId: "original-artifact", conversationId: owner, status: "ready" }],
    selectedSkills: [{ name: "original-skill" }], quotedMessage: first.quotedMessage });
  const edit = useAppStore.getState().queuedEdit!;
  persistComposerDrafts(collectComposerDrafts(useAppStore.getState(), 100)); await flushComposerDrafts();
  const cold = (await loadComposerDrafts()).find(record => record.owner.id === owner)!.draft.queuedEdit!;
  expect(cold.backup.draft).toBe("existing unsent draft");
  expect(cold.backup.attachments[0].localFile!.name).toBe(localFile.name);
  expect(await cold.backup.attachments[0].localFile!.text()).toBe("unsaved original bytes");
  useAppStore.setState({ draft: "edited body" });
  expect(await cancelQueuedMessageEdit()).toBe(true);
  expect(queue.map(message => message.queueMessageId)).toEqual([first.queueMessageId, second.queueMessageId]);
  expect(useAppStore.getState().draft).toBe("existing unsent draft");
  expect(useAppStore.getState().attachments[0].localFile).toBe(localFile);
  expect(readDiffCommentState(scope).comments[0].content[0].text).toBe("prior unsent comment");
  expect(useAppStore.getState().queuedEdit).toBe(edit.backup.queuedEdit);
  expect(wire.commands.some(command => command.type === "user_message")).toBe(false);
});

it("saves the edited input through the durable queue without starting a model turn and restores the earlier draft", async () => {
  useAppStore.setState({ draft: "return to this original draft" });
  await beginQueuedMessageEdit(owner, first);
  const captured = useAppStore.getState().queuedEdit!;
  useAppStore.setState({ draft: "actual edited text" });
  const result = await sendChatMessage({ queuedEdit: captured, conversationId: owner, displayContent: "actual edited text",
    backendContent: "actual model-visible edit", attachments: useAppStore.getState().attachments.map(attachment => attachment.attachment!),
    contextRefs: first.contextRefs, quotedMessage: first.quotedMessage,
    onReceived: () => useAppStore.setState({ draft: "", attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [] }),
  });
  expect(result).toBe(true);
  expect(queue.map(message => message.content)).toEqual(["actual edited text", second.content]);
  expect(queue[0].backendContent).toBe("actual model-visible edit");
  expect(queue[0].attachmentRefs?.[0].artifactId).toBe("original-artifact");
  expect(useAppStore.getState().draft).toBe("return to this original draft");
  expect(useAppStore.getState().isStreaming).toBe(true);
  expect(wire.commands.some(command => command.type === "user_message")).toBe(false);
});

it("returns a removed message if navigation or new typing supersedes preparation without replacing B", async () => {
  hold = true;
  const operation = beginQueuedMessageEdit(owner, first);
  useAppStore.setState({ conversationId: "B", draft: "new B text" });
  hold = false; pending.splice(0).forEach(execute => execute());
  await operation;
  expect(queue.map(message => message.queueMessageId)).toEqual([first.queueMessageId, second.queueMessageId]);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", draft: "new B text", queuedEdit: undefined });
});

it("retains a newer draft typed during acceptance and returns its predecessor only after that newer input is consumed", async () => {
  useAppStore.setState({ draft: "prior unsent input" });
  await beginQueuedMessageEdit(owner, first);
  const edit = useAppStore.getState().queuedEdit!;
  const unsubscribe = installQueuedEditRecovery();
  hold = true;
  const saving = sendChatMessage({ queuedEdit: edit, conversationId: owner, displayContent: "saved edit", backendContent: "saved edit" });
  useAppStore.setState({ draft: "newer input before receipt", attachments: [], quotedMessage: null, selectedSkills: [] });
  hold = false; pending.splice(0).forEach(execute => execute());
  expect(await saving).toBe(true);
  expect(useAppStore.getState().draft).toBe("newer input before receipt");
  expect(useAppStore.getState().queuedEdit?.completed).toBe(true);
  useAppStore.setState({ draft: "" });
  await waitFor(() => expect(useAppStore.getState().draft).toBe("prior unsent input"));
  unsubscribe();
});

it("exposes the source edit action and semantic drag handle through the existing numbered queue rows", async () => {
  render(<QueuedMessageList />);
  fireEvent.click(screen.getByRole("button", { name: "排队消息 2 的更多操作" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "编辑" }));
  await waitFor(() => expect(useAppStore.getState().draft).toBe(first.content));
  expect(screen.getByRole("button", { name: "队列第 2 项" })).toBeTruthy();
});

it("global skillsChanged requests fresh catalogs for the selected owner without fabricating catalog contents", () => {
  useAppStore.setState({ availableSkills: [{ name: "retained", description: "existing", active: false }] });
  expect(handleCommandCatalogEvent({ type: "skills/changed" })).toBe(true);
  expect(wire.commands.map(command => command.type)).toEqual(["skills.list", "commands.list", "runtime.capabilities.inspect"]);
  expect(wire.commands.every(command => command.conversation_id === owner)).toBe(true);
  expect(useAppStore.getState().availableSkills[0].name).toBe("retained");
});

it("undo and redo a deletion through its original identity without replacing the current draft", async () => {
  useAppStore.setState({ draft: "still unsent" });
  await deleteQueuedMessage(owner, first);
  expect(queue.map(message => message.id)).toEqual([second.id]);
  expect(dispatchQueuedMessageHistory("undo")).toBe(true);
  await waitForQueuedMessageHistory();
  expect(queue.map(message => message.id)).toEqual([first.id, second.id]);
  expect(queue[0].attachmentRefs?.[0].artifactId).toBe("original-artifact");
  expect(useAppStore.getState().draft).toBe("still unsent");
  expect(wire.toast).toHaveBeenCalledWith("排队消息已恢复", "success");
  expect(dispatchQueuedMessageHistory("redo")).toBe(true);
  await waitForQueuedMessageHistory();
  expect(queue.map(message => message.id)).toEqual([second.id]);
  expect(useAppStore.getState().draft).toBe("still unsent");
});

it("keeps queue undo on A while B is selected and requests the original conversation only after acceptance", async () => {
  await deleteQueuedMessage(owner, first);
  useAppStore.setState({ conversationId: "B", messages: [], draft: "B original draft" });
  expect(dispatchQueuedMessageHistory("undo")).toBe(true);
  await waitForQueuedMessageHistory();
  expect(queue[0].id).toBe(first.id);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", draft: "B original draft" });
  expect(wire.commands.at(-1)).toMatchObject({ type: "conversation.switch", conversation_id: owner });
  expect(wire.commands.filter(command => command.type === "user_message.queue.add").at(-1)).toMatchObject({ conversation_id: owner, user_message_id: first.id, message_id: first.queueMessageId });
});

it("honors Source's 60-second delete and 30-minute edit action lifetimes", async () => {
  const now = Date.now();
  vi.spyOn(Date, "now").mockReturnValue(now);
  await deleteQueuedMessage(owner, first);
  vi.mocked(Date.now).mockReturnValue(now + 60_001);
  expect(dispatchQueuedMessageHistory("undo")).toBe(false);
  expect(queue.map(message => message.id)).toEqual([second.id]);
  vi.mocked(Date.now).mockReturnValue(now);
  await beginQueuedMessageEdit(owner, second);
  vi.mocked(Date.now).mockReturnValue(now + 60_001);
  expect(dispatchQueuedMessageHistory("undo")).toBe(true);
  await waitForQueuedMessageHistory();
  expect(queue[0].id).toBe(second.id);
  vi.mocked(Date.now).mockReturnValue(now + 60_001 + 30 * 60_000 + 1);
  expect(dispatchQueuedMessageHistory("redo")).toBe(false);
});

it("undoes an edit with its original draft and redoes it until a new input branches the text history", async () => {
  useAppStore.setState({ draft: "earlier draft" });
  await beginQueuedMessageEdit(owner, first);
  expect(dispatchQueuedMessageHistory("undo")).toBe(true);
  await waitForQueuedMessageHistory();
  expect(useAppStore.getState().draft).toBe("earlier draft");
  expect(dispatchQueuedMessageHistory("redo")).toBe(true);
  await waitForQueuedMessageHistory();
  expect(useAppStore.getState().draft).toBe(first.content);
  expect(queue.some(message => message.id === first.id)).toBe(false);
  expect(dispatchQueuedMessageHistory("undo")).toBe(true);
  await waitForQueuedMessageHistory();
  useAppStore.setState({ draft: "a newer input branch" });
  expect(dispatchQueuedMessageHistory("redo")).toBe(false);
  expect(useAppStore.getState().draft).toBe("a newer input branch");
});

it("invalidates the edit action when its replacement is submitted rather than resurrecting the old request", async () => {
  await beginQueuedMessageEdit(owner, first);
  const edit = useAppStore.getState().queuedEdit!;
  expect(await sendChatMessage({ queuedEdit: edit, conversationId: owner, displayContent: "replacement", backendContent: "replacement" })).toBe(true);
  expect(dispatchQueuedMessageHistory("undo")).toBe(false);
  expect(queue.map(message => message.content)).toEqual(["replacement", second.content]);
});

it("does not turn a consumed restored item back into queued work when redo arrives too late", async () => {
  await deleteQueuedMessage(owner, first);
  dispatchQueuedMessageHistory("undo"); await waitForQueuedMessageHistory();
  queue = queue.filter(message => message.id !== first.id);
  expect(dispatchQueuedMessageHistory("redo")).toBe(true);
  await waitForQueuedMessageHistory();
  expect(queue.some(message => message.id === first.id)).toBe(false);
  expect(wire.toast).toHaveBeenCalledWith("Queued message is no longer available", "error");
  expect(dispatchQueuedMessageHistory("undo")).toBe(false);
});

function KeyboardHistoryHarness() {
  useKeyboardShortcuts();
  const draft = useAppStore(state => state.draft);
  return <ComposerTextarea rich value={draft} richDocument={useAppStore(state => state.richDraft)}
    onChange={(value, _selection, document) => useAppStore.getState().setDraft(value, document)} onSubmit={() => {}} />;
}

it("lets actual ProseMirror text history run before queue undo and redo", async () => {
  await deleteQueuedMessage(owner, first);
  useAppStore.setState({ draft: "", richDraft: undefined, composerPlainTextMode: false });
  const rect = new DOMRect(0, 0, 600, 80);
  Object.defineProperty(Range.prototype, "getClientRects", { configurable: true, value: () => [rect] });
  Object.defineProperty(Range.prototype, "getBoundingClientRect", { configurable: true, value: () => rect });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(rect);
  vi.spyOn(window, "scrollBy").mockImplementation(() => {});
  render(<KeyboardHistoryHarness />);
  const input = screen.getByRole("textbox"); input.focus();
  fireEvent.paste(input, { clipboardData: { items: [], files: [], types: ["text/plain"], getData: () => "ordinary text" } });
  expect(useAppStore.getState().draft).toBe("ordinary text");
  fireEvent.keyDown(input, { key: "z", code: "KeyZ", ctrlKey: true });
  expect(useAppStore.getState().draft).toBe("");
  expect(queue.some(message => message.id === first.id)).toBe(false);
  fireEvent.keyDown(input, { key: "z", code: "KeyZ", ctrlKey: true });
  await act(waitForQueuedMessageHistory);
  expect(queue[0].id).toBe(first.id);
  fireEvent.keyDown(input, { key: "Z", code: "KeyZ", ctrlKey: true, shiftKey: true });
  expect(useAppStore.getState().draft).toBe("ordinary text");
  expect(queue[0].id).toBe(first.id);
  fireEvent.keyDown(input, { key: "Z", code: "KeyZ", ctrlKey: true, shiftKey: true });
  await act(waitForQueuedMessageHistory);
  expect(queue.some(message => message.id === first.id)).toBe(false);
  expect(useAppStore.getState().draft).toBe("ordinary text");
});

it("leaves other inputs and Monaco to their native text undo", async () => {
  await deleteQueuedMessage(owner, first);
  function Harness() { useKeyboardShortcuts(); return <><input aria-label="Other input" /><div className="monaco-editor"><textarea aria-label="Code input" /></div></>; }
  render(<Harness />);
  for (const name of ["Other input", "Code input"]) {
    const input = screen.getByRole("textbox", { name }); input.focus();
    const event = new KeyboardEvent("keydown", { key: "z", code: "KeyZ", ctrlKey: true, bubbles: true, cancelable: true });
    input.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(queue.some(message => message.id === first.id)).toBe(false);
  }
});

it("exposes the verified queue undo consumer in the command menu and updates its redo availability", async () => {
  await deleteQueuedMessage(owner, first);
  useAppStore.setState({ commandPaletteOpen: true });
  render(<CommandPalette />);
  fireEvent.change(screen.getByPlaceholderText("搜索命令或会话…"), { target: { value: "撤销" } });
  fireEvent.click(screen.getByRole("option", { name: /撤销/ }));
  await act(waitForQueuedMessageHistory);
  expect(queue[0].id).toBe(first.id);
  act(() => useAppStore.setState({ commandPaletteOpen: true }));
  fireEvent.change(screen.getByPlaceholderText("搜索命令或会话…"), { target: { value: "重做" } });
  expect(screen.getByRole("option", { name: /重做/ })).toBeTruthy();
});
