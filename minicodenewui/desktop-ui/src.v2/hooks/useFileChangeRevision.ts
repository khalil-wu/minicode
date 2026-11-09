import { useEffect, useState } from "react";
import { useAppStore } from "../stores";
import { workspaceFilePathComparisonKey, workspaceRootsEqual } from "../lib/workspace-path";

/** Coalesce workspace edits without invalidating other workspaces' open panels. */
export function useFileChangeRevision(workspaceRoot: string, filePath?: string): number {
  const target = filePath === undefined ? "" : workspaceFilePathComparisonKey(filePath, workspaceRoot);
  const scope = JSON.stringify([workspaceRoot, target]);
  const latest = useAppStore(state => {
    for (let index = state.fileChanges.length - 1; index >= 0; index -= 1) {
      const change = state.fileChanges[index];
      if (workspaceRootsEqual(change.workspaceRoot, workspaceRoot)
        && (!target || workspaceFilePathComparisonKey(change.path, change.workspaceRoot) === target)) return change.sequence;
    }
    return 0;
  });
  const [revision, setRevision] = useState({ scope, sequence: latest });
  useEffect(() => {
    if (revision.scope !== scope) { setRevision({ scope, sequence: latest }); return; }
    // Rolling the bounded change log past this file is not another edit.
    if (latest <= revision.sequence) return;
    const timer = setTimeout(() => setRevision({ scope, sequence: latest }), 300);
    return () => clearTimeout(timer);
  }, [scope, latest, revision]);
  return revision.scope === scope ? revision.sequence : latest;
}
