import type { EditorTab } from "./types";
import { pushToast } from "../overlays/ToastContainer";

type DraftRecord = { workspace: string; path: string; tab: EditorTab };
export type EditorWorkspaceIndex = {
  workspace: string;
  paths: string[];
  metadata: Record<string, Pick<EditorTab, "pinned" | "preview" | "lastActivated" | "language" | "recoveryPath">>;
  activeTabPath: string | null;
  activeEditorPath: string | null;
};
type DraftSnapshot = { tabs: EditorTab[]; legacy: EditorTab[]; legacyKey: string; legacyIndex?: EditorWorkspaceIndex; index: EditorWorkspaceIndex; closed: Set<string> };
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
    transaction.onerror = () => reject(transaction.error);
  });
}

export const cachedEditorDrafts = (workspace: string) => drafts.get(workspace);

function mergedIndex(saved: EditorWorkspaceIndex, snapshot: DraftSnapshot): EditorWorkspaceIndex {
  const paths = [...snapshot.index.paths, ...saved.paths.filter(path => !snapshot.index.paths.includes(path) && !snapshot.closed.has(path))];
  return { ...saved, ...snapshot.index, paths,
    metadata: Object.fromEntries(paths.map(path => [path, snapshot.index.metadata[path] ?? saved.metadata[path]])),
  };
}

export function cachedEditorWorkspaceIndex(workspace: string) {
  const saved = indexes.get(workspace);
  const snapshot = pending.get(workspace);
  return saved && snapshot ? mergedIndex(saved, snapshot) : saved ?? snapshot?.index;
}

export function loadEditorDrafts(workspace: string, legacy: EditorTab[], legacyKey: string, legacyIndex?: EditorWorkspaceIndex) {
  if (loads.has(workspace)) return loads.get(workspace)!;
  const loading = (async () => {
    const db = await openDatabase();
    const transaction = db.transaction(["drafts", "workspaces"], "readonly");
    const request = transaction.objectStore("drafts").index("workspace").getAll(workspace);
    const indexRequest = transaction.objectStore("workspaces").get(workspace);
    const recordsPromise = new Promise<DraftRecord[]>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const indexPromise = new Promise<EditorWorkspaceIndex | undefined>((resolve, reject) => {
      indexRequest.onsuccess = () => resolve(indexRequest.result);
      indexRequest.onerror = () => reject(indexRequest.error);
    });
    const [records, storedIndex] = await Promise.all([recordsPromise, indexPromise]);
    const saved = new Map(records.map(record => [record.path, record.tab]));
    const migration = legacy.filter(tab => !saved.has(tab.path));
    // Version 1 stored bodies separately from its localStorage index. Recover
    // any committed body whose old index was lost to the proven LS quota bug.
    const bodyMetadata = Object.fromEntries([...saved.values(), ...legacy].map(tab => [tab.path, {
        pinned: tab.pinned, preview: tab.preview, lastActivated: tab.lastActivated, language: tab.language,
      }]));
    const index = storedIndex ?? { workspace,
      paths: [...new Set([...(legacyIndex?.paths ?? []), ...saved.keys(), ...legacy.map(tab => tab.path)])],
      metadata: { ...bodyMetadata, ...legacyIndex?.metadata },
      activeTabPath: legacyIndex?.activeTabPath ?? records[0]?.path ?? legacy[0]?.path ?? null,
      activeEditorPath: legacyIndex?.activeEditorPath ?? null };
    if (migration.length || !storedIndex) {
      const transaction = db.transaction(["drafts", "workspaces"], "readwrite");
      for (const tab of migration) transaction.objectStore("drafts").put({ workspace, path: tab.path, tab });
      transaction.objectStore("workspaces").put(index);
      await committed(transaction);
      for (const tab of migration) saved.set(tab.path, tab);
    }
    // The old body is released only after its replacement transaction commits.
    if (legacy.length) localStorage.removeItem(legacyKey);
    drafts.set(workspace, saved);
    indexes.set(workspace, index);
    return saved;
  })().catch(error => { loads.delete(workspace); throw error; });
  loads.set(workspace, loading);
  return loading;
}

export function persistEditorDrafts(workspace: string, tabs: EditorTab[], legacy: EditorTab[], legacyKey: string, index: EditorWorkspaceIndex, legacyIndex?: EditorWorkspaceIndex) {
  const previous = pending.get(workspace);
  const before = declaredPaths.get(workspace) ?? legacyIndex?.paths ?? [];
  const closed = new Set([...(previous?.closed ?? []), ...before.filter(path => !index.paths.includes(path))]);
  for (const path of index.paths) closed.delete(path);
  declaredPaths.set(workspace, index.paths);
  pending.set(workspace, { tabs, legacy, legacyKey, index, legacyIndex, closed });
  startWriting();
}

function startWriting() {
  if (writing) return;
  failure = undefined;
  writing = (async () => {
    const db = await openDatabase();
    while (pending.size) {
      const [workspace, snapshot] = pending.entries().next().value!;
      const saved = await loadEditorDrafts(workspace, snapshot.legacy, snapshot.legacyKey, snapshot.legacyIndex);
      const latest = pending.get(workspace)!;
      const index = mergedIndex(indexes.get(workspace)!, latest);
      const paths = new Set([...index.paths, ...latest.tabs.flatMap(tab => tab.recoveryPending && tab.recoveryPath ? [tab.recoveryPath] : [])]);
      const changes = latest.tabs.filter(tab => !tab.recoveryPending).filter(tab => {
        const previous = saved.get(tab.path);
        if (tab.readOnly || tab.largeFile || tab.content === tab.original) return Boolean(previous);
        return tab.content !== previous?.content || tab.original !== previous?.original
          || tab.contentHash !== previous?.contentHash || tab.pendingBufferTransactions !== previous?.pendingBufferTransactions;
      });
      const removed = [...saved.keys()].filter(path => !paths.has(path));
      const indexChanged = JSON.stringify(index) !== JSON.stringify(indexes.get(workspace));
      if (!changes.length && !removed.length && !indexChanged) {
        if (pending.get(workspace) === latest) pending.delete(workspace);
        continue;
      }
      const transaction = db.transaction(["drafts", "workspaces"], "readwrite");
      const records = transaction.objectStore("drafts");
      transaction.objectStore("workspaces").put(index);
      for (const path of removed) records.delete([workspace, path]);
      for (const tab of changes) {
        if (!tab.readOnly && !tab.largeFile && tab.content !== tab.original) records.put({ workspace, path: tab.path, tab });
        else records.delete([workspace, tab.path]);
      }
      // Update memory only after commit; a failed commit retains the authored
      // snapshot and propagates to flush instead of claiming it is saved.
      await committed(transaction);
      for (const path of removed) saved.delete(path);
      for (const tab of changes) {
        if (!tab.readOnly && !tab.largeFile && tab.content !== tab.original) saved.set(tab.path, tab);
        else saved.delete(tab.path);
      }
      indexes.set(workspace, index);
      if (pending.get(workspace) === latest) pending.delete(workspace);
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
