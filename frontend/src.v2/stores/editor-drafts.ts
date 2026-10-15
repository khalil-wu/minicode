import type { EditorTab } from "./types";
import { pushToast } from "../overlays/ToastContainer";
import { canClaimLegacyEditorDrafts, draftWindowOwnerId, windowDraftWorkspaceKey } from "./draft-window-owner";

type DraftRecord = { workspace: string; path: string; tab: EditorTab };
export type EditorWorkspaceIndex = {
  workspace: string;
  windowOwnerId?: string;
  legacyClaimOwnerId?: string;
  paths: string[];
  metadata: Record<string, Pick<EditorTab, "pinned" | "preview" | "lastActivated" | "language" | "recoveryPath">>;
  activeTabPath: string | null;
  activeEditorPath: string | null;
};
type DraftSnapshot = { workspace: string; ownerId: string; tabs: EditorTab[]; legacy: EditorTab[]; legacyKey: string; legacyIndex?: EditorWorkspaceIndex; index: EditorWorkspaceIndex; closed: Set<string> };
type LegacyWorkspaceIndex = EditorWorkspaceIndex & { legacyClaimOwnerId?: string };
const drafts = new Map<string, Map<string, EditorTab>>();
const indexes = new Map<string, EditorWorkspaceIndex>();
const declaredPaths = new Map<string, string[]>();
const loads = new Map<string, Promise<Map<string, EditorTab>>>();
const pending = new Map<string, DraftSnapshot>();
let database: Promise<IDBDatabase> | undefined;
let writing: Promise<void> | null = null;
let failure: unknown;

function openDatabase() {
  return database ??= new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open("minicode-editor-drafts", 2);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("drafts")) {
        request.result.createObjectStore("drafts", { keyPath: ["workspace", "path"] }).createIndex("workspace", "workspace");
      }
      request.result.createObjectStore("workspaces", { keyPath: "workspace" });
    };
    request.onblocked = () => {
      console.error("Editor recovery upgrade is blocked by another page's open database connection");
      pushToast("编辑器草稿数据库升级正在等待其它 MiniCode 页面关闭。请先保留旧页面的未保存内容，再关闭旧页面。", "error");
    };
    request.onsuccess = () => {
      const opened = request.result;
      const owner = database;
      opened.onversionchange = () => {
        opened.close();
        if (database === owner) {
          database = undefined;
          drafts.clear(); indexes.clear(); loads.clear();
        }
      };
      resolve(opened);
    };
    request.onerror = () => { database = undefined; reject(request.error); };
  });
}

function committed(transaction: IDBTransaction) {
  return new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error);
  });
}

export const cachedEditorDrafts = (workspace: string) => drafts.get(windowDraftWorkspaceKey(workspace));

function result<T>(request: IDBRequest<T>) {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function mergedIndex(saved: EditorWorkspaceIndex, snapshot: DraftSnapshot): EditorWorkspaceIndex {
  const paths = [...snapshot.index.paths, ...saved.paths.filter(path => !snapshot.index.paths.includes(path) && !snapshot.closed.has(path))];
  return { ...saved, ...snapshot.index, paths,
    metadata: Object.fromEntries(paths.map(path => [path, snapshot.index.metadata[path] ?? saved.metadata[path]])),
  };
}

export function cachedEditorWorkspaceIndex(workspace: string) {
  const key = windowDraftWorkspaceKey(workspace);
  const saved = indexes.get(key);
  const snapshot = pending.get(key);
  return saved && snapshot ? mergedIndex(saved, snapshot) : saved ?? snapshot?.index;
}

export function loadEditorDrafts(workspace: string, legacy: EditorTab[], legacyKey: string, legacyIndex?: EditorWorkspaceIndex, ownerId = draftWindowOwnerId()) {
  const key = windowDraftWorkspaceKey(workspace, ownerId);
  if (loads.has(key)) return loads.get(key)!;
  const loading = (async () => {
    const db = await openDatabase();
    const transaction = db.transaction(["drafts", "workspaces"], "readonly");
    const [records, storedIndex] = await Promise.all([
      result<DraftRecord[]>(transaction.objectStore("drafts").index("workspace").getAll(key)),
      result<EditorWorkspaceIndex | undefined>(transaction.objectStore("workspaces").get(key)),
    ]);
    const saved = new Map(records.map(record => [record.path, record.tab]));
    let index = storedIndex && { ...storedIndex, workspace };
    if (!index) {
      const transaction = db.transaction(["drafts", "workspaces"], "readwrite");
      const draftStore = transaction.objectStore("drafts");
      const workspaceStore = transaction.objectStore("workspaces");
      const [oldRecords, oldIndex] = canClaimLegacyEditorDrafts(ownerId) ? await Promise.all([
        result<DraftRecord[]>(draftStore.index("workspace").getAll(workspace)),
        result<LegacyWorkspaceIndex | undefined>(workspaceStore.get(workspace)),
      ]) : [[], undefined] as const;
      // The legacy rows stay at their original keys. An atomic claim assigns
      // them to the primary native window or the first browser upgrading them.
      const claimLegacy = canClaimLegacyEditorDrafts(ownerId)
        && (!oldIndex?.legacyClaimOwnerId || oldIndex.legacyClaimOwnerId === ownerId);
      const admittedLegacy = legacyKey.endsWith(key) || claimLegacy ? legacy : [];
      const admittedIndex = legacyIndex?.windowOwnerId === ownerId || claimLegacy ? legacyIndex : undefined;
      if (claimLegacy) for (const record of oldRecords) if (!saved.has(record.path)) saved.set(record.path, record.tab);
      for (const tab of admittedLegacy) if (!saved.has(tab.path)) saved.set(tab.path, tab);
      // Version 1 stored bodies separately from its localStorage index. Keep
      // bodies recoverable even when the old path index was lost to LS quota.
      const bodyMetadata = Object.fromEntries([...saved.values()].map(tab => [tab.path, {
        pinned: tab.pinned, preview: tab.preview, lastActivated: tab.lastActivated, language: tab.language,
      }]));
      const inheritedIndex = claimLegacy ? oldIndex ?? admittedIndex : admittedIndex;
      index = { workspace, windowOwnerId: ownerId,
        ...(claimLegacy && (oldRecords.length || oldIndex || admittedLegacy.length || admittedIndex) ? { legacyClaimOwnerId: ownerId } : {}),
        paths: [...new Set([...(inheritedIndex?.paths ?? []), ...saved.keys()])],
        metadata: { ...bodyMetadata, ...inheritedIndex?.metadata },
        activeTabPath: inheritedIndex?.activeTabPath ?? records[0]?.path ?? saved.keys().next().value ?? null,
        activeEditorPath: inheritedIndex?.activeEditorPath ?? null };
      for (const tab of saved.values()) draftStore.put({ workspace: key, path: tab.path, tab });
      workspaceStore.put({ ...index, workspace: key });
      if (claimLegacy && (oldRecords.length || oldIndex || admittedLegacy.length || admittedIndex)) {
        for (const tab of admittedLegacy) if (!oldRecords.some(record => record.path === tab.path)) {
          draftStore.put({ workspace, path: tab.path, tab });
        }
        workspaceStore.put({ ...(oldIndex ?? index), workspace, legacyClaimOwnerId: ownerId });
      }
      await committed(transaction);
      if (admittedLegacy.length) localStorage.removeItem(legacyKey);
    }
    drafts.set(key, saved);
    indexes.set(key, index);
    return saved;
  })().catch(error => { loads.delete(key); throw error; });
  loads.set(key, loading);
  return loading;
}

export function persistEditorDrafts(workspace: string, tabs: EditorTab[], legacy: EditorTab[], legacyKey: string, index: EditorWorkspaceIndex, legacyIndex?: EditorWorkspaceIndex) {
  const ownerId = draftWindowOwnerId();
  const key = windowDraftWorkspaceKey(workspace, ownerId);
  const previous = pending.get(key);
  const before = declaredPaths.get(key) ?? indexes.get(key)?.paths
    ?? (legacyIndex?.windowOwnerId === ownerId || ownerId === "primary" ? legacyIndex?.paths : undefined) ?? [];
  const closed = new Set([...(previous?.closed ?? []), ...before.filter(path => !index.paths.includes(path))]);
  for (const path of index.paths) closed.delete(path);
  declaredPaths.set(key, index.paths);
  pending.set(key, { workspace, ownerId, tabs, legacy, legacyKey, index, legacyIndex, closed });
  startWriting();
}

function startWriting() {
  if (writing) return;
  writing = (async () => {
    const db = await openDatabase();
    while (pending.size) {
      const [key, snapshot] = pending.entries().next().value!;
      const saved = await loadEditorDrafts(snapshot.workspace, snapshot.legacy, snapshot.legacyKey, snapshot.legacyIndex, snapshot.ownerId);
      const latest = pending.get(key)!;
      const index = mergedIndex(indexes.get(key)!, latest);
      const paths = new Set([...index.paths, ...latest.tabs.flatMap(tab => tab.recoveryPending && tab.recoveryPath ? [tab.recoveryPath] : [])]);
      const changes = latest.tabs.filter(tab => !tab.recoveryPending).filter(tab => {
        const previous = saved.get(tab.path);
        if (tab.readOnly || tab.largeFile || tab.content === tab.original) return Boolean(previous);
        return tab.content !== previous?.content || tab.original !== previous?.original
          || tab.contentHash !== previous?.contentHash || tab.pendingBufferTransactions !== previous?.pendingBufferTransactions;
      });
      const removed = [...saved.keys()].filter(path => !paths.has(path));
      const indexChanged = JSON.stringify(index) !== JSON.stringify(indexes.get(key));
      if (!changes.length && !removed.length && !indexChanged) {
        failure = undefined;
        if (pending.get(key) === latest) pending.delete(key);
        continue;
      }
      const transaction = db.transaction(["drafts", "workspaces"], "readwrite");
      const records = transaction.objectStore("drafts");
      transaction.objectStore("workspaces").put({ ...index, workspace: key, windowOwnerId: latest.ownerId });
      for (const path of removed) records.delete([key, path]);
      for (const tab of changes) {
        if (!tab.readOnly && !tab.largeFile && tab.content !== tab.original) records.put({ workspace: key, path: tab.path, tab });
        else records.delete([key, tab.path]);
      }
      // Update memory only after commit; a failed commit retains the authored
      // snapshot and propagates to flush instead of claiming it is saved.
      await committed(transaction);
      failure = undefined;
      for (const path of removed) saved.delete(path);
      for (const tab of changes) {
        if (!tab.readOnly && !tab.largeFile && tab.content !== tab.original) saved.set(tab.path, tab);
        else saved.delete(tab.path);
      }
      indexes.set(key, index);
      if (pending.get(key) === latest) pending.delete(key);
    }
  })().catch(error => {
    failure = error;
    console.error("Editor draft recovery commit failed", error);
    pushToast(`无法保存编辑器恢复草稿：${error instanceof Error ? error.message : String(error)}。请保留窗口和未保存内容。`, "error");
  }).finally(() => {
    writing = null;
    if (pending.size && failure === undefined) startWriting();
  });
}

export const hasPendingEditorDrafts = () => Boolean(writing || pending.size);
export async function flushEditorDrafts() {
  if (pending.size && failure !== undefined) startWriting();
  const loaded = await Promise.allSettled([...loads.values()]);
  while (writing) await writing;
  if (failure !== undefined) throw failure;
  const rejected = loaded.find(result => result.status === "rejected");
  if (rejected?.status === "rejected") throw rejected.reason;
}

export async function resetEditorDraftStorageForTests(preserveDatabase = false) {
  await Promise.allSettled([...loads.values()]);
  while (writing) await writing;
  if (database) (await database).close();
  database = undefined;
  drafts.clear();
  indexes.clear();
  declaredPaths.clear();
  loads.clear();
  pending.clear();
  failure = undefined;
  if (preserveDatabase) return;
  await new Promise<void>((resolve, reject) => {
    const request = indexedDB.deleteDatabase("minicode-editor-drafts");
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}
