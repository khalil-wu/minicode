/* @vitest-environment jsdom */
import { IDBFactory } from "fake-indexeddb";
import { afterEach, expect, it, vi } from "vitest";
import type { AppStore } from "./types";

afterEach(async () => {
  const storage = await import("./composer-drafts");
  await storage.resetComposerDraftStorageForTests(true);
  vi.unstubAllGlobals(); vi.resetModules();
  delete window.__MINICODE_RUNTIME__; localStorage.clear();
});

it("hydrates the active rich goal mode before the ready snapshot can overwrite its saved owner", async () => {
  vi.stubGlobal("indexedDB", new IDBFactory());
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "active-rich-recovery" };
  vi.resetModules();
  const storage = await import("./composer-drafts");
  const rich = { markdown: "**Goal objective**", plainTextMode: false, document: { type: "doc",
    content: [{ type: "paragraph", content: [{ type: "text", text: "Goal objective", marks: [{ type: "strong" }] }] }] } };
  const seed = { conversationId: null, workingDirectory: "", conversations: [], conversationWorkbenchStates: {},
    workspaceComposerDrafts: {}, sideChats: {}, draft: rich.markdown, richDraft: rich, goalMode: true,
    attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [], allowedRemoteImageDomains: [] } as unknown as AppStore;
  storage.persistComposerDrafts(storage.collectComposerDrafts(seed, 1));
  await storage.flushComposerDrafts();
  await storage.resetComposerDraftStorageForTests(true);
  vi.resetModules();
  const { useAppStore, waitComposerDraftRecovery } = await import("./index");
  await waitComposerDraftRecovery();
  expect(useAppStore.getState()).toMatchObject({ composerDraftsReady: true, goalMode: true, draft: rich.markdown, richDraft: rich });
  const current = await import("./composer-drafts");
  await current.flushComposerDrafts();
  const saved = (await current.loadComposerDrafts()).find(record => record.owner.kind === "workspace")!;
  expect(saved.draft).toMatchObject({ goalMode: true, richDraft: rich, draft: rich.markdown });
});
