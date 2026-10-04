import type * as Monaco from "monaco-editor/editor/editor.api.js";
import type { EditorTab } from "../stores/types";
import type { WorkspaceProjectIndex, WorkspaceProjectIndexFile } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { editorPathsEqual } from "../stores/shared-helpers";
import { isWindowsLikeWorkspacePath, workspacePathWithin, workspaceRootsEqual } from "../lib/workspace-path";
import { editorModelUri, setWorkspaceTypeScriptFiles } from "./monacoLanguageServices";
import { installWorkspaceBulkEditUndo } from "./workspaceBulkEditUndo";
import { editorFileLimitReason } from "../lib/editor-file-policy";

export const isDependencyIndexPath = (path: string) => /(?:^|[/\\])node_modules(?:[/\\]|$)/i.test(path);
export const isIndexedSource = (file: WorkspaceProjectIndexFile) => (file.kind === "source" || file.kind === "declaration") && !isDependencyIndexPath(file.path);
export const isTypeScriptSourcePath = (path: string) => /\.[cm]?[jt]sx?$/i.test(path);
const modelLanguage = (path: string) => /\.[cm]?tsx?$/i.test(path) ? "typescript" : "javascript";

type OwnedModel = { model: Monaco.editor.ITextModel; listener: Monaco.IDisposable; disposal: Monaco.IDisposable };

/** Owns real source models without opening tabs. Native bulk edits become
 * ordinary dirty buffers; disk snapshots never replace a user's draft. */
export class WorkspaceModelIndex {
  private files = new Map<string, WorkspaceProjectIndexFile>();
  private diskKeys = new Set<string>();
  private models = new Map<string, OwnedModel>();
  private pending = new Set<string>();
  private suppressChanges = false;
  private disposed = false;
  private lastTabs: EditorTab[];
  private subscription: () => void;
  private modelCreation: Monaco.IDisposable;
  private bulkUndo: Monaco.IDisposable;

  constructor(readonly monaco: typeof Monaco, readonly workspaceRoot: string, private readonly store = useAppStore) {
    this.lastTabs = store.getState().editorTabs;
    this.addOpenBuffers(this.lastTabs);
    this.publish();
    this.modelCreation = monaco.editor.onDidCreateModel((model) => this.attachModel(model));
    for (const model of monaco.editor.getModels()) this.attachModel(model);
    this.subscription = store.subscribe((state) => {
      if (workspaceRootsEqual(state.workingDirectory, workspaceRoot) && state.editorTabs !== this.lastTabs) this.syncTabs(state.editorTabs);
    });
    this.bulkUndo = installWorkspaceBulkEditUndo(monaco, (uri) => {
      const file = this.files.get(this.key(uri));
      return Boolean(file && isIndexedSource(file));
    });
  }

  private uri(path: string): Monaco.Uri { return this.monaco.Uri.parse(editorModelUri(path, this.workspaceRoot)); }
  private key(uri: Monaco.Uri): string { const name = uri.toString(); return isWindowsLikeWorkspacePath(this.workspaceRoot) ? name.toLowerCase() : name; }
  private tabFor(path: string): EditorTab | undefined { return this.store.getState().editorTabs.find((tab) => editorPathsEqual(tab.path, path, this.workspaceRoot)); }
  private eligibleTab(tab: EditorTab): boolean {
    return !tab.loading && !tab.error && !tab.largeFile && !tab.readOnly && isTypeScriptSourcePath(tab.path)
      && workspacePathWithin(this.uri(tab.path).fsPath, this.workspaceRoot) && !isDependencyIndexPath(tab.path);
  }
  private tabSnapshot(tab: EditorTab): WorkspaceProjectIndexFile {
    return { path: tab.path, content: tab.original, content_hash: tab.contentHash ?? "", size_bytes: tab.sizeBytes,
      kind: /\.d\.[cm]?ts$/i.test(tab.path) ? "declaration" : "source" };
  }
  private addOpenBuffers(tabs: EditorTab[]): boolean {
    let added = false;
    for (const tab of tabs) {
      if (!this.eligibleTab(tab)) continue;
      const key = this.key(this.uri(tab.path));
      if (!this.files.has(key)) { this.files.set(key, this.tabSnapshot(tab)); added = true; }
    }
    return added;
  }

  private attachModel(model: Monaco.editor.ITextModel): void {
    const key = this.key(model.uri);
    const file = this.files.get(key);
    if (!file || !isIndexedSource(file) || this.models.get(key)?.model === model) return;
    const listener = model.onDidChangeContent(() => {
      if (this.suppressChanges || this.disposed) return;
      this.pending.add(key);
      if (this.pending.size === 1) queueMicrotask(() => this.flushChanges());
    });
    const disposal = model.onWillDispose(() => {
      this.flushChanges();
      listener.dispose();
      disposal.dispose();
      this.models.delete(key);
    });
    this.models.set(key, { model, listener, disposal });
  }

  private flushChanges(): void {
    const changes = [];
    for (const key of this.pending) {
      const file = this.files.get(key);
      const owned = this.models.get(key);
      if (file && owned) changes.push({ path: file.path, content: owned.model.getValue(undefined, true), original: file.content,
        contentHash: file.content_hash, sizeBytes: file.size_bytes ?? file.size });
    }
    this.pending.clear();
    if (changes.length) this.store.getState().adoptEditorModelChanges(changes, this.workspaceRoot);
  }

  private setModelValue(model: Monaco.editor.ITextModel, content: string): void {
    if (model.getValue(undefined, true) === content) return;
    this.suppressChanges = true;
    model.setValue(content);
    this.suppressChanges = false;
  }

  private syncTabs(tabs: EditorTab[]): void {
    const previous = this.lastTabs;
    this.lastTabs = tabs;
    let changedNames = tabs.some((tab) => tab.readOnly !== previous.find((entry) => entry.id === tab.id)?.readOnly)
      || previous.some((tab) => tab.readOnly && !tabs.some((entry) => entry.id === tab.id));
    for (const tab of tabs) {
      if (!this.eligibleTab(tab)) continue;
      const old = previous.find((entry) => entry.id === tab.id);
      if (old && !editorPathsEqual(old.path, tab.path, this.workspaceRoot)) {
        this.files.delete(this.key(this.uri(old.path)));
        changedNames = true;
      }
      const key = this.key(this.uri(tab.path));
      const record = this.files.get(key);
      if (!record) { this.files.set(key, this.tabSnapshot(tab)); changedNames = true; }
      else if (old && (old.original !== tab.original || old.contentHash !== tab.contentHash)) this.files.set(key, this.tabSnapshot(tab));
      const model = this.monaco.editor.getModel(this.uri(tab.path));
      if (model) {
        this.attachModel(model);
        if (!old || old.content !== tab.content) this.setModelValue(model, tab.content);
      }
    }
    for (const tab of previous) {
      if (tabs.some((entry) => entry.id === tab.id) || !this.eligibleTab(tab)) continue;
      const key = this.key(this.uri(tab.path));
      const file = this.files.get(key);
      const owned = this.models.get(key);
      if (!this.diskKeys.has(key)) {
        this.files.delete(key);
        owned?.model.dispose();
        changedNames = true;
      } else if (file && owned) this.setModelValue(owned.model, tab.externalChanged ? file.content : tab.original);
      else if (owned) owned.model.dispose();
    }
    if (changedNames) this.publish();
  }

  async applySnapshot(snapshot: WorkspaceProjectIndex, includeDependencies: boolean, signal: AbortSignal): Promise<Array<{ path: string; message: string }>> {
    if (signal.aborted || this.disposed) return [];
    this.flushChanges();
    const issues: Array<{ path: string; message: string }> = [];
    const sources: WorkspaceProjectIndexFile[] = [];
    const next = new Map<string, WorkspaceProjectIndexFile>();
    if (!includeDependencies) for (const [key, file] of this.files) if (isDependencyIndexPath(file.path)) next.set(key, file);
    for (const file of snapshot.files) {
      if (isIndexedSource(file)) {
        const warning = editorFileLimitReason(file.content, file.size_bytes ?? file.size);
        if (warning) {
          issues.push({ path: file.path, message: warning });
          const tab = this.tabFor(file.path);
          if (tab && tab.content !== tab.original) {
            this.store.getState().markTabExternalChanged(tab.path, { workspaceRoot: this.workspaceRoot });
          } else {
            if (tab) this.store.getState().markTabLoaded(tab.path, "", undefined, file.content_hash,
              { sizeBytes: file.size_bytes ?? file.size, largeFile: true, loadWarning: warning, readOnly: tab.readOnly });
            this.monaco.editor.getModel(this.uri(file.path))?.dispose();
          }
          continue;
        }
        sources.push(file);
      }
      next.set(this.key(this.uri(file.path)), file);
    }
    this.files = next;
    this.diskKeys = new Set(next.keys());
    this.addOpenBuffers(this.store.getState().editorTabs);
    let processed = 0;
    for (const file of sources) {
      if (signal.aborted || this.disposed) return issues;
      const tab = this.tabFor(file.path);
      const dirty = tab && tab.content !== tab.original;
      const model = this.monaco.editor.getModel(this.uri(file.path)) ?? this.monaco.editor.createModel(dirty ? tab.content : file.content, modelLanguage(file.path), this.uri(file.path));
      this.attachModel(model);
      if (dirty) {
        this.setModelValue(model, tab.content);
        this.store.getState().markTabExternalChanged(tab.path, { workspaceRoot: this.workspaceRoot, changed: file.content !== tab.original });
      } else if (tab) {
        if (tab.original !== file.content || tab.contentHash !== file.content_hash) this.store.getState().markTabLoaded(tab.path, file.content, undefined, file.content_hash, { sizeBytes: file.size_bytes ?? file.size, readOnly: tab.readOnly });
        this.setModelValue(model, file.content);
      } else this.setModelValue(model, file.content);
      if (++processed % 80 === 0) await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    }
    for (const [key, owned] of [...this.models]) {
      if (this.files.has(key)) continue;
      const tab = this.store.getState().editorTabs.find((entry) => this.key(this.uri(entry.path)) === key);
      if (tab) this.store.getState().markTabExternalChanged(tab.path, { workspaceRoot: this.workspaceRoot });
      else owned.model.dispose();
    }
    if (!signal.aborted && !this.disposed) this.publish();
    return issues;
  }

  addConfigurations(files: WorkspaceProjectIndexFile[]): void {
    for (const file of files) {
      const key = this.key(this.uri(file.path));
      this.files.set(key, file); this.diskKeys.add(key);
    }
  }

  noteFileChanges(changes: Array<{ path: string; event: string }>): void {
    this.flushChanges();
    let changed = false;
    for (const change of changes) {
      if (!/^(deleted|delete|removed|remove)$/i.test(change.event)) continue;
      const key = this.key(this.uri(change.path));
      this.diskKeys.delete(key);
      const tab = this.tabFor(change.path);
      if (tab) this.store.getState().markTabExternalChanged(tab.path, { workspaceRoot: this.workspaceRoot });
      else {
        this.files.delete(key);
        this.models.get(key)?.model.dispose();
        changed = true;
      }
    }
    if (changed) this.publish();
  }

  publish(): void {
    const sourceFileNames = [...this.files.values()].filter(isIndexedSource).map((file) => this.uri(file.path).toString());
    const readOnlyFileNames = this.store.getState().editorTabs.filter((tab) => tab.readOnly).map((tab) => this.uri(tab.path).toString());
    setWorkspaceTypeScriptFiles({ workspaceRoot: this.uri(".").toString(), sourceFileNames, readOnlyFileNames, caseSensitive: !isWindowsLikeWorkspacePath(this.workspaceRoot) },
      [...this.files.values()].map((file) => ({ filePath: this.uri(file.path).toString(), content: file.content })));
  }
  resources(): Monaco.Uri[] { return [...this.models.values()].map(({ model }) => model.uri); }
  ownsModel(path: string): boolean { return this.models.has(this.key(this.uri(path))); }
  sourceCount(): number { return [...this.files.entries()].filter(([key, file]) => this.diskKeys.has(key) && isIndexedSource(file)).length; }
  configurationPath(uri: string): string { return this.monaco.Uri.parse(uri).fsPath; }

  dispose(): void {
    this.flushChanges();
    this.disposed = true;
    this.subscription();
    this.modelCreation.dispose();
    this.bulkUndo.dispose();
    for (const owned of this.models.values()) {
      owned.listener.dispose(); owned.disposal.dispose(); owned.model.dispose();
    }
    this.models.clear();
    setWorkspaceTypeScriptFiles({ workspaceRoot: this.uri(".").toString(), sourceFileNames: [], readOnlyFileNames: [], caseSensitive: !isWindowsLikeWorkspacePath(this.workspaceRoot) }, []);
  }
}
