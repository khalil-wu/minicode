import type { AgentMode, AppStore, ChatMessage, ComposerAttachment, ComposerQuote, ComposerSlice, ConversationDraftContext, SkillContextRef } from "./types";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";
import { draftWindowOwnerId } from "./draft-window-owner";
import type { RichComposerDocument } from "../composer/rich-composer-document";

export interface ComposerDraft {
  queuedEdit?: QueuedMessageEdit;
  retainedInputEdit?: RetainedInputEdit;
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

export interface RetainedInputEdit {
  inputId: string;
  backup: ComposerDraft;
  backupDiffComments: import("../lib/diff-comments").DiffComment[];
  completed?: boolean;
}

export interface RetainedComposerInput {
  owner: { kind: "conversation" | "draft"; id: string; workspaceRoot: string };
  message: ChatMessage;
  draft: ComposerDraft;
  comments: import("../lib/diff-comments").DiffComment[];
  sourceInputId?: string;
  unconfirmed?: true;
  /** Only restored records have no still-running local preparation/transport. */
  recovered?: true;
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
  retainedInputEdit: undefined,
  goalMode: false,
  draft: "", attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [], allowedRemoteImageDomains: [],
});

export const composerDraftOf = (state: ComposerDraft): ComposerDraft => ({
  queuedEdit: state.queuedEdit,
  retainedInputEdit: state.retainedInputEdit,
  agentMode: state.agentMode,
  goalMode: state.goalMode,
  draft: state.draft, attachments: state.attachments, quotedMessage: state.quotedMessage,
  ...(state.richDraft ? { richDraft: state.richDraft } : {}),
  selectedMentions: state.selectedMentions, selectedSkills: state.selectedSkills,
  allowedRemoteImageDomains: state.allowedRemoteImageDomains,
});

export const composerDraftAttachments = (draft: Pick<ComposerDraft, "attachments" | "queuedEdit" | "retainedInputEdit">): ComposerAttachment[] => [
  ...draft.attachments, ...(draft.queuedEdit ? composerDraftAttachments(draft.queuedEdit.backup) : []),
  ...(draft.retainedInputEdit ? composerDraftAttachments(draft.retainedInputEdit.backup) : []),
];

/** An upload still belongs to its draft when that draft is temporarily backed up. */
export function updateComposerDraftAttachment<T extends ComposerDraft>(draft: T, id: string, patch: Partial<ComposerAttachment>, seed?: ComposerAttachment, insert = false): T {
  const queuedBackup = draft.queuedEdit ? updateComposerDraftAttachment(draft.queuedEdit.backup, id, patch) : undefined;
  const retainedBackup = draft.retainedInputEdit ? updateComposerDraftAttachment(draft.retainedInputEdit.backup, id, patch) : undefined;
  const nested = queuedBackup !== draft.queuedEdit?.backup || retainedBackup !== draft.retainedInputEdit?.backup;
  const index = draft.attachments.findIndex(attachment => attachment.id === id);
  if (index < 0 && !nested && !(insert && seed)) return draft;
  const attachments = index < 0 ? !nested && insert && seed ? [...draft.attachments, { ...seed, ...patch }] : draft.attachments
    : draft.attachments.map((attachment, position) => position === index ? { ...attachment, ...patch } : attachment);
  return { ...draft, attachments,
    ...(draft.queuedEdit && queuedBackup !== draft.queuedEdit.backup ? { queuedEdit: { ...draft.queuedEdit, backup: queuedBackup! } } : {}),
    ...(draft.retainedInputEdit && retainedBackup !== draft.retainedInputEdit.backup ? { retainedInputEdit: { ...draft.retainedInputEdit, backup: retainedBackup! } } : {}),
  };
}

export const composerWorkspaceKey = (root: string) => normalizeWorkspaceRoot(root);
export const activeComposerDraftKey = (state: Pick<AppStore, "activeConversationDraftId" | "workingDirectory">) =>
  state.activeConversationDraftId || composerWorkspaceKey(state.workingDirectory);
export const hasComposerDraft = (draft: ComposerDraft) => Boolean(
  draft.queuedEdit || draft.retainedInputEdit || draft.goalMode || (draft.agentMode && draft.agentMode !== "build") || draft.draft || draft.attachments.length || draft.quotedMessage || draft.selectedMentions.length || draft.selectedSkills.length || draft.allowedRemoteImageDomains.length,
);

type DraftOwner = { kind: "conversation" | "workspace" | "draft" | "side"; id: string; workspaceRoot: string };
type AttachmentRecord = Omit<ComposerAttachment, "localFile"> & { localFileId?: string };
type StoredComposerDraft = Omit<ComposerDraft, "attachments" | "queuedEdit" | "retainedInputEdit"> & { attachments: AttachmentRecord[];
  queuedEdit?: Omit<QueuedMessageEdit, "backup"> & { backup: StoredComposerDraft };
  retainedInputEdit?: Omit<RetainedInputEdit, "backup"> & { backup: StoredComposerDraft } };
type StoredDraft = { key: string; windowOwnerId: string; owner: DraftOwner; draft: StoredComposerDraft; retainedInput?: Pick<RetainedComposerInput, "message" | "comments" | "sourceInputId" | "unconfirmed">; draftContext?: ConversationDraftContext; side?: Pick<AppStore["sideChats"][string], "inheritedContext" | "parentConversationId" | "selectedContext" | "contextRefs" | "model" | "permissionMode"> };
type RecoveredDraft = Omit<StoredDraft, "draft"> & { draft: ComposerDraft };
type DraftSource = { draft: ComposerDraft; side?: StoredDraft["side"]; context?: ConversationDraftContext; workspaceRoot: string; retainedInput?: RetainedComposerInput };
type DraftSnapshot = { records: Map<string, StoredDraft>; files: Map<string, File>; sources: Map<string, DraftSource>; revision: number };
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
const storedRecords = new Map<string, StoredDraft>();
let collectedSnapshot: DraftSnapshot | undefined;
const storedFiles = new Set<string>();
const storedFileReferences = new Set<string>();
const deletions = new Map<string, { promise?: Promise<void>; error?: unknown }>();
const deletedConversationOwners = new Set<string>();
export const isComposerConversationDeleted = (id: string): boolean => deletedConversationOwners.has(id);

const storedAttachmentRecords = (draft: StoredComposerDraft): AttachmentRecord[] => [
  ...draft.attachments, ...(draft.queuedEdit ? storedAttachmentRecords(draft.queuedEdit.backup) : []),
  ...(draft.retainedInputEdit ? storedAttachmentRecords(draft.retainedInputEdit.backup) : []),
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
const storedConversationOwner = (record: StoredDraft) => record.draftContext?.conversationId
  ?? record.draftContext?.requestedConversationId ?? record.owner.id;

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
    for (const owner of deletedOwners) deletedConversationOwners.add(String(owner));
    const records = savedRecords.filter(record => record.owner.kind === "workspace" || !deleted.has(storedConversationOwner(record)));
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
    retainedInputEdit: draft.retainedInputEdit ? { ...draft.retainedInputEdit, backup: restore(draft.retainedInputEdit.backup) } : undefined,
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
  return records.filter(record => record.owner.kind === "workspace" || !deletedConversationOwners.has(storedConversationOwner(record)))
    .map(record => ({ ...record, draft: restore(record.draft) }));
}

export function collectComposerDrafts(state: AppStore, revision: number): DraftSnapshot {
  const records = new Map<string, StoredDraft>();
  const files = new Map<string, File>();
  const sources = new Map<string, DraftSource>();
  const workspaces = new Map(state.conversations.map(conversation => [conversation.id, conversation.workspaceRoot || ""]));
  const add = (owner: DraftOwner, draft: ComposerDraft, side?: StoredDraft["side"], draftContext?: ConversationDraftContext, retainedInput?: RetainedComposerInput) => {
    const key = retainedInput ? JSON.stringify([composerWindowOwnerId(), "retained-input", retainedInput.message.id]) : draftKey(owner);
    records.delete(key);
    sources.delete(key);
    if (!retainedInput && !draftContext && !hasComposerDraft(draft) && !(side?.selectedContext || side?.contextRefs?.length)) return;
    const source = { draft: composerDraftOf(draft), side, context: draftContext, workspaceRoot: owner.workspaceRoot, retainedInput };
    sources.set(key, source);
    const previous = collectedSnapshot?.sources.get(key);
    // Store updates preserve immutable draft fields. Reuse their serialized
    // records so typing in one chat does not clone/write every dormant draft.
    if (previous && previous.retainedInput === retainedInput && previous.context === draftContext && previous.workspaceRoot === owner.workspaceRoot
      && sameDraft(previous.draft, source.draft) && sameSideDraft(previous.side, side)) {
      const record = collectedSnapshot!.records.get(key)!;
      records.set(key, record);
      for (const attachment of storedAttachmentRecords(record.draft)) {
        if (attachment.localFileId) files.set(attachment.localFileId, collectedSnapshot!.files.get(attachment.localFileId)!);
      }
      return;
    }
    const serialize = (input: ComposerDraft): StoredComposerDraft => ({ ...composerDraftOf(input),
      queuedEdit: input.queuedEdit ? { ...input.queuedEdit, backup: serialize(input.queuedEdit.backup) } : undefined,
      retainedInputEdit: input.retainedInputEdit ? { ...input.retainedInputEdit, backup: serialize(input.retainedInputEdit.backup) } : undefined,
      attachments: input.attachments.map(attachment => {
        const { localFile, dataUrl, ...metadata } = attachment;
        if (localFile) files.set(attachment.id, localFile);
        return { ...metadata, ...(localFile ? { localFileId: attachment.id } : {}),
          ...(dataUrl && !dataUrl.startsWith("blob:") ? { dataUrl } : {}) };
      }),
    });
    records.set(key, { key, windowOwnerId: composerWindowOwnerId(), owner, side,
      ...(retainedInput ? { retainedInput: { comments: retainedInput.comments, sourceInputId: retainedInput.sourceInputId, unconfirmed: retainedInput.unconfirmed, message: { ...retainedInput.message,
        attachmentRefs: retainedInput.message.attachmentRefs?.map(ref => ({ ...ref, dataUrl: ref.dataUrl?.startsWith("blob:") ? undefined : ref.dataUrl })) } } } : {}),
      draftContext: draftContext ? { ...draftContext, pendingUserMessage: undefined } : undefined,
      draft: serialize(draft) });
  };
  for (const [id, draft] of Object.entries(state.workspaceComposerDrafts)) {
    const context = state.conversationDrafts[id];
    add(context ? { kind: "draft", id, workspaceRoot: context.workspaceRoot } : { kind: "workspace", id, workspaceRoot: id }, draft, undefined, context);
  }
  for (const [id, draft] of Object.entries(state.conversationWorkbenchStates)) add({ kind: "conversation", id,
    workspaceRoot: workspaces.get(id) || "" }, draft);
  for (const thread of Object.values(state.sideChats)) add({ kind: "side", id: thread.id, workspaceRoot: thread.workspaceRoot || "" }, {
    ...emptyComposerDraft(), draft: thread.draft, richDraft: thread.richDraft, attachments: thread.attachments || [],
  }, { inheritedContext: thread.inheritedContext, parentConversationId: thread.parentConversationId, selectedContext: thread.selectedContext, contextRefs: thread.contextRefs,
    model: thread.model, permissionMode: thread.permissionMode });
  const context = state.activeConversationDraftId ? state.conversationDrafts[state.activeConversationDraftId] : undefined;
  const boundOwner = context?.adopted ? context.conversationId : state.conversationId;
  add(boundOwner ? { kind: "conversation", id: boundOwner, workspaceRoot: state.workingDirectory }
    : context ? { kind: "draft", id: context.id, workspaceRoot: context.workspaceRoot }
    : { kind: "workspace", id: composerWorkspaceKey(state.workingDirectory), workspaceRoot: state.workingDirectory }, state, undefined, context);
  for (const input of Object.values(state.retainedComposerInputs)) {
    add(input.owner, input.draft, undefined, input.owner.kind === "draft" ? state.conversationDrafts[input.owner.id] : undefined, input);
  }
  return collectedSnapshot = { records, files, sources, revision };
}

const sideDraftFields = ["inheritedContext", "parentConversationId", "selectedContext", "contextRefs", "model", "permissionMode"] as const;
const sameSideDraft = (left: StoredDraft["side"], right: StoredDraft["side"]) => left === right || Boolean(left && right
  && sideDraftFields.every(field => left[field] === right[field]));
const draftFields = ["queuedEdit", "retainedInputEdit", "agentMode", "goalMode", "draft", "richDraft", "attachments", "quotedMessage", "selectedMentions", "selectedSkills", "allowedRemoteImageDomains"] as const;
const sameDraft = (left: ComposerDraft, right: ComposerDraft) => draftFields.every(field => left[field] === right[field]);
const sameDraftMap = (left: Record<string, ComposerDraft>, right: Record<string, ComposerDraft>) => left === right || (
  Object.keys(left).length === Object.keys(right).length && Object.keys(left).every(key => right[key] && sameDraft(left[key], right[key]))
);
export const selectComposerDraftInputs = (state: AppStore) => ({
  ready: state.composerDraftsReady, conversationId: state.conversationId, workspace: state.workingDirectory,
  activeDraftId: state.activeConversationDraftId, draftContexts: state.conversationDrafts,
  retainedInputs: state.retainedComposerInputs,
  main: composerDraftOf(state), projects: state.workspaceComposerDrafts,
  workbenches: state.conversationWorkbenchStates, sideChats: state.sideChats,
});
export const composerDraftInputsEqual = (left: ReturnType<typeof selectComposerDraftInputs>, right: ReturnType<typeof selectComposerDraftInputs>) => (
  left.ready === right.ready && left.conversationId === right.conversationId && left.workspace === right.workspace
  && left.activeDraftId === right.activeDraftId && left.draftContexts === right.draftContexts
  && left.retainedInputs === right.retainedInputs
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
      const effectiveRecords = new Map([...next.records].filter(([, record]) => record.owner.kind === "workspace" || !deletedConversationOwners.has(storedConversationOwner(record))));
      const referencedFiles = new Set<string>();
      const removedFiles: string[] = [];
      await Promise.all([completion, (async () => {
        await Promise.all([...effectiveRecords.values()].filter(record => record.owner.kind !== "workspace").map(async record => {
          if (await result(tombstones.get(storedConversationOwner(record)))) effectiveRecords.delete(record.key);
        }));
        effectiveRecords.forEach(record => storedAttachmentRecords(record.draft).forEach(attachment => { if (attachment.localFileId) referencedFiles.add(attachment.localFileId); }));
        for (const key of storedKeys) if (!effectiveRecords.has(key)) records.delete(key);
        for (const record of effectiveRecords.values()) if (storedRecords.get(record.key) !== record) {
          records.put({ ...record, fileIds: storedAttachmentRecords(record.draft).flatMap(attachment => attachment.localFileId ? [attachment.localFileId] : []) });
        }
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
      storedRecords.clear(); effectiveRecords.forEach(record => storedRecords.set(record.key, record));
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
  deletedConversationOwners.add(conversationId);
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
    const owners = (await result<StoredDraft[]>(records.getAll()))
      .filter(record => record.owner.kind !== "workspace" && storedConversationOwner(record) === conversationId);
    for (const record of owners) {
      if (record.owner.kind === "workspace") continue;
      records.delete(record.key);
      storedAttachmentRecords(record.draft).forEach(attachment => { if (attachment.localFileId) removedFiles.add(attachment.localFileId); });
    }
    for (const id of removedFiles) {
      const reference = records.index("fileIds").getKey(id);
      reference.onsuccess = () => { if (reference.result === undefined) transaction.objectStore("files").delete(id); };
    }
    return owners;
  })()]);
  owners.forEach(record => { if (record.owner.kind !== "workspace") { storedKeys.delete(record.key); storedRecords.delete(record.key); } });
  removedFiles.forEach(id => { storedFiles.delete(id); storedFileReferences.delete(id); });
}

export async function resetComposerDraftStorageForTests(preserveDatabase = false) {
  let active: Promise<void>[];
  while ((active = [...(writing ? [writing] : []), ...[...deletions.values()].flatMap(operation => operation.promise ? [operation.promise] : [])]).length) {
    await Promise.allSettled(active);
  }
  deletions.clear();
  deletedConversationOwners.clear();
  pending = null; failure = undefined;
  collectedSnapshot = undefined; storedRecords.clear();
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
