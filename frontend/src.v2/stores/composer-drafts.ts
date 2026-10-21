import type { AgentMode, AppStore, ChatMessage, ComposerAttachment, ComposerQuote, ComposerSlice, SkillContextRef } from "./types";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";
import { draftWindowOwnerId } from "./draft-window-owner";
import type { RichComposerDocument } from "../composer/rich-composer-document";

export interface ComposerDraft {
  queuedEdit?: QueuedMessageEdit;
  agentMode?: AgentMode;
  goalMode?: boolean;
  draft: string;
  richDraft?: RichComposerDocument;
  attachments: ComposerAttachment[];
  quotedMessage: ComposerQuote | null;
  selectedMentions: ComposerSlice["selectedMentions"];
  selectedSkills: SkillContextRef[];
  allowedRemoteImageDomains: string[];
}

export interface QueuedMessageEdit {
  ownerId: string;
  workspaceRoot: string;
  original: ChatMessage;
  previousMessageId?: string;
  nextMessageId?: string;
  backup: ComposerDraft;
  backupDiffComments: import("../lib/diff-comments").DiffComment[];
  completed?: boolean;
}

export const emptyComposerDraft = (): ComposerDraft => ({
  agentMode: "build",
  queuedEdit: undefined,
  goalMode: false,
  draft: "", attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [], allowedRemoteImageDomains: [],
});

export const composerDraftOf = (state: ComposerDraft): ComposerDraft => ({
  queuedEdit: state.queuedEdit,
  agentMode: state.agentMode,
  goalMode: state.goalMode,
  draft: state.draft, attachments: state.attachments, quotedMessage: state.quotedMessage,
  ...(state.richDraft ? { richDraft: state.richDraft } : {}),
  selectedMentions: state.selectedMentions, selectedSkills: state.selectedSkills,
  allowedRemoteImageDomains: state.allowedRemoteImageDomains,
});

export const composerWorkspaceKey = (root: string) => normalizeWorkspaceRoot(root);
export const hasComposerDraft = (draft: ComposerDraft) => Boolean(
  draft.queuedEdit || draft.goalMode || (draft.agentMode && draft.agentMode !== "build") || draft.draft || draft.attachments.length || draft.quotedMessage || draft.selectedMentions.length || draft.selectedSkills.length || draft.allowedRemoteImageDomains.length,
);

type DraftOwner = { kind: "conversation" | "workspace" | "side"; id: string; workspaceRoot: string };
type AttachmentRecord = Omit<ComposerAttachment, "localFile"> & { localFileId?: string };
type StoredComposerDraft = Omit<ComposerDraft, "attachments" | "queuedEdit"> & { attachments: AttachmentRecord[];
  queuedEdit?: Omit<QueuedMessageEdit, "backup"> & { backup: StoredComposerDraft } };
type StoredDraft = { key: string; windowOwnerId: string; owner: DraftOwner; draft: StoredComposerDraft; side?: Pick<AppStore["sideChats"][string], "inheritedContext" | "parentConversationId" | "selectedContext" | "contextRefs" | "model" | "permissionMode"> };
type RecoveredDraft = Omit<StoredDraft, "draft"> & { draft: ComposerDraft };
type DraftSnapshot = { records: Map<string, StoredDraft>; files: Map<string, File>; revision: number };
type FileRecord = { id: string; file: Blob; name: string; lastModified: number };
type StorageStatus = { pending: boolean; revision: number; error: string | null };
const DATABASE_NAME = "minicode-composer-drafts";
let database: Promise<IDBDatabase> | undefined;
let pending: DraftSnapshot | null = null;
let writing: Promise<void> | null = null;
let failure: unknown;
let committedRevision = 0;
let statusListener: ((status: StorageStatus) => void) | undefined;
const storedKeys = new Set<string>();
const storedFiles = new Set<string>();
const storedFileReferences = new Set<string>();
const deletions = new Map<string, { promise?: Promise<void>; error?: unknown }>();

const storedAttachmentRecords = (draft: StoredComposerDraft): AttachmentRecord[] => [
  ...draft.attachments, ...(draft.queuedEdit ? storedAttachmentRecords(draft.queuedEdit.backup) : []),
];

function storageFailure(): unknown {
  return failure !== undefined ? failure : [...deletions.values()].find(operation => operation.error !== undefined)?.error;
}

function publishStorageStatus() {
  const error = storageFailure();
  statusListener?.({ pending: hasPendingComposerDrafts(), revision: committedRevision,
    error: error === undefined ? null : error instanceof Error ? error.message : String(error) });
}

export const composerWindowOwnerId = draftWindowOwnerId;
const draftKey = (owner: DraftOwner) => JSON.stringify([composerWindowOwnerId(), owner.kind, owner.id]);

function openDatabase(): Promise<IDBDatabase> {
  return database ??= new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, 1);
    request.onupgradeneeded = () => {
      const records = request.result.createObjectStore("drafts", { keyPath: "key" });
      records.createIndex("window", "windowOwnerId");
      records.createIndex("fileIds", "fileIds", { multiEntry: true });
      records.createIndex("owner", "owner.id");
      request.result.createObjectStore("files", { keyPath: "id" });
      request.result.createObjectStore("deletedConversations");
    };
    request.onsuccess = () => {
      const opened = request.result;
      opened.onversionchange = () => { opened.close(); database = undefined; };
      resolve(opened);
    };
    request.onerror = () => { database = undefined; reject(request.error); };
  });
}

function committed(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error);
  });
}

function result<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function loadComposerDrafts(): Promise<RecoveredDraft[]> {
  const db = await openDatabase();
  const transaction = db.transaction(["drafts", "files", "deletedConversations"], "readonly");
  const completion = committed(transaction);
  const [, { records, fileRecords }] = await Promise.all([completion, (async () => {
    const [savedRecords, deletedOwners] = await Promise.all([
      result<StoredDraft[]>(transaction.objectStore("drafts").index("window").getAll(composerWindowOwnerId())),
      result<IDBValidKey[]>(transaction.objectStore("deletedConversations").getAllKeys()),
    ]);
    const deleted = new Set(deletedOwners);
    const records = savedRecords.filter(record => record.owner.kind === "workspace" || !deleted.has(record.owner.id));
    const fileIds = new Set(records.flatMap(record => storedAttachmentRecords(record.draft).flatMap(attachment => attachment.localFileId ? [attachment.localFileId] : [])));
    const fileRecords = await Promise.all([...fileIds].map(id => result<FileRecord | undefined>(transaction.objectStore("files").get(id))));
    return { records, fileRecords };
  })()]);
  const files = new Map(fileRecords.filter((record): record is FileRecord => Boolean(record)).map(record => [record.id, record]));
  files.forEach(record => storedFiles.add(record.id));
  records.forEach(record => storedKeys.add(record.key));
  records.forEach(record => storedAttachmentRecords(record.draft).forEach(attachment => {
    if (attachment.localFileId) storedFileReferences.add(attachment.localFileId);
  }));
  const restore = (draft: StoredComposerDraft): ComposerDraft => ({ ...draft,
    queuedEdit: draft.queuedEdit ? { ...draft.queuedEdit, backup: restore(draft.queuedEdit.backup) } : undefined,
    attachments: draft.attachments.map(attachment => {
      const saved = attachment.localFileId ? files.get(attachment.localFileId) : undefined;
      if (attachment.localFileId && !saved) throw new Error(`附件恢复数据缺失：${attachment.name}`);
      const localFile = saved ? new File([saved.file], saved.name, { type: saved.file.type, lastModified: saved.lastModified }) : undefined;
      return { ...attachment, localFile,
        dataUrl: localFile?.type.startsWith("image/") ? URL.createObjectURL(localFile) : attachment.dataUrl,
        ...(attachment.status === "uploading" ? { status: "error" as const, uploadPhase: undefined, progress: undefined, error: "上传已中断，可点击重试。" } : {}),
      };
    }),
  });
  return records.map(record => ({ ...record, draft: restore(record.draft) }));
}

export function collectComposerDrafts(state: AppStore, revision: number): DraftSnapshot {
  const records = new Map<string, StoredDraft>();
  const files = new Map<string, File>();
  const workspaces = new Map(state.conversations.map(conversation => [conversation.id, conversation.workspaceRoot || ""]));
  const add = (owner: DraftOwner, draft: ComposerDraft, side?: StoredDraft["side"]) => {
    const key = draftKey(owner);
    records.delete(key);
    if (!hasComposerDraft(draft) && !(side?.selectedContext || side?.contextRefs?.length)) return;
    const serialize = (input: ComposerDraft): StoredComposerDraft => ({ ...composerDraftOf(input),
      queuedEdit: input.queuedEdit ? { ...input.queuedEdit, backup: serialize(input.queuedEdit.backup) } : undefined,
      attachments: input.attachments.map(attachment => {
        const { localFile, dataUrl, ...metadata } = attachment;
        if (localFile) files.set(attachment.id, localFile);
        return { ...metadata, ...(localFile ? { localFileId: attachment.id } : {}),
          ...(dataUrl && !dataUrl.startsWith("blob:") ? { dataUrl } : {}) };
      }),
    });
    records.set(key, { key, windowOwnerId: composerWindowOwnerId(), owner, side,
      draft: serialize(draft) });
  };
  for (const [id, draft] of Object.entries(state.workspaceComposerDrafts)) add({ kind: "workspace", id, workspaceRoot: id }, draft);
  for (const [id, draft] of Object.entries(state.conversationWorkbenchStates)) add({ kind: "conversation", id,
    workspaceRoot: workspaces.get(id) || "" }, draft);
  for (const thread of Object.values(state.sideChats)) add({ kind: "side", id: thread.id, workspaceRoot: thread.workspaceRoot || "" }, {
    ...emptyComposerDraft(), draft: thread.draft, richDraft: thread.richDraft, attachments: thread.attachments || [],
  }, { inheritedContext: thread.inheritedContext, parentConversationId: thread.parentConversationId, selectedContext: thread.selectedContext, contextRefs: thread.contextRefs,
    model: thread.model, permissionMode: thread.permissionMode });
  add(state.conversationId ? { kind: "conversation", id: state.conversationId, workspaceRoot: state.workingDirectory }
    : { kind: "workspace", id: composerWorkspaceKey(state.workingDirectory), workspaceRoot: state.workingDirectory }, state);
  return { records, files, revision };
}

const draftFields = ["queuedEdit", "agentMode", "goalMode", "draft", "richDraft", "attachments", "quotedMessage", "selectedMentions", "selectedSkills", "allowedRemoteImageDomains"] as const;
const sameDraft = (left: ComposerDraft, right: ComposerDraft) => draftFields.every(field => left[field] === right[field]);
const sameDraftMap = (left: Record<string, ComposerDraft>, right: Record<string, ComposerDraft>) => left === right || (
  Object.keys(left).length === Object.keys(right).length && Object.keys(left).every(key => right[key] && sameDraft(left[key], right[key]))
);
export const selectComposerDraftInputs = (state: AppStore) => ({
  ready: state.composerDraftsReady, conversationId: state.conversationId, workspace: state.workingDirectory,
  main: composerDraftOf(state), projects: state.workspaceComposerDrafts,
  workbenches: state.conversationWorkbenchStates, sideChats: state.sideChats,
});
export const composerDraftInputsEqual = (left: ReturnType<typeof selectComposerDraftInputs>, right: ReturnType<typeof selectComposerDraftInputs>) => (
  left.ready === right.ready && left.conversationId === right.conversationId && left.workspace === right.workspace
  && sameDraft(left.main, right.main) && sameDraftMap(left.projects, right.projects) && sameDraftMap(left.workbenches, right.workbenches)
  && (left.sideChats === right.sideChats || (Object.keys(left.sideChats).length === Object.keys(right.sideChats).length
    && Object.keys(left.sideChats).every(id => {
      const a = left.sideChats[id], b = right.sideChats[id];
      return b && a.draft === b.draft && a.richDraft === b.richDraft && a.attachments === b.attachments && a.inheritedContext === b.inheritedContext && a.parentConversationId === b.parentConversationId
        && a.selectedContext === b.selectedContext && a.contextRefs === b.contextRefs && a.workspaceRoot === b.workspaceRoot
        && a.model === b.model && a.permissionMode === b.permissionMode;
    })))
);

export function watchComposerDraftStorage(listener: (status: StorageStatus) => void) {
  statusListener = listener;
}

export function persistComposerDrafts(snapshot: DraftSnapshot) {
  pending = snapshot;
  publishStorageStatus();
  startWriting();
}

function startWriting() {
  if (writing) return;
  publishStorageStatus();
  writing = (async () => {
    const db = await openDatabase();
    while (pending) {
      const next = pending;
      const transaction = db.transaction(["drafts", "files", "deletedConversations"], "readwrite");
      const completion = committed(transaction);
      const records = transaction.objectStore("drafts");
      const files = transaction.objectStore("files");
      const tombstones = transaction.objectStore("deletedConversations");
      const effectiveRecords = new Map([...next.records].filter(([, record]) => record.owner.kind === "workspace" || !deletions.has(record.owner.id)));
      const referencedFiles = new Set<string>();
      const removedFiles: string[] = [];
      await Promise.all([completion, (async () => {
        await Promise.all([...effectiveRecords.values()].filter(record => record.owner.kind !== "workspace").map(async record => {
          if (await result(tombstones.get(record.owner.id))) effectiveRecords.delete(record.key);
        }));
        effectiveRecords.forEach(record => storedAttachmentRecords(record.draft).forEach(attachment => { if (attachment.localFileId) referencedFiles.add(attachment.localFileId); }));
        for (const key of storedKeys) if (!effectiveRecords.has(key)) records.delete(key);
        for (const record of effectiveRecords.values()) records.put({ ...record, fileIds: storedAttachmentRecords(record.draft).flatMap(attachment => attachment.localFileId ? [attachment.localFileId] : []) });
        for (const [id, file] of next.files) if (referencedFiles.has(id) && !storedFiles.has(id)) files.put({ id, file, name: file.name, lastModified: file.lastModified });
        for (const id of storedFileReferences) if (!referencedFiles.has(id)) {
          const reference = records.index("fileIds").getKey(id);
          reference.onsuccess = () => {
            if (reference.result === undefined) { files.delete(id); removedFiles.push(id); }
          };
        }
      })()]);
      failure = undefined;
      storedKeys.clear(); effectiveRecords.forEach(record => storedKeys.add(record.key));
      next.files.forEach((_file, id) => { if (referencedFiles.has(id)) storedFiles.add(id); });
      removedFiles.forEach(id => storedFiles.delete(id));
      storedFileReferences.clear(); referencedFiles.forEach(id => storedFileReferences.add(id));
      committedRevision = next.revision;
      if (pending === next) pending = null;
    }
  })().catch(error => {
    failure = error;
    publishStorageStatus();
  }).finally(() => {
    writing = null;
    if (pending && failure === undefined) startWriting();
    else publishStorageStatus();
  });
}

export const hasPendingComposerDrafts = () => Boolean(writing || pending || deletions.size);
export async function flushComposerDrafts() {
  if (pending && failure !== undefined) startWriting();
  const retries = [...deletions].filter(([, operation]) => !operation.promise && operation.error !== undefined)
    .map(([conversationId]) => deleteConversationComposerDrafts(conversationId));
  await Promise.allSettled(retries);
  let active: Promise<void>[];
  while ((active = [...(writing ? [writing] : []), ...[...deletions.values()].flatMap(operation => operation.promise ? [operation.promise] : [])]).length) {
    await Promise.allSettled(active);
  }
  const error = storageFailure();
  if (error !== undefined) throw error;
}

// An explicit server deletion applies to dormant windows too. The tombstone
// shares the write transaction so a late old-window snapshot cannot resurrect it.
export function deleteConversationComposerDrafts(conversationId: string): Promise<void> {
  const previous = deletions.get(conversationId);
  if (previous?.promise) return previous.promise;
  const operation: { promise?: Promise<void>; error?: unknown } = { error: previous?.error };
  deletions.set(conversationId, operation);
  publishStorageStatus();
  operation.promise = commitConversationDraftDeletion(conversationId).then(() => {
    deletions.delete(conversationId);
  }).catch(error => {
    operation.error = error;
    throw error;
  }).finally(() => {
    operation.promise = undefined;
    publishStorageStatus();
  });
  return operation.promise;
}

async function commitConversationDraftDeletion(conversationId: string) {
  const db = await openDatabase();
  const transaction = db.transaction(["drafts", "files", "deletedConversations"], "readwrite");
  const completion = committed(transaction);
  const records = transaction.objectStore("drafts");
  const removedFiles = new Set<string>();
  const [, owners] = await Promise.all([completion, (async () => {
    transaction.objectStore("deletedConversations").put(true, conversationId);
    const owners = await result<StoredDraft[]>(records.index("owner").getAll(conversationId));
    for (const record of owners) {
      if (record.owner.kind === "workspace") continue;
      records.delete(record.key);
      record.draft.attachments.forEach(attachment => { if (attachment.localFileId) removedFiles.add(attachment.localFileId); });
    }
    for (const id of removedFiles) {
      const reference = records.index("fileIds").getKey(id);
      reference.onsuccess = () => { if (reference.result === undefined) transaction.objectStore("files").delete(id); };
    }
    return owners;
  })()]);
  owners.forEach(record => { if (record.owner.kind !== "workspace") storedKeys.delete(record.key); });
  removedFiles.forEach(id => { storedFiles.delete(id); storedFileReferences.delete(id); });
}

export async function resetComposerDraftStorageForTests(preserveDatabase = false) {
  let active: Promise<void>[];
  while ((active = [...(writing ? [writing] : []), ...[...deletions.values()].flatMap(operation => operation.promise ? [operation.promise] : [])]).length) {
    await Promise.allSettled(active);
  }
  deletions.clear();
  pending = null; failure = undefined;
  storedKeys.clear(); storedFiles.clear(); storedFileReferences.clear(); committedRevision = 0;
  if (preserveDatabase) {
    if (database) (await database).close();
    database = undefined;
    return;
  }
  const db = await openDatabase();
  const transaction = db.transaction(["drafts", "files", "deletedConversations"], "readwrite");
  const completion = committed(transaction);
  for (const name of ["drafts", "files", "deletedConversations"]) transaction.objectStore(name).clear();
  await completion;
  statusListener?.({ pending: false, revision: 0, error: null });
}
