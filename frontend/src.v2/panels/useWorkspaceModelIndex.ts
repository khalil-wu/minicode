import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { useAppStore } from "../stores";
import { ApiError } from "../protocol/api";
import { readWorkspaceFile, readWorkspaceProjectIndex, type WorkspaceProjectIndexFile } from "../protocol/workspace";
import { normalizeWorkspaceRoot, workspacePathWithin, workspaceRootsEqual } from "../lib/workspace-path";
import { editorModelUri, syncWorkspaceTypeScriptModels } from "./monacoLanguageServices";
import { isDependencyIndexPath, isTypeScriptSourcePath, WorkspaceModelIndex } from "./workspaceModelIndex";
import { editorPathComparisonKey } from "../stores/shared-helpers";

export interface WorkspaceIndexStatus {
  phase: "idle" | "loading" | "ready" | "partial" | "error";
  sourceCount: number;
  issues: Array<{ path: string; message: string }>;
}
const idleStatus: WorkspaceIndexStatus = { phase: "idle", sourceCount: 0, issues: [] };
const affectsDependencies = (path: string) => isDependencyIndexPath(path)
  || /(?:^|[/\\])(?:package|tsconfig(?:\.[^/\\]+)?|jsconfig(?:\.[^/\\]+)?)\.json$/i.test(path);
type WorkspaceFileChange = ReturnType<typeof useAppStore.getState>["fileChanges"][number];
type PendingFileChange = { latest: WorkspaceFileChange; deletion?: WorkspaceFileChange };

// The editor and Problems share models, requests, and the same published status.
// A file notification queues the next snapshot; it never cancels the one being read.
class WorkspaceIndexSession {
  readonly index: WorkspaceModelIndex;
  private readonly controller = new AbortController();
  private readonly listeners = new Set<(status: WorkspaceIndexStatus) => void>();
  private readonly unsubscribe: () => void;
  private status = idleStatus;
  private observedSequence = 0;
  private pendingChanges = new Map<string, PendingFileChange>();
  private loaded = false;
  private dependenciesDirty = true;
  private running = false;
  private pending = false;
  private timer: number | undefined;

  constructor(monaco: typeof Monaco, readonly workspaceRoot: string) {
    this.index = new WorkspaceModelIndex(monaco, workspaceRoot);
    this.unsubscribe = useAppStore.subscribe((state, previous) => {
      if (state.fileChanges !== previous.fileChanges) this.observeChanges();
    });
    this.observeChanges();
    this.request();
  }

  subscribe(listener: (status: WorkspaceIndexStatus) => void): () => void {
    this.listeners.add(listener);
    listener(this.status);
    return () => { this.listeners.delete(listener); };
  }

  private publish(status: WorkspaceIndexStatus): void {
    this.status = status;
    this.listeners.forEach((listener) => listener(status));
  }

  private changesSince(sequence: number) {
    return useAppStore.getState().fileChanges.filter((change) => change.sequence > sequence
      && workspaceRootsEqual(change.workspaceRoot, this.workspaceRoot));
  }

  private observeChanges(): void {
    const changes = this.changesSince(this.observedSequence);
    if (!changes.length) return;
    this.observedSequence = changes.at(-1)!.sequence;
    for (const change of changes) {
      const key = editorPathComparisonKey(change.path, this.workspaceRoot);
      const deletion = /^(?:deleted|delete|removed|remove)$/i.test(change.event) ? change : this.pendingChanges.get(key)?.deletion;
      this.pendingChanges.set(key, { latest: change, deletion });
    }
    this.dependenciesDirty ||= changes.some((change) => affectsDependencies(change.path));
    this.request();
  }

  refresh(): void { this.dependenciesDirty = true; this.request(); }

  private request(): void {
    this.pending = true;
    if (this.running) return;
    window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => { this.timer = undefined; void this.load(); }, this.loaded ? 180 : 0);
  }

  private async load(): Promise<void> {
    this.running = true;
    this.pending = false;
    const changes = [...this.pendingChanges.values()].flatMap(({ latest, deletion }) =>
      deletion && deletion !== latest ? [deletion, latest] : [latest]).sort((left, right) => left.sequence - right.sequence);
    this.pendingChanges.clear();
    const includeDependencies = this.dependenciesDirty;
    this.dependenciesDirty = false;
    const signal = this.controller.signal;
    this.publish({ ...this.status, phase: "loading", issues: [] });
    try {
      this.index.noteFileChanges(changes);
      const snapshot = await readWorkspaceProjectIndex(this.workspaceRoot, includeDependencies, signal);
      if (signal.aborted) return;
      const sourceIssues = await this.index.applySnapshot(snapshot, includeDependencies, signal);
      if (signal.aborted) return;
      const workers = await syncWorkspaceTypeScriptModels(this.index.resources());
      if (signal.aborted) return;
      const attempted = new Set<string>();
      while (!signal.aborted) {
        const requested = (await workers.configurationRequests()).filter((uri) => !attempted.has(uri));
        if (signal.aborted) return;
        if (!requested.length) break;
        const additions: Array<{ filePath: string; file: WorkspaceProjectIndexFile }> = [];
        for (const uri of requested) {
          attempted.add(uri);
          const path = this.index.configurationPath(uri);
          if (!workspacePathWithin(path, this.workspaceRoot)) continue;
          try {
            const file = await readWorkspaceFile(path, this.workspaceRoot, signal);
            additions.push({ filePath: uri, file: { ...file, content_hash: file.content_hash!, kind: "config" } });
          } catch (error) {
            if (error instanceof ApiError && error.status === 404) continue;
            throw error;
          }
        }
        if (signal.aborted) return;
        this.index.addConfigurations(additions.map((entry) => entry.file));
        if (additions.length) await workers.addConfigurationFiles(additions.map((entry) => ({ filePath: entry.filePath, content: entry.file.content })));
      }
      if (signal.aborted) return;
      const configurationIssues = await workers.configurationDiagnostics();
      if (signal.aborted) return;
      const issues = [...snapshot.issues.map((issue) => ({ path: issue.path, message: issue.message })), ...sourceIssues,
        ...configurationIssues.map((issue) => ({ path: this.index.configurationPath(issue.path), message: issue.message }))];
      this.loaded = true;
      this.publish({ phase: issues.length ? "partial" : "ready", sourceCount: this.index.sourceCount(), issues });
    } catch (error) {
      if (!signal.aborted) {
        this.dependenciesDirty ||= includeDependencies;
        this.publish({ phase: "error", sourceCount: this.index.sourceCount(), issues: [{ path: this.workspaceRoot,
          message: error instanceof Error ? error.message : String(error) }] });
      }
    } finally {
      this.running = false;
      if (!signal.aborted && this.pending) this.request();
    }
  }

  dispose(): void {
    this.controller.abort();
    window.clearTimeout(this.timer);
    this.unsubscribe();
    this.listeners.clear();
    this.index.dispose();
  }
}

const sharedIndexes = new Map<string, { session: WorkspaceIndexSession; references: number }>();

export function useWorkspaceModelIndex(workspaceRoot: string) {
  const registry = useRef<WorkspaceIndexSession | null>(null);
  const releaseRegistry = useRef<(() => void) | null>(null);
  const [status, setStatus] = useState<WorkspaceIndexStatus>(idleStatus);

  useLayoutEffect(() => {
    setStatus(idleStatus);
    return () => { releaseRegistry.current?.(); releaseRegistry.current = null; registry.current = null; };
  }, [workspaceRoot]);

  const initialize = useCallback((monaco: typeof Monaco) => {
    if (registry.current && workspaceRootsEqual(registry.current.workspaceRoot, workspaceRoot)) return;
    releaseRegistry.current?.();
    const key = normalizeWorkspaceRoot(workspaceRoot);
    let shared = sharedIndexes.get(key);
    if (!shared) { shared = { session: new WorkspaceIndexSession(monaco, workspaceRoot), references: 0 }; sharedIndexes.set(key, shared); }
    shared.references += 1;
    registry.current = shared.session;
    const owner = shared;
    const unsubscribe = owner.session.subscribe(setStatus);
    releaseRegistry.current = () => {
      unsubscribe();
      owner.references -= 1;
      if (owner.references === 0) { owner.session.dispose(); sharedIndexes.delete(key); }
    };
  }, [workspaceRoot]);

  const refresh = useCallback(() => registry.current?.refresh(), []);
  const ownsModel = useCallback((path: string) => registry.current?.index.ownsModel(path) ?? false, []);
  const sourceFiles = useCallback(() => registry.current?.index.sourceFiles() ?? [], []);
  const retainsModel = useCallback((path: string) => {
    const url = new URL(editorModelUri(path, workspaceRoot));
    const modelPath = decodeURIComponent(url.pathname).replace(/^\/(?=[A-Za-z]:\/)/, "");
    const absolutePath = url.hostname ? "//" + url.hostname + modelPath : modelPath;
    return Boolean(workspaceRoot) && isTypeScriptSourcePath(path) && !isDependencyIndexPath(path) && workspacePathWithin(absolutePath, workspaceRoot);
  }, [workspaceRoot]);
  return { initialize, refresh, status, ownsModel, retainsModel, sourceFiles };
}
