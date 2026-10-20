/* @vitest-environment jsdom */
import { File as NativeFile } from "node:buffer";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppStore, ComposerAttachment } from "./types";

type Storage = typeof import("./composer-drafts");
const renderers: Storage[] = [];
const setOwner = (windowOwnerId: string) => { window.__MINICODE_RUNTIME__ = { windowOwnerId }; };
const renderer = async (owner: string) => {
  setOwner(owner);
  vi.resetModules();
  const storage = await import("./composer-drafts");
  renderers.push(storage);
  return storage;
};
const state = (patch: Partial<AppStore> = {}): AppStore => ({
  draft: "", attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [], allowedRemoteImageDomains: [],
  conversationId: null, workingDirectory: "C:/draft-project", conversations: [], workspaceComposerDrafts: {},
  conversationWorkbenchStates: {}, sideChats: {}, ...patch,
} as AppStore);
const attachment = (id: string, file: File, status: ComposerAttachment["status"] = "uploading"): ComposerAttachment => ({
  id, name: file.name, type: file.type, size: file.size, status, localFile: file,
});
const save = async (storage: Storage, snapshot: AppStore, revision = 1) => {
  storage.persistComposerDrafts(storage.collectComposerDrafts(snapshot, revision));
  await storage.flushComposerDrafts();
};

beforeEach(() => {
  vi.stubGlobal("indexedDB", new IDBFactory());
  vi.stubGlobal("File", NativeFile);
});
afterEach(async () => {
  vi.restoreAllMocks();
  for (const storage of renderers.splice(0)) await storage.resetComposerDraftStorageForTests(true);
  vi.unstubAllGlobals();
  delete window.__MINICODE_RUNTIME__;
});

describe("durable owned composer drafts", () => {
  it("cold reopens structured drafts for the active conversation, hidden workspace and side chat without changing ownership", async () => {
    const first = await renderer("rich-window");
    const rich = { markdown: "**keep**", plainTextMode: false,
      document: { type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: "keep", marks: [{ type: "strong" }] }] }] } };
    const hidden = { ...state(), draft: "**hidden**", richDraft: { ...rich, markdown: "**hidden**",
      document: { type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: "hidden", marks: [{ type: "strong" }] }] }] } } };
    await save(first, state({ conversationId: "rich-main", draft: rich.markdown, richDraft: rich,
      workspaceComposerDrafts: { "c:/hidden": hidden }, sideChats: { side: { id: "side", draft: rich.markdown, richDraft: rich, messages: [], isStreaming: false } } }));
    await first.resetComposerDraftStorageForTests(true);
    const cold = await renderer("rich-window");
    const records = await cold.loadComposerDrafts();
    expect(records.find(record => record.owner.id === "rich-main")!.draft.richDraft).toEqual(rich);
    expect(records.find(record => record.owner.id === "c:/hidden")!.draft.richDraft).toEqual(hidden.richDraft);
    expect(records.find(record => record.owner.kind === "side")!.draft.richDraft).toEqual(rich);
    expect(records.every(record => record.windowOwnerId === "rich-window")).toBe(true);
  });
  it("cold reopens original file bytes, all draft metadata and an interrupted upload as retryable", async () => {
    const first = await renderer("primary");
    const bytes = new Uint8Array([0, 1, 127, 128, 255, 10]);
    const file = new File([bytes], "未完成.bin", { type: "application/octet-stream", lastModified: 123456 });
    const snapshot = state({ draft: "未发送的正文", goalMode: true, attachments: [attachment("binary-file", file)],
      quotedMessage: { id: "quoted", content: "保留引用", role: "assistant" },
      selectedMentions: [{ kind: "file", name: "a.ts", path: "a.ts" }],
      selectedSkills: [{ kind: "skill", path: "SKILL.md", name: "retained" }],
      allowedRemoteImageDomains: ["example.test"] });
    await save(first, snapshot);
    await first.resetComposerDraftStorageForTests(true);
    const cold = await renderer("primary");
    const recovered = (await cold.loadComposerDrafts())[0].draft;
    expect(recovered).toMatchObject({ draft: snapshot.draft, goalMode: true, quotedMessage: snapshot.quotedMessage,
      selectedMentions: snapshot.selectedMentions, selectedSkills: snapshot.selectedSkills, allowedRemoteImageDomains: ["example.test"] });
    expect(recovered.attachments[0]).toMatchObject({ status: "error", error: "上传已中断，可点击重试。" });
    const reopened = recovered.attachments[0].localFile!;
    expect(reopened).toMatchObject({ name: file.name, type: file.type, lastModified: file.lastModified });
    expect(new Uint8Array(await reopened.arrayBuffer())).toEqual(bytes);
  });

  it("reads only files referenced by this window while the same owner remains independent in another window", async () => {
    const primary = await renderer("primary");
    await save(primary, state({ conversationId: "same-thread", draft: "primary draft",
      attachments: [attachment("primary-file", new File(["primary bytes"], "primary.txt"))] }));
    const secondary = await renderer("secondary-1");
    await save(secondary, state({ conversationId: "same-thread", draft: "secondary draft",
      attachments: [attachment("secondary-file", new File(["secondary bytes"], "secondary.txt"))] }));
    const reads = vi.spyOn(IDBObjectStore.prototype, "get");
    setOwner("primary");
    const recovered = await primary.loadComposerDrafts();
    expect(recovered.map(record => record.draft.draft)).toEqual(["primary draft"]);
    expect(await recovered[0].draft.attachments[0].localFile!.text()).toBe("primary bytes");
    expect(reads.mock.calls.filter((_, index) => reads.mock.contexts[index].name === "files").map(([key]) => key)).toEqual(["primary-file"]);
    reads.mockClear();
    setOwner("secondary-1");
    expect((await secondary.loadComposerDrafts()).map(record => record.draft.draft)).toEqual(["secondary draft"]);
    expect(reads.mock.calls.filter((_, index) => reads.mock.contexts[index].name === "files").map(([key]) => key)).toEqual(["secondary-file"]);
  });

  it.each(["workspace", "conversation"] as const)("a live empty %s draft removes its stale cached record and file", async kind => {
    const storage = await renderer("primary");
    const id = kind === "conversation" ? "active-thread" : "c:/draft-project";
    const cached = { ...storage.emptyComposerDraft(), draft: "stale cached input",
      attachments: [attachment("stale-file", new File(["old bytes"], "old.txt"))] };
    const active = state({ ...(kind === "conversation" ? { conversationId: id } : {}), ...cached });
    await save(storage, active);
    const empty = state(kind === "conversation" ? { conversationId: id, conversationWorkbenchStates: { [id]: cached as never } }
      : { workspaceComposerDrafts: { [id]: cached } });
    await save(storage, empty, 2);
    await storage.resetComposerDraftStorageForTests(true);
    expect(await storage.loadComposerDrafts()).toEqual([]);
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("minicode-composer-drafts", 1);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    const count = db.transaction("files").objectStore("files").count();
    expect(await new Promise<number>((resolve, reject) => { count.onsuccess = () => resolve(count.result); count.onerror = () => reject(count.error); })).toBe(0);
    db.close();
  });

  it("an explicit deletion cannot be resurrected by snapshots captured in two dormant windows", async () => {
    const primary = await renderer("primary");
    const primaryState = state({ conversationId: "deleted-thread", draft: "old primary",
      attachments: [attachment("deleted-primary-file", new File(["primary"], "p.txt"))] });
    const oldPrimary = primary.collectComposerDrafts(primaryState, 3);
    await save(primary, primaryState);
    const secondary = await renderer("secondary-1");
    const secondaryState = state({ conversationId: "deleted-thread", draft: "old secondary",
      attachments: [attachment("deleted-secondary-file", new File(["secondary"], "s.txt"))] });
    const oldSecondary = secondary.collectComposerDrafts(secondaryState, 3);
    await save(secondary, secondaryState);
    await primary.deleteConversationComposerDrafts("deleted-thread");
    primary.persistComposerDrafts(oldPrimary);
    secondary.persistComposerDrafts(oldSecondary);
    await Promise.all([primary.flushComposerDrafts(), secondary.flushComposerDrafts()]);
    setOwner("primary"); expect(await primary.loadComposerDrafts()).toEqual([]);
    setOwner("secondary-1"); expect(await secondary.loadComposerDrafts()).toEqual([]);
  });

  it("retains a failed commit and its previous durable bytes until a genuine flush retry commits", async () => {
    const storage = await renderer("primary");
    await save(storage, state({ draft: "previous durable input" }));
    const status = vi.fn(); storage.watchComposerDraftStorage(status);
    const put = IDBObjectStore.prototype.put;
    const failure = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (...args) {
      const request = put.apply(this, args);
      if (this.name === "drafts") this.add(args[0]);
      return request;
    });
    storage.persistComposerDrafts(storage.collectComposerDrafts(state({ draft: "last authored input" }), 2));
    await expect(storage.flushComposerDrafts()).rejects.toMatchObject({ name: "ConstraintError" });
    expect(storage.hasPendingComposerDrafts()).toBe(true);
    expect(status.mock.lastCall?.[0]).toMatchObject({ pending: true, revision: 1, error: expect.any(String) });
    expect((await storage.loadComposerDrafts())[0].draft.draft).toBe("previous durable input");
    failure.mockRestore();
    await storage.flushComposerDrafts();
    expect(storage.hasPendingComposerDrafts()).toBe(false);
    expect((await storage.loadComposerDrafts())[0].draft.draft).toBe("last authored input");
    expect(status.mock.lastCall?.[0]).toEqual({ pending: false, revision: 2, error: null });
  });

  it("keeps a failed deletion visible through later snapshots and retries its authorised tombstone after space is released", async () => {
    const storage = await renderer("primary");
    await save(storage, state({ conversationId: "deleted-thread", draft: "already deleted by backend" }));
    const status = vi.fn(); storage.watchComposerDraftStorage(status);
    const put = IDBObjectStore.prototype.put;
    const failure = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (...args) {
      if (this.name === "deletedConversations") throw new DOMException("Disk is full", "QuotaExceededError");
      return put.apply(this, args);
    });
    await expect(storage.deleteConversationComposerDrafts("deleted-thread")).rejects.toMatchObject({ name: "QuotaExceededError" });
    await expect(save(storage, state({ draft: "new workspace input" }), 2)).rejects.toMatchObject({ name: "QuotaExceededError" });
    expect(status.mock.lastCall?.[0]).toMatchObject({ pending: true, error: expect.stringContaining("Disk is full") });
    expect(storage.hasPendingComposerDrafts()).toBe(true);
    failure.mockRestore();
    const retry = storage.flushComposerDrafts();
    expect(status.mock.lastCall?.[0]).toMatchObject({ pending: true, error: expect.stringContaining("Disk is full") });
    await retry;
    expect(status.mock.lastCall?.[0]).toEqual({ pending: false, revision: 2, error: null });
    expect((await storage.loadComposerDrafts()).map(record => record.draft.draft)).toEqual(["new workspace input"]);
    storage.persistComposerDrafts(storage.collectComposerDrafts(state({ conversationId: "deleted-thread", draft: "late dormant snapshot" }), 3));
    await storage.flushComposerDrafts();
    expect(await storage.loadComposerDrafts()).toEqual([]);
  });
});
