import { useEffect, useRef } from "react";

const isVisibleShortcutSurface = (surface: HTMLElement) => {
  if (surface.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
  for (let element: HTMLElement | null = surface; element; element = element.parentElement) {
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
  }
  return true;
};

export const useApprovalShortcuts = ({ responding, onDeny, onAllow }: {
  responding: boolean;
  onDeny: () => void;
  onAllow?: () => void;
}) => {
  const actionsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" && event.key !== "Enter") return;
      if (responding || event.defaultPrevented || event.isComposing || event.repeat || event.altKey || event.ctrlKey || event.metaKey) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('input, textarea, select, [contenteditable]:not([contenteditable="false" i]), [role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]')) return;
      if (event.key === "Enter" && target?.closest("button, a")) return;
      if (Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"], dialog[open]'))
        .some(isVisibleShortcutSurface)) return;

      // Hidden chats stay mounted to retain drafts and history. Only the
      // visible approval in the keyboard event's card/pane owns this shortcut.
      const visibleActions = Array.from(document.querySelectorAll<HTMLElement>(".inline-approval-actions"))
        .filter(isVisibleShortcutSurface);
      const scope = target?.closest(".inline-approval-bar") ?? target?.closest(".chat-pane");
      const activeActions = scope ? visibleActions.find(actions => scope.contains(actions)) : visibleActions[0];
      if (activeActions !== actionsRef.current) return;
      if (event.key === "Enter" && !onAllow) return;
      event.preventDefault();
      (event.key === "Escape" ? onDeny : onAllow!)();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [responding, onDeny, onAllow]);
  return actionsRef;
};
