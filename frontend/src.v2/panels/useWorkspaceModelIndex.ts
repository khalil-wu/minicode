import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { useAppStore } from "../stores";
import { ApiError } from "../protocol/api";
import { readWorkspaceFile, readWorkspaceProjectIndex, type WorkspaceProjectIndexFile } from "../protocol/workspace";
import { workspacePathWithin, workspaceRootsEqual } from "../lib/workspace-path";
import { editorModelUri, syncWorkspaceTypeScriptModels } from "./monacoLanguageServices";
import { isDependencyIndexPath, isTypeScriptSourcePath, WorkspaceModelIndex } from "./workspaceModelIndex";

export interface WorkspaceIndexStatus {
  phase: "idle" | "loading" | "ready" | "partial" | "error";
  sourceCount: number;
  issues: Array<{ path: string; message: string }>;
}
const idleStatus: WorkspaceIndexStatus = { phase: "idle", sourceCount: 0, issues: [] };

export function useWorkspaceModelIndex(workspaceRoot: string) {
  const registry = useRef<WorkspaceModelIndex | null>(null);
  const loadedDependencies = useRef(false);
  const lastSequence = useRef(0);
  const [epoch, setEpoch] = useState(0);
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const [status, setStatus] = useState<WorkspaceIndexStatus>(idleStatus);
  const fileChanges = useAppStore((state) => state.fileChanges);
  const latestSequence = fileChanges.filter((change) => workspaceRootsEqual(change.workspaceRoot, workspaceRoot)).at(-1)?.sequence ?? 0;

  useLayoutEffect(() => {
    loadedDependencies.current = false;
    lastSequence.current = 0;
    setStatus(idleStatus);
    return () => { registry.current?.dispose(); registry.current = null; };
  }, [workspaceRoot]);

  const initialize = useCallback((monaco: typeof Monaco) => {
    if (registry.current && workspaceRootsEqual(registry.current.workspaceRoot, workspaceRoot)) return;
    registry.current?.dispose();
    registry.current = new WorkspaceModelIndex(monaco, workspaceRoot);
    setEpoch((value) => value + 1);
  }, [workspaceRoot]);

  useEffect(() => {
    const owner = registry.current;
    if (!owner || !workspaceRoot) return;
    const controller = new AbortController();
    const changes = fileChanges.filter((change) => change.sequence > lastSequence.current && workspaceRootsEqual(change.workspaceRoot, workspaceRoot));
    owner.noteFileChanges(changes);
    const includeDependencies = !loadedDependencies.current || changes.some((change) =>
      isDependencyIndexPath(change.path) || /(?:^|[/\\])(?:package|tsconfig(?:\.[^/\\]+)?|jsconfig(?:\.[^/\\]+)?)\.json$/i.test(change.path));

    const load = async () => {
      setStatus((previous) => ({ ...previous, phase: "loading", issues: [] }));
      try {
        const snapshot = await readWorkspaceProjectIndex(workspaceRoot, includeDependencies, controller.signal);
        if (controller.signal.aborted) return;
        const sourceIssues = await owner.applySnapshot(snapshot, includeDependencies, controller.signal);
        if (controller.signal.aborted) return;
        const workers = await syncWorkspaceTypeScriptModels(owner.resources());
        if (controller.signal.aborted) return;
        const attempted = new Set<string>();
        while (!controller.signal.aborted) {
          const requested = (await workers.configurationRequests()).filter((uri) => !attempted.has(uri));
          if (controller.signal.aborted) return;
          if (!requested.length) break;
          const additions: Array<{ filePath: string; file: WorkspaceProjectIndexFile }> = [];
          for (const uri of requested) {
            attempted.add(uri);
            const path = owner.configurationPath(uri);
            if (!workspacePathWithin(path, workspaceRoot)) continue;
            try {
              const file = await readWorkspaceFile(path, workspaceRoot, controller.signal);
              additions.push({ filePath: uri, file: { ...file, content_hash: file.content_hash!, kind: "config" } });
            } catch (error) {
              if (error instanceof ApiError && error.status === 404) continue;
              throw error;
            }
          }
          if (controller.signal.aborted) return;
          owner.addConfigurations(additions.map((entry) => entry.file));
          if (additions.length) await workers.addConfigurationFiles(additions.map((entry) => ({ filePath: entry.filePath, content: entry.file.content })));
        }
        if (controller.signal.aborted || registry.current !== owner) return;
        const configurationIssues = await workers.configurationDiagnostics();
        if (controller.signal.aborted) return;
        const issues = [...snapshot.issues.map((issue) => ({ path: issue.path, message: issue.message })), ...sourceIssues, ...configurationIssues.map((issue) => ({ path: owner.configurationPath(issue.path), message: issue.message }))];
        loadedDependencies.current = true;
        lastSequence.current = latestSequence;
        setStatus({ phase: issues.length ? "partial" : "ready", sourceCount: owner.sourceCount(), issues });
      } catch (error) {
        if (!controller.signal.aborted && registry.current === owner) setStatus({ phase: "error", sourceCount: owner.sourceCount(), issues: [{ path: workspaceRoot, message: error instanceof Error ? error.message : String(error) }] });
      }
    };
    const timer = window.setTimeout(() => void load(), loadedDependencies.current ? 180 : 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
    // The sequence groups native file notifications; buffer edits stay in models.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceRoot, epoch, latestSequence, refreshEpoch]);

  const refresh = useCallback(() => { loadedDependencies.current = false; setRefreshEpoch((value) => value + 1); }, []);
  const ownsModel = useCallback((path: string) => registry.current?.ownsModel(path) ?? false, []);
  const retainsModel = useCallback((path: string) => {
    const url = new URL(editorModelUri(path, workspaceRoot));
    const modelPath = decodeURIComponent(url.pathname).replace(/^\/(?=[A-Za-z]:\/)/, "");
    const absolutePath = url.hostname ? `//${url.hostname}${modelPath}` : modelPath;
    return Boolean(workspaceRoot) && isTypeScriptSourcePath(path) && !isDependencyIndexPath(path) && workspacePathWithin(absolutePath, workspaceRoot);
  }, [workspaceRoot]);
  return { initialize, refresh, status, ownsModel, retainsModel };
}
