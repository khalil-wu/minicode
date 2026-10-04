import type { MessageContextRef } from "../stores/types";
import { useAppStore } from "../stores";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { pushToast } from "../overlays/ToastContainer";
import { openWebTarget } from "./openWebTarget";
import { returnToBrowserPage } from "./openWebInBrowser";

export function contextReferenceLabel(ref: MessageContextRef): string {
  if (ref.kind !== "file" || !ref.range) return ref.name;
  const { startLineNumber, endLineNumber, endColumn } = ref.range;
  const last = endLineNumber > startLineNumber && endColumn === 1 ? endLineNumber - 1 : endLineNumber;
  return `${ref.name}:${startLineNumber}${last === startLineNumber ? "" : `–${last}`}`;
}

export function openContextReference(ref: MessageContextRef, conversationId?: string): void {
  const state = useAppStore.getState();
  if (ref.kind === "browser_annotation") {
    if (ref.targetId) returnToBrowserPage({ conversationId: conversationId || state.conversationId || "", targetId: ref.targetId, url: ref.url });
    else openWebTarget(ref.url);
  } else if (ref.kind === "url") {
    openWebTarget(ref.path);
  } else if (ref.kind === "file") {
    if (ref.workspaceRoot && !workspaceRootsEqual(ref.workspaceRoot, state.workingDirectory)) {
      pushToast(`这段引用属于 ${ref.workspaceRoot}，切回该工作区后可定位原代码。`, "info", 5000);
      return;
    }
    const anchor = ref.path.match(/#L?(\d+)(?:-L?(\d+))?$/i);
    state.openEditorFile(anchor ? ref.path.slice(0, anchor.index) : ref.path, ref.name, {
      exact: true,
      line: ref.range?.startLineNumber ?? (anchor ? Number(anchor[1]) : undefined),
      column: ref.range?.startColumn,
      endLine: ref.range?.endLineNumber ?? (anchor?.[2] ? Number(anchor[2]) : undefined),
      endColumn: ref.range?.endColumn,
    });
  }
}
