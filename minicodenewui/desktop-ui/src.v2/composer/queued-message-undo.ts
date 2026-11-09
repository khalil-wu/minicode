import { useAppStore } from "../stores";
import { pushToast } from "../overlays/ToastContainer";
import { focusedEditContext, tryFocusedComposerHistory, type EditHistoryDirection } from "./composer-edit-history";
import { translateApp } from "../i18n";

type QueueUndoAction = {
  ownerId: string;
  expiresAfterMs: number;
  undo(): Promise<void>;
  redo(): Promise<void>;
  onFinalize?(): void;
};
type Entry = QueueUndoAction & { side: EditHistoryDirection | "pending"; expiresAt: number;
  timer?: ReturnType<typeof setTimeout>; live: boolean };
const entries: Entry[] = [];
const listeners = new Set<() => void>();
let snapshot = { undo: false, redo: false };
let nextOrder = 0;
let transition: Promise<void> = Promise.resolve();

function publish() {
  const undo = entries.some(entry => entry.side === "undo");
  const redo = entries.some(entry => entry.side === "redo");
  if (undo === snapshot.undo && redo === snapshot.redo) return;
  snapshot = { undo, redo };
  listeners.forEach(listener => listener());
}

export const subscribeQueuedMessageHistory = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export const queuedMessageHistorySnapshot = () => snapshot;

function remove(entry: Entry) {
  if (!entry.live) return;
  entry.live = false;
  clearTimeout(entry.timer);
  const index = entries.indexOf(entry);
  if (index >= 0) entries.splice(index, 1);
  entry.onFinalize?.();
  publish();
}

function expire(entry: Entry) {
  clearTimeout(entry.timer);
  entry.expiresAt = Date.now() + entry.expiresAfterMs;
  entry.timer = setTimeout(() => remove(entry), entry.expiresAfterMs);
}

/** Queue actions alone participate; this does not capture other app changes. */
export const recordQueuedMessageUndo = (action: QueueUndoAction) => {
  nextOrder++;
  for (const entry of [...entries]) if (entry.side === "redo") remove(entry);
  const entry: Entry = { ...action, side: "undo", expiresAt: Date.now(), live: true };
  entries.push(entry);
  expire(entry);
  publish();
  return { invalidate: () => remove(entry) };
};

export const dispatchQueuedMessageHistory = (direction: EditHistoryDirection): boolean => {
  for (const entry of [...entries]) if (entry.expiresAt <= Date.now()) remove(entry);
  const entry = [...entries].reverse().find(candidate => candidate.side === direction);
  if (!entry) return false;
  const order = nextOrder;
  entry.side = "pending";
  clearTimeout(entry.timer);
  publish();
  transition = transition.then(async () => {
    if (!entry.live) return;
    try {
      await entry[direction]();
      if (!entry.live) return;
      if (direction === "undo" && order !== nextOrder) { remove(entry); return; }
      entries.splice(entries.indexOf(entry), 1);
      entries.push(entry);
      entry.side = direction === "undo" ? "redo" : "undo";
      expire(entry);
      publish();
      const state = useAppStore.getState();
      if (state.conversationId !== entry.ownerId && !state.sideChats[entry.ownerId]) state.requestConversationSwitch(entry.ownerId);
      if (direction === "undo") pushToast(translateApp("排队消息已恢复"), "success");
    } catch (error) {
      remove(entry);
      pushToast(error instanceof Error ? error.message : String(error), "error");
    }
  });
  return true;
};

export const queueHistoryShortcutIsStandard = (event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">,
  direction: EditHistoryDirection) => !event.altKey && (event.ctrlKey || event.metaKey)
  && (direction === "undo" ? event.key.toLowerCase() === "z" && !event.shiftKey
    : event.key.toLowerCase() === "z" && event.shiftKey || event.ctrlKey && event.key.toLowerCase() === "y" && !event.shiftKey);

export const routeQueuedMessageHistory = (direction: EditHistoryDirection, standardChord = true): "handled" | "native" | "none" => {
  if (standardChord) {
    const textHistory = tryFocusedComposerHistory(direction);
    if (textHistory === true) return "handled";
    const context = focusedEditContext();
    if (context === "other" || context === "composer" && textHistory === null) return "native";
  }
  return dispatchQueuedMessageHistory(direction) ? "handled" : "none";
};

export const waitForQueuedMessageHistory = () => transition;
export const resetQueuedMessageHistoryForTests = () => { for (const entry of [...entries]) remove(entry); nextOrder++; };
