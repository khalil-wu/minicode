import { useEffect, useSyncExternalStore } from "react";
import type { GitDiffSource } from "../../stores/types";
import { workspaceRootsEqual } from "../../lib/workspace-path";

export interface ReviewSelection {
  source: GitDiffSource | "last-turn";
  baseBranch?: string;
  commitSha?: string;
}
const key = "last-explicit-review-diff-filter";
const owners = new Map<string, { cwd: string; selection: ReviewSelection }>();
const listeners = new Set<() => void>();
let revision = 0;
const notify = () => { revision++; listeners.forEach(listener => listener()); };
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

function save(owner: string, cwd: string, selection: ReviewSelection) {
  owners.delete(owner);
  owners.set(owner, { cwd, selection });
  if (owners.size > 20) owners.delete(owners.keys().next().value!);
  notify();
}

export function clearGitReviewSelection(owner: string) { owners.delete(owner); notify(); }

/** Source ThreadScope retains twenty owners; only the explicit source preference is global. */
export function useGitReviewSelection(owner: string, cwd: string) {
  useSyncExternalStore(subscribe, () => revision);
  const stored = owners.get(owner);
  const selection: ReviewSelection = stored && workspaceRootsEqual(stored.cwd, cwd) ? stored.selection
    : { source: stored?.selection.source ?? JSON.parse(localStorage.getItem(key) ?? '"branch"') };
  useEffect(() => {
    if (stored && !workspaceRootsEqual(stored.cwd, cwd)) save(owner, cwd, selection);
  }, [owner, cwd]);
  const update = (next: ReviewSelection | ((current: ReviewSelection) => ReviewSelection), explicitSource = false) => {
    const value = typeof next === "function" ? next(selection) : next;
    // Source sts keeps a selected commit in ThreadScope, never the global preference.
    if (explicitSource && value.source !== "commit") localStorage.setItem(key, JSON.stringify(value.source));
    save(owner, cwd, value);
  };
  return [selection, update] as const;
}
