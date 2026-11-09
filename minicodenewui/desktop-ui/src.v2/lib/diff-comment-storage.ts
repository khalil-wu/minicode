import { windowDraftWorkspaceKey } from "../stores/draft-window-owner";
import { normalizeWorkspaceRoot } from "./workspace-path";
import { cloneDiffComments, diffCommentKey, mergeDiffComments, type DiffComment, type DiffCommentDraft } from "./diff-comments";

const storagePrefix = "minicode.diff_comments.";

export interface DiffCommentState {
  comments: DiffComment[];
  drafts: Record<string, DiffCommentDraft>;
  revision: number;
}

interface Entry { raw: string | null; snapshot: DiffCommentState }
const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();

/** New chats share the existing window-owned workspace composer identity. */
export const getDiffCommentScope = (conversationId: string | null | undefined, workingDirectory: string): string =>
  conversationId || `workspace:${windowDraftWorkspaceKey(normalizeWorkspaceRoot(workingDirectory))}`;

function readEntry(scope: string): Entry {
  const raw = localStorage.getItem(storagePrefix + scope);
  const entry = entries.get(scope);
  if (entry?.raw === raw) return entry;
  const next = { raw, snapshot: {
    comments: raw === null ? [] : JSON.parse(raw) as DiffComment[],
    drafts: entry?.snapshot.drafts ?? {},
    revision: entry ? entry.snapshot.revision + 1 : 0,
  } };
  entries.set(scope, next);
  return next;
}

export const readDiffCommentState = (scope: string): DiffCommentState => readEntry(scope).snapshot;

const emit = () => { for (const listener of listeners) listener(); };

function onStorage(event: StorageEvent) {
  if (event.key !== null && !event.key.startsWith(storagePrefix)) return;
  let changed = false;
  for (const [scope, entry] of entries) {
    if (event.key !== null && event.key !== storagePrefix + scope) continue;
    if (readEntry(scope) !== entry) changed = true;
  }
  if (changed) emit();
}

export function subscribeDiffComments(listener: () => void): () => void {
  if (listeners.size === 0) window.addEventListener("storage", onStorage);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener("storage", onStorage);
  };
}

export function setDiffComments(scope: string, value: readonly DiffComment[] | ((comments: DiffComment[]) => readonly DiffComment[])): void {
  const entry = readEntry(scope);
  const comments = cloneDiffComments(typeof value === "function" ? value(entry.snapshot.comments) : value);
  const raw = comments.length === 0 ? null : JSON.stringify(comments);
  if (raw === null) localStorage.removeItem(storagePrefix + scope);
  else localStorage.setItem(storagePrefix + scope, raw);
  entries.set(scope, { raw, snapshot: { ...entry.snapshot, comments, revision: entry.snapshot.revision + 1 } });
  emit();
}

export function saveDiffComment(scope: string, comment: DiffComment, previous?: DiffComment): void {
  setDiffComments(scope, comments => {
    if (!previous) return [...comments, comment];
    const key = diffCommentKey(previous);
    return comments.map(item => diffCommentKey(item) === key ? comment : item);
  });
}

export function removeDiffComment(scope: string, key: string): void {
  setDiffComments(scope, comments => comments.filter(comment => diffCommentKey(comment) !== key));
}

/** Delete only the submitted immutable snapshot; a later edit is a new key. */
export function clearSubmittedDiffComments(scope: string, submitted: readonly DiffComment[]): void {
  const keys = new Set(submitted.map(diffCommentKey));
  setDiffComments(scope, comments => comments.filter(comment => !keys.has(diffCommentKey(comment))));
}

/** The composer owns the submitted draft token; this only merges its context. */
export function restoreSubmittedDiffComments(scope: string, submitted: readonly DiffComment[]): void {
  setDiffComments(scope, comments => mergeDiffComments(comments, submitted));
}

export function transferDiffComments(fromScope: string, toScope: string): void {
  if (fromScope === toScope) return;
  const incoming = readDiffCommentState(fromScope).comments;
  if (incoming.length > 0) setDiffComments(toScope, comments => mergeDiffComments(comments, incoming));
  setDiffComments(fromScope, []);
}

export function clearDiffCommentScope(scope: string): void {
  const entry = readEntry(scope);
  localStorage.removeItem(storagePrefix + scope);
  entries.set(scope, { raw: null, snapshot: { comments: [], drafts: {}, revision: entry.snapshot.revision + 1 } });
  emit();
}

export function setDiffCommentDraft(scope: string, draft: DiffCommentDraft): void {
  const entry = readEntry(scope);
  const snapshot = { ...entry.snapshot, drafts: { ...entry.snapshot.drafts, [draft.key]: {
    ...draft, position: { ...draft.position },
  } }, revision: entry.snapshot.revision + 1 };
  entries.set(scope, { ...entry, snapshot });
  emit();
}

export function removeDiffCommentDraft(scope: string, key: string): void {
  const entry = readEntry(scope);
  const { [key]: removed, ...drafts } = entry.snapshot.drafts;
  if (!removed) return;
  entries.set(scope, { ...entry, snapshot: { ...entry.snapshot, drafts, revision: entry.snapshot.revision + 1 } });
  emit();
}

/** A reader host owns all of its source views, including currently hidden scopes. */
export function clearDiffReaderDrafts(readerOwnerId: string): void {
  let changed = false;
  for (const scope of entries.keys()) {
    const entry = readEntry(scope);
    const drafts = Object.fromEntries(Object.entries(entry.snapshot.drafts)
      .filter(([, draft]) => !draft.ownerId.startsWith(`${readerOwnerId}::`)));
    if (Object.keys(drafts).length === Object.keys(entry.snapshot.drafts).length) continue;
    entries.set(scope, { ...entry, snapshot: { ...entry.snapshot, drafts, revision: entry.snapshot.revision + 1 } });
    changed = true;
  }
  if (changed) emit();
}
