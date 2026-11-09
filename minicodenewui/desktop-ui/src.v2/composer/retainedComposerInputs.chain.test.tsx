/* @vitest-environment jsdom */
import { File as NativeFile } from "node:buffer";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import { emptyComposerDraft, flushComposerDrafts, loadComposerDrafts, resetComposerDraftStorageForTests, type RetainedComposerInput } from "../stores/composer-drafts";
import { emptyConversationWorkbenchState } from "../stores/ui-slice";
import { adoptConversationDraftContent, newConversationDraftContext } from "../stores/conversation-drafts";
import { getDiffCommentScope, readDiffCommentState, setDiffComments } from "../lib/diff-comment-storage";
import type { DiffComment } from "../lib/diff-comments";
import { UserMessageCell } from "../chat/cells/UserMessageCell";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import * as outbox from "../protocol/ws-outbox";
import { cancelRetainedInputEdit, completeRetainedInputEdit, installRetainedInputEditRecovery, reconcileRetainedInputOutcome, retainFailedComposerInput, retainPendingComposerInput, restoreRetainedComposerInput } from "./retainedComposerInputs";
import { applyQueuedUserMessageSnapshot } from "../chat/sessionEvents";

const root = "C:/retained-owner", owner = "retained-owner", scope = getDiffCommentScope(owner, root);
const comment = (text: string): DiffComment => ({ type: "comment", content: [{ content_type: "text", text }], position: { path: "file.ts", side: "right", line: 3 } });
const failedInput = (id = "failed-input"): RetainedComposerInput => ({ owner: { kind: "conversation", id: owner, workspaceRoot: root },
  message: { id, role: "user", content: "**Original input**", timestamp: 20, artifacts: [], deliveryStatus: "failed",
    attachmentRefs: [{ id: "original-file", name: "original.bin", kind: "file", mediaType: "application/octet-stream", artifactId: "artifact-original" }] },
  comments: [comment("original comment")], draft: { ...emptyComposerDraft(), draft: "**Original input**",
    richDraft: { markdown: "**Original input**", plainTextMode: false, document: { type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: "Original input", marks: [{ type: "strong" }] }] }] } },
    attachments: [{ id: "original-file", name: "original.bin", type: "application/octet-stream", size: 3, status: "ready",
      conversationId: owner, workspaceRoot: root, artifactId: "artifact-original", attachment: { id: "original-file", artifact_id: "artifact-original" },
      localFile: new File([new Uint8Array([0, 127, 255])], "original.bin", { lastModified: 42 }) }],
    quotedMessage: { id: "quote", role: "assistant", content: "quoted context" },
    selectedMentions: [{ kind: "file", name: "a.ts", path: "src/a.ts" }], selectedSkills: [{ kind: "skill", name: "skill", path: "SKILL.md" }],
  } });

beforeEach(async () => {
  await waitComposerDraftRecovery();
  vi.stubGlobal("File", NativeFile);
  localStorage.clear();
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: owner, workingDirectory: root, activeConversationDraftId: null,
    conversationDrafts: {}, workspaceComposerDrafts: {}, conversationWorkbenchStates: {}, retainedComposerInputs: {}, sideChats: {},
    messages: [], conversationMessages: {}, conversationStreaming: {}, isStreaming: false, isConnected: false,
    conversations: [{ id: owner, title: "Owner", updatedAt: "2026-10-29", workspaceRoot: root }], composerDraftsReady: true });
  await flushComposerDrafts();
  await resetComposerDraftStorageForTests();
});
afterEach(async () => { cleanup(); await flushComposerDrafts(); await resetComposerDraftStorageForTests(true); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("persists a failed rich input separately from the newer draft and retrieves it without a server truncate", async () => {
  useAppStore.setState({ draft: "newer draft" }); setDiffComments(scope, [comment("newer comment")]);
  const input = failedInput(); retainFailedComposerInput(input);
  await flushComposerDrafts();
  const saved = await loadComposerDrafts();
  const retained = saved.find(record => record.retainedInput)!;
  expect(saved.find(record => !record.retainedInput && record.owner.id === owner)!.draft.draft).toBe("newer draft");
  expect(retained.draft.richDraft).toEqual(input.draft.richDraft);
  expect(retained.retainedInput?.comments).toEqual(input.comments);
  expect(new Uint8Array(await retained.draft.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([0, 127, 255]));
  const send = vi.spyOn(outbox, "sendClientCommandAwaitResult");
  render(<UserMessageCell cell={{ id: input.message.id, kind: "user", content: input.message.content } as never} conversationId={owner} />);
  fireEvent.click(screen.getByRole("button", { name: "撤回到输入框" }));
  expect(send).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ draft: input.draft.draft, richDraft: input.draft.richDraft,
    retainedInputEdit: { inputId: input.message.id, backup: { draft: "newer draft" } } });
  expect(readDiffCommentState(scope).comments).toEqual(input.comments);
  expect(useAppStore.getState().messages).toHaveLength(0);
  cancelRetainedInputEdit();
  expect(useAppStore.getState().draft).toBe("newer draft");
  expect(readDiffCommentState(scope).comments).toEqual([comment("newer comment")]);
  expect(useAppStore.getState().retainedComposerInputs[input.message.id]).toBeDefined();
  expect(useAppStore.getState().messages.map(message => message.id)).toEqual([input.message.id]);
});

it("restores the saved newer draft only after accepted retry content and subsequent typing have been consumed", async () => {
  useAppStore.setState({ draft: "backup input" });
  retainFailedComposerInput(failedInput()); restoreRetainedComposerInput("failed-input");
  const stop = installRetainedInputEditRecovery();
  try {
    useAppStore.setState({ draft: "typed during retry", attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null });
    setDiffComments(scope, []);
    completeRetainedInputEdit("failed-input");
    await act(async () => {});
    expect(useAppStore.getState().draft).toBe("typed during retry");
    expect(useAppStore.getState().retainedComposerInputs["failed-input"]).toBeUndefined();
    useAppStore.getState().setDraft("");
    await act(async () => {});
    expect(useAppStore.getState().draft).toBe("backup input");
    expect(useAppStore.getState().retainedInputEdit).toBeUndefined();
  } finally { stop(); }
});

it("keeps late attachment completion in the backed-up draft", () => {
  useAppStore.setState({ draft: "new input", attachments: [{ id: "new-upload", name: "new.txt", type: "text/plain", size: 3,
    status: "uploading", localFile: new File(["new"], "new.txt"), conversationId: owner }] });
  retainFailedComposerInput(failedInput()); restoreRetainedComposerInput("failed-input");
  useAppStore.getState().updateAttachment("new-upload", { status: "ready", artifactId: "new-artifact", attachment: { id: "new-upload" } });
  expect(useAppStore.getState().attachments.map(file => file.id)).toEqual(["original-file"]);
  cancelRetainedInputEdit();
  expect(useAppStore.getState().attachments).toEqual([expect.objectContaining({ id: "new-upload", status: "ready", artifactId: "new-artifact" })]);
});

it("owns failed-image previews independently and releases each recalled copy on cancel", async () => {
  const create = vi.fn();
  create.mockReturnValueOnce("blob:retained-image").mockReturnValueOnce("blob:recalled-image-1").mockReturnValueOnce("blob:recalled-image-2");
  const revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  const input = failedInput();
  input.draft.attachments = [{ ...input.draft.attachments[0], type: "image/png", dataUrl: "blob:submitted-image",
    localFile: new File([new Uint8Array([0, 128, 255])], "original.png", { type: "image/png" }) }];
  input.message.attachmentRefs = [{ ...input.message.attachmentRefs![0], kind: "image", mediaType: "image/png", dataUrl: "blob:submitted-image" }];
  useAppStore.setState({ draft: "newer image draft", attachments: [{ ...input.draft.attachments[0], id: "newer-image", dataUrl: "blob:newer-image" }] });
  retainFailedComposerInput(input);
  const retained = useAppStore.getState().retainedComposerInputs[input.message.id];
  expect(retained.message.attachmentRefs![0].dataUrl).toBe("blob:retained-image");
  expect(input.draft.attachments[0].dataUrl).toBe("blob:submitted-image");
  for (const suffix of [1, 2]) {
    restoreRetainedComposerInput(input.message.id);
    expect(useAppStore.getState().attachments[0].dataUrl).toBe(`blob:recalled-image-${suffix}`);
    cancelRetainedInputEdit();
    expect(useAppStore.getState().attachments[0].dataUrl).toBe("blob:newer-image");
  }
  expect(revoke.mock.calls).toEqual([["blob:recalled-image-1"], ["blob:recalled-image-2"]]);
  completeRetainedInputEdit(input.message.id);
  expect(revoke.mock.calls.at(-1)).toEqual(["blob:retained-image"]);
  expect(revoke).not.toHaveBeenCalledWith("blob:newer-image");
});

it("switches a restored accepted image to its durable artifact before releasing the retained preview", () => {
  const create = vi.fn(() => "blob:restored-submission"), revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  const input = failedInput("restored-image");
  input.draft.attachments = [{ ...input.draft.attachments[0], type: "image/png", dataUrl: "blob:old-process",
    localFile: new File([new Uint8Array([0, 128, 255])], "image.png", { type: "image/png" }) }];
  input.message.attachmentRefs = [{ ...input.message.attachmentRefs![0], kind: "image", mediaType: "image/png", dataUrl: "blob:old-process" }];
  retainPendingComposerInput(input);
  useAppStore.setState(state => ({ retainedComposerInputs: { ...state.retainedComposerInputs,
    [input.message.id]: { ...state.retainedComposerInputs[input.message.id], recovered: true } } }));
  useAppStore.getState().hydrateConversationMessages(owner, [], { confirmedUserMessageIds: [] });
  expect(useAppStore.getState().messages[0].attachmentRefs![0].dataUrl).toBe("blob:restored-submission");
  handleCommandResultEvent({ type: "command.result", command: "user_message", client_command_id: "before-restart",
    level: "success", message: "", data: { conversation_id: owner, user_message_id: input.message.id, admitted: true } });
  expect(useAppStore.getState().messages[0]).toMatchObject({ deliveryStatus: "sent",
    attachmentRefs: [expect.objectContaining({ artifactId: "artifact-original" })] });
  expect(useAppStore.getState().messages[0].attachmentRefs![0].dataUrl).toBeUndefined();
  expect(revoke).toHaveBeenCalledWith("blob:restored-submission");
  expect(useAppStore.getState().retainedComposerInputs).toEqual({});
});

it.each(["active", "inactive", "draft"])("settles a recovered %s input in one transcript publication while preserving the current authoring draft", location => {
  const input = failedInput(`single-accept-${location}`);
  if (location === "draft") {
    const context = { ...newConversationDraftContext(useAppStore.getState(), root, true), id: "unadopted-draft", requestedConversationId: owner };
    input.owner = { kind: "draft", id: context.id, workspaceRoot: root };
    useAppStore.setState({ conversationId: null, activeConversationDraftId: context.id, conversationDrafts: { [context.id]: context } });
  } else if (location === "inactive") useAppStore.setState({ conversationId: "current-reader" });
  useAppStore.setState({ draft: "newer authored draft" });
  retainPendingComposerInput(input);
  const pending = useAppStore.getState().retainedComposerInputs[input.message.id].message;
  const other = { id: "other-accepted", role: "user" as const, content: "Already accepted", timestamp: 30, artifacts: [], deliveryStatus: "sent" as const };
  const messages = [pending, other];
  useAppStore.setState({ ...(location === "active" ? { messages } : {}), conversationMessages: { [owner]: messages } });
  const publish = vi.fn();
  const stop = useAppStore.subscribe(state => state.conversationMessages, publish);
  try { reconcileRetainedInputOutcome({ ownerId: owner, inputId: input.message.id, accepted: true }); }
  finally { stop(); }
  expect(publish).toHaveBeenCalledOnce();
  expect(useAppStore.getState().conversationMessages[owner][0]).toMatchObject({ id: input.message.id, deliveryStatus: "sent" });
  expect(useAppStore.getState().conversationMessages[owner][1]).toBe(other);
  expect(useAppStore.getState().retainedComposerInputs).toEqual({});
  expect(useAppStore.getState().draft).toBe("newer authored draft");
});

it("does not turn a legacy inferred queue ID into proof that an uncertain input was received", () => {
  const input = failedInput("user_legacy-answer");
  retainPendingComposerInput(input);
  applyQueuedUserMessageSnapshot([{ conversation_id: owner, message_id: "legacy-answer", content: input.message.content }]);
  expect(useAppStore.getState().retainedComposerInputs[input.message.id]?.unconfirmed).toBe(true);
  handleCommandResultEvent({ type: "command.result", command: "user_message", client_command_id: "wrong-owner",
    level: "success", message: "", data: { conversation_id: "another-owner", user_message_id: input.message.id, admitted: true } });
  expect(useAppStore.getState().retainedComposerInputs[input.message.id]?.unconfirmed).toBe(true);
});

it("keeps the durable original when the editor-to-pending transaction aborts, then retries the same byte transfer", async () => {
  const input = failedInput("pending-transaction");
  useAppStore.setState(input.draft); await flushComposerDrafts();
  const put = IDBObjectStore.prototype.put;
  const fail = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (...args) {
    const request = put.apply(this, args);
    if (this.name === "drafts" && args[0].retainedInput) this.add(args[0]);
    return request;
  });
  retainPendingComposerInput(input);
  useAppStore.setState({ ...emptyComposerDraft(), richDraft: undefined });
  await expect(flushComposerDrafts()).rejects.toMatchObject({ name: "ConstraintError" });
  expect((await loadComposerDrafts()).find(record => !record.retainedInput && record.owner.id === owner)!.draft.draft).toBe(input.draft.draft);
  fail.mockRestore(); await flushComposerDrafts();
  const pending = (await loadComposerDrafts()).find(record => record.retainedInput?.unconfirmed)!;
  expect(new Uint8Array(await pending.draft.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([0, 127, 255]));
});

it("retires earlier failed attempts on an accepted edited retry while retaining each newer authored draft", async () => {
  useAppStore.setState({ draft: "first newer draft" });
  retainFailedComposerInput(failedInput()); restoreRetainedComposerInput("failed-input");
  useAppStore.setState({ draft: "typed during first retry", attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null });
  setDiffComments(scope, []);
  retainFailedComposerInput({ ...failedInput("failed-retry"), sourceInputId: "failed-input" });
  restoreRetainedComposerInput("failed-retry");
  const stop = installRetainedInputEditRecovery();
  try {
    useAppStore.setState({ draft: "", attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null });
    setDiffComments(scope, []);
    retainPendingComposerInput({ ...failedInput("accepted-retry"), sourceInputId: "failed-retry" });
    completeRetainedInputEdit("accepted-retry");
    await act(async () => {});
    expect(useAppStore.getState().retainedComposerInputs).toEqual({});
    expect(useAppStore.getState().draft).toBe("typed during first retry");
    useAppStore.getState().setDraft("");
    await act(async () => {});
    expect(useAppStore.getState().draft).toBe("first newer draft");
    expect(useAppStore.getState().retainedInputEdit).toBeUndefined();
  } finally { stop(); }
});

it("merges local failures around canonical history without replacing accepted records or changing their order", () => {
  retainFailedComposerInput(failedInput());
  const earlier = { id: "earlier", role: "assistant" as const, content: "Earlier", timestamp: 10, artifacts: [] };
  const later = { id: "later", role: "user" as const, content: "Later accepted", timestamp: 30, artifacts: [] };
  useAppStore.getState().hydrateConversationMessages(owner, [earlier, later]);
  expect(useAppStore.getState().messages.map(message => message.id)).toEqual(["earlier", "failed-input", "later"]);
  const accepted = { ...failedInput().message, content: "Canonical content", deliveryStatus: "sent" as const };
  useAppStore.getState().hydrateConversationMessages(owner, [earlier, accepted, later]);
  expect(useAppStore.getState().messages[1]).toBe(accepted);
  expect(useAppStore.getState().retainedComposerInputs).toEqual({});
});

it("keeps a failed persistence transaction retryable without overwriting the previously saved draft", async () => {
  useAppStore.setState({ draft: "saved newer input" }); await flushComposerDrafts();
  const put = IDBObjectStore.prototype.put;
  const fail = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (...args) {
    const request = put.apply(this, args);
    if (this.name === "drafts" && args[0].retainedInput) this.add(args[0]);
    return request;
  });
  retainFailedComposerInput(failedInput());
  await expect(flushComposerDrafts()).rejects.toMatchObject({ name: "ConstraintError" });
  expect((await loadComposerDrafts()).find(record => record.owner.id === owner)!.draft.draft).toBe("saved newer input");
  expect(useAppStore.getState().retainedComposerInputs["failed-input"]).toBeDefined();
  fail.mockRestore(); await flushComposerDrafts();
  const restored = (await loadComposerDrafts()).find(record => record.retainedInput)!;
  expect(restored.draft.draft).toBe("**Original input**");
  expect(new Uint8Array(await restored.draft.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([0, 127, 255]));
});

it("moves draft-owned failed inputs on adoption and deletes their recovery files with the actual owner", async () => {
  const context = newConversationDraftContext(useAppStore.getState(), root, true);
  useAppStore.setState({ conversationId: null, activeConversationDraftId: context.id, draft: "new draft input",
    conversationDrafts: { [context.id]: context }, workspaceComposerDrafts: { [context.id]: { ...emptyComposerDraft(), draft: "new draft input" } } });
  retainFailedComposerInput({ ...failedInput(), owner: { kind: "draft", id: context.id, workspaceRoot: root } });
  adoptConversationDraftContent(context.id, owner);
  expect(useAppStore.getState().retainedComposerInputs["failed-input"].owner).toMatchObject({ kind: "conversation", id: owner });
  useAppStore.setState({ conversationId: null, activeConversationDraftId: null, ...emptyComposerDraft() });
  await flushComposerDrafts();
  handleCommandResultEvent({ type: "command.result", command: "conversation.delete", level: "success", message: "",
    data: { conversation_id: owner, deleted: true } } as never);
  await flushComposerDrafts();
  expect(useAppStore.getState().retainedComposerInputs).toEqual({});
  expect((await loadComposerDrafts()).filter(record => record.owner.id === owner || record.retainedInput)).toEqual([]);
});
