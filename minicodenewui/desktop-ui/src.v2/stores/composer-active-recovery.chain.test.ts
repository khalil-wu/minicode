/* @vitest-environment jsdom */
import { IDBFactory } from "fake-indexeddb";
import { afterEach, expect, it, vi } from "vitest";
import type { AppStore } from "./types";
import type { DraftModelSelection } from "../protocol/api";
import { File as NativeFile } from "node:buffer";

afterEach(async () => {
  const storage = await import("./composer-drafts");
  await storage.resetComposerDraftStorageForTests(true);
  vi.restoreAllMocks(); vi.unstubAllGlobals();
  delete window.__MINICODE_RUNTIME__; localStorage.clear();
});

it.each([false, true])("cold restores an input (unconfirmed=%s) and the current draft as separate owners of their file bytes", async unconfirmed => {
  vi.stubGlobal("indexedDB", new IDBFactory());
  vi.stubGlobal("File", NativeFile);
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "retained-cold", initialNavigation: { conversationId: "cold-owner" } } as never;
  vi.resetModules();
  const storage = await import("./composer-drafts");
  const original = { ...storage.emptyComposerDraft(), draft: "**original failed input**",
    richDraft: { markdown: "**original failed input**", plainTextMode: false, document: { type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: "original failed input" }] }] } },
    attachments: [{ id: "old-file", name: "old.bin", type: "application/octet-stream", size: 3, status: "ready" as const,
      localFile: new File([new Uint8Array([0, 128, 255])], "old.bin", { lastModified: 12 }), artifactId: "old-artifact" }],
  };
  const message = { id: "failed-cold", role: "user" as const, content: original.draft, artifacts: [], timestamp: 10,
    deliveryStatus: "failed" as const, attachmentRefs: [{ id: "old-file", name: "old.bin", kind: "file" as const, mediaType: "application/octet-stream", artifactId: "old-artifact" }] };
  const comment = { type: "comment" as const, content: [{ content_type: "text" as const, text: "retained review" }], position: { path: "a.ts", side: "right" as const, line: 2 } };
  const seed = { ...storage.emptyComposerDraft(), conversationId: "cold-owner", workingDirectory: "C:/cold-owner", activeConversationDraftId: null,
    conversationDrafts: {}, homeConversationDraftIds: {}, conversations: [], conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, sideChats: {},
    retainedComposerInputs: { [message.id]: { owner: { kind: "conversation", id: "cold-owner", workspaceRoot: "C:/cold-owner" },
      unconfirmed: unconfirmed || undefined, message, draft: original, comments: [comment] } },
    draft: "newer current draft", attachments: [{ id: "new-file", name: "new.bin", type: "application/octet-stream", size: 2, status: "ready",
      localFile: new File([new Uint8Array([9, 8])], "new.bin", { lastModified: 34 }) }],
  } as unknown as AppStore;
  storage.persistComposerDrafts(storage.collectComposerDrafts(seed, 1)); await storage.flushComposerDrafts();
  await storage.resetComposerDraftStorageForTests(true); vi.resetModules();
  const { useAppStore, waitComposerDraftRecovery } = await import("./index");
  await waitComposerDraftRecovery();
  useAppStore.setState({ conversations: [{ id: "cold-owner", title: "Owner", workspaceRoot: "C:/cold-owner", updatedAt: "2026-10-29" }] });
  useAppStore.getState().applyConversationSwitched({ conversationId: "cold-owner" });
  useAppStore.getState().hydrateConversationMessages("cold-owner", []);
  expect(useAppStore.getState().draft).toBe("newer current draft");
  expect(useAppStore.getState().messages.map(item => item.id)).toEqual([message.id]);
  const retained = useAppStore.getState().retainedComposerInputs[message.id];
  expect(retained.recovered).toBe(true);
  expect(retained.message.deliveryStatus).toBe(unconfirmed ? "sending" : "failed");
  if (unconfirmed) {
    useAppStore.getState().hydrateConversationMessages("cold-owner", [{ ...retained.message, deliveryStatus: "sent" }], { confirmedUserMessageIds: [] });
    expect(useAppStore.getState().retainedComposerInputs[message.id]).toBe(retained);
  }
  expect(retained.draft.richDraft).toEqual(original.richDraft);
  expect(retained.comments).toEqual([comment]);
  expect(new Uint8Array(await retained.draft.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([0, 128, 255]));
  const recovery = await import("../composer/retainedComposerInputs");
  recovery.restoreRetainedComposerInput(message.id);
  const current = await import("./composer-drafts");
  await current.flushComposerDrafts();
  const savedEditor = (await current.loadComposerDrafts()).find(record => !record.retainedInput && record.owner.id === "cold-owner")!;
  expect(savedEditor.draft.retainedInputEdit?.backup.draft).toBe("newer current draft");
  expect(new Uint8Array(await savedEditor.draft.retainedInputEdit!.backup.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([9, 8]));
  recovery.cancelRetainedInputEdit();
  expect(useAppStore.getState().draft).toBe("newer current draft");
});

it.each(["history-before-recovery", "queue-before-recovery", "steer-before-recovery", "bound-draft-history", "late-admission", "late-queue", "late-rejection"] as const)("reconciles a restored input from %s without an original callback or replaying it", async outcome => {
  vi.stubGlobal("indexedDB", new IDBFactory());
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "unknown-recovery", initialNavigation: { conversationId: "pending-owner" } } as never;
  vi.resetModules();
  const storage = await import("./composer-drafts");
  const draftOwned = outcome === "bound-draft-history";
  const draftContext = { id: "requested-draft", workspaceRoot: "C:/pending-owner", bindWorkspace: true,
    requestedConversationId: "pending-owner", appMode: "code", permissionMode: "confirm", permissionEdited: false,
    modelSelection: { mode: "default" }, selectionLoading: false, selectionError: null, preferencesLoading: false };
  const original = { ...storage.emptyComposerDraft(), draft: "submitted before restart" };
  const message = { id: "stable-submission-id", role: "user" as const, content: original.draft, timestamp: 1, artifacts: [], deliveryStatus: "sending" as const };
  storage.persistComposerDrafts(storage.collectComposerDrafts({ ...storage.emptyComposerDraft(),
    conversationId: draftOwned ? null : "pending-owner", workingDirectory: "C:/pending-owner", activeConversationDraftId: draftOwned ? "requested-draft" : null,
    conversationDrafts: draftOwned ? { "requested-draft": draftContext } : {},
    conversations: [], homeConversationDraftIds: {}, workspaceComposerDrafts: {}, conversationWorkbenchStates: {}, sideChats: {},
    retainedComposerInputs: { [message.id]: { owner: { kind: draftOwned ? "draft" : "conversation", id: draftOwned ? "requested-draft" : "pending-owner", workspaceRoot: "C:/pending-owner" },
      message, draft: original, comments: [], unconfirmed: true } }, draft: "newer authored draft",
  } as unknown as AppStore, 1));
  await storage.flushComposerDrafts(); await storage.resetComposerDraftStorageForTests(true);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const load = storage.loadComposerDrafts;
  vi.spyOn(storage, "loadComposerDrafts").mockImplementation(async () => { await gate; return load(); });
  const { useAppStore, waitComposerDraftRecovery } = await import("./index");
  const outbox = await import("../protocol/ws-outbox");
  const send = vi.spyOn(outbox, "sendClientCommand");
  const canonical = { ...message, deliveryStatus: "sent" as const };
  if (outcome === "history-before-recovery" || draftOwned) useAppStore.getState().hydrateConversationMessages("pending-owner", [canonical], { activate: false });
  if (outcome === "queue-before-recovery" || outcome === "steer-before-recovery") {
    const { applyQueuedUserMessageSnapshot, applyPendingTurnInputSnapshot } = await import("../chat/sessionEvents");
    const entry = { conversation_id: "pending-owner", message_id: "queued-answer", user_message_id: message.id, content: original.draft };
    if (outcome === "queue-before-recovery") applyQueuedUserMessageSnapshot([entry]);
    else applyPendingTurnInputSnapshot([{ ...entry, mode: "steer", target_message_id: "running-answer" }]);
  }
  release(); await waitComposerDraftRecovery();
  useAppStore.setState({ conversations: [{ id: "pending-owner", title: "Pending", workspaceRoot: "C:/pending-owner" }] });
  useAppStore.getState().applyConversationSwitched({ conversationId: "pending-owner" });
  if (outcome.startsWith("late-")) {
    useAppStore.getState().hydrateConversationMessages("pending-owner", [], { confirmedUserMessageIds: [] });
    if (outcome === "late-queue") {
      const { applyUserMessageQueueUpdate } = await import("../chat/sessionEvents");
      applyUserMessageQueueUpdate({ type: "user_message.queue.updated", conversation_id: "pending-owner", message_id: "queued-answer",
        user_message_id: message.id, status: "queued", position: 1 });
    } else {
      const { handleCommandResultEvent } = await import("../chat/commandResultEvents");
      handleCommandResultEvent({ type: "command.result", command: "user_message", client_command_id: "old-process-command",
        level: outcome === "late-rejection" ? "error" : "success", message: "actual recovered outcome",
        data: { conversation_id: "pending-owner", user_message_id: message.id, admitted: outcome !== "late-rejection" } });
    }
  }
  expect(draftOwned ? useAppStore.getState().workspaceComposerDrafts["requested-draft"].draft : useAppStore.getState().draft).toBe("newer authored draft");
  if (outcome === "late-rejection") expect(useAppStore.getState().retainedComposerInputs[message.id]).toMatchObject({
    unconfirmed: undefined, message: { deliveryStatus: "failed", failureMessage: "actual recovered outcome" }, draft: original,
  });
  else expect(useAppStore.getState().retainedComposerInputs).toEqual({});
  expect(send.mock.calls.some(([command]) => command.type === "user_message")).toBe(false);
});

it("hydrates the active rich goal mode before the ready snapshot can overwrite its saved owner", async () => {
  vi.stubGlobal("indexedDB", new IDBFactory());
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "active-rich-recovery" };
  vi.resetModules();
  const storage = await import("./composer-drafts");
  const rich = { markdown: "**Goal objective**", plainTextMode: false, document: { type: "doc",
    content: [{ type: "paragraph", content: [{ type: "text", text: "Goal objective", marks: [{ type: "strong" }] }] }] } };
  const seed = { conversationId: null, workingDirectory: "", conversations: [], conversationWorkbenchStates: {},
    workspaceComposerDrafts: {}, sideChats: {}, retainedComposerInputs: {}, draft: rich.markdown, richDraft: rich, goalMode: true,
    attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [], allowedRemoteImageDomains: [] } as unknown as AppStore;
  storage.persistComposerDrafts(storage.collectComposerDrafts(seed, 1));
  await storage.flushComposerDrafts();
  await storage.resetComposerDraftStorageForTests(true);
  vi.resetModules();
  const api = await import("../protocol/api");
  let finishSelection!: (selection: DraftModelSelection) => void;
  const modelRead = vi.spyOn(api, "fetchDraftModelSelection").mockReturnValue(
    new Promise<DraftModelSelection>(resolve => { finishSelection = resolve; }));
  const { useAppStore, waitComposerDraftRecovery } = await import("./index");
  await waitComposerDraftRecovery();
  expect(useAppStore.getState()).toMatchObject({ composerDraftsReady: true, goalMode: true, draft: rich.markdown, richDraft: rich });
  const draftId = useAppStore.getState().activeConversationDraftId!;
  const modelReady = useAppStore.getState().refreshConversationDraftSelection(draftId);
  expect(modelRead).toHaveBeenCalledOnce();
  expect(useAppStore.getState().conversationDrafts[draftId].selectionLoading).toBe(true);
  finishSelection({ provider: "custom", model: "recovery-fixture", current_model: "recovery-fixture",
    available_models: ["recovery-fixture"], models_source: "configured", available_permission_modes: ["confirm"],
    config_version: "recovered-selection", config_cwd: "", feature_flags: {}, composer_commands: [] });
  expect(await modelReady).toBe(true);
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: draftId, currentModel: "recovery-fixture",
    goalMode: true, draft: rich.markdown, richDraft: rich });
  const current = await import("./composer-drafts");
  await current.flushComposerDrafts();
  const saved = (await current.loadComposerDrafts()).find(record => record.owner.kind === "draft")!;
  expect(saved.draft).toMatchObject({ goalMode: true, richDraft: rich, draft: rich.markdown });
});
