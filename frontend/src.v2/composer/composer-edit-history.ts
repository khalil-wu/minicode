export type EditHistoryDirection = "undo" | "redo";
type ComposerHistory = { hasText(): boolean; undo(): boolean; redo(): boolean };
const histories = new Map<HTMLElement, ComposerHistory>();

export const registerComposerEditHistory = (element: HTMLElement, history: ComposerHistory) => {
  histories.set(element, history);
  return () => { histories.delete(element); };
};

/** false means a known composer exhausted its own text history. */
export const tryFocusedComposerHistory = (direction: EditHistoryDirection): boolean | null => {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return null;
  for (const [element, history] of histories) {
    if (element.isConnected && (element === active || element.contains(active))) {
      return direction === "undo" && !history.hasText() ? false : history[direction]();
    }
  }
  return null;
};

export const focusedEditContext = (): "composer" | "other" | null => {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || !(active.isContentEditable
    || active.closest('[contenteditable]:not([contenteditable="false"])') || active.matches("input,textarea,select"))) return null;
  return active.closest("[data-composer-input]") ? "composer" : "other";
};
