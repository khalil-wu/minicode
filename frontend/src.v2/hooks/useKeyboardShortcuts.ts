import { useEffect, useRef } from "react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { openSettings } from "../lib/settings-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";

let lastZoomToastAt = 0;

const announceZoom = (scale: number) => {
  const now = Date.now();
  if (now - lastZoomToastAt < 350) return;
  lastZoomToastAt = now;
  pushToast(`Zoom ${Math.round(scale * 100)}%`, "info", 1200);
};

const announceViewMode = (mode: string) => {
  pushToast(`View mode: ${mode.charAt(0).toUpperCase()}${mode.slice(1)}`, "info", 1200);
};

const isInteractiveTarget = (target: EventTarget | null): boolean => {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  return (
    tag === "input" ||
    tag === "textarea" ||
    tag === "select" ||
    target.isContentEditable ||
    Boolean(target.closest("[role='dialog'], .modal-content, .overlay-backdrop"))
  );
};

const isModalTarget = (target: EventTarget | null): boolean =>
  target instanceof HTMLElement
  && Boolean(target.closest("[role='dialog'], .modal-content, .overlay-backdrop"));

export const useKeyboardShortcuts = () => {
  const sidebarWidthRef = useRef(280);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const s = useAppStore.getState();
      const createConversationInCurrentMode = () => {
        s.createConversation({ appMode: s.appMode, bindWorkspace: Boolean(s.workingDirectory) });
      };

      // The topmost dialog owns its keyboard context. Individual dialogs
      // handle Enter/Escape and navigation locally; global application
      // shortcuts must not mutate conversations or panels through them.
      if (isModalTarget(e.target) && e.key !== "Escape") return;

      // Alt+1/2/3 for mode switching (no Ctrl required)
      if (e.altKey && !mod) {
        if (e.key === "1") { e.preventDefault(); s.setAppMode("chat"); return; }
        if (e.key === "2") { e.preventDefault(); s.setAppMode("code"); return; }
        if (e.key === "3") { e.preventDefault(); s.setAppMode("cowork"); return; }
      }

      if (e.key === "Escape") {
        if (!isInteractiveTarget(e.target)) {
          s.interrupt();
          sendClientCommand({
            type: "interrupt",
            ...(s.conversationId ? { conversation_id: s.conversationId } : {}),
          });
        }
        return;
      }

      if (!mod) return;

      // Ctrl/Cmd+1..9 jumps directly to the Nth non-archived conversation.
      if (!e.shiftKey && !e.altKey && e.key >= "1" && e.key <= "9") {
        e.preventDefault();
        const convs = s.conversations.filter((c) => !c.archived);
        const target = convs[Number(e.key) - 1];
        if (target && target.id !== s.conversationId) {
          s.requestConversationSwitch(target.id);
        }
        return;
      }

      switch (e.key) {
        case "r":
        case "R":
          if (!e.shiftKey && !e.altKey) {
            e.preventDefault();
            window.dispatchEvent(new Event("composer:history-search"));
          }
          break;
        case "l":
          if (e.shiftKey) {
            e.preventDefault();
            createConversationInCurrentMode();
          } else {
            e.preventDefault();
            s.setDraft("");
            document.querySelector<HTMLTextAreaElement>("textarea")?.focus();
          }
          break;
        case "o":
        case "O":
          e.preventDefault();
          {
            const next = s.viewMode === "normal" ? "verbose" : s.viewMode === "verbose" ? "summary" : "normal";
            s.setViewMode(next);
            announceViewMode(next);
          }
          break;
        case "d":
        case "D":
          if (e.shiftKey) {
            e.preventDefault();
            // Toggle diff pane
            const hasDiff = s.panelSlots.some(p => p.kind === "diff");
            if (hasDiff) {
              const diffPanels = s.panelSlots.filter(p => p.kind === "diff");
              diffPanels.forEach(p => s.removePanel(p.id));
            } else {
              s.addPanel({ id: "diff-" + Date.now(), kind: "diff" });
            }
          }
          break;
        case "p":
        case "P":
          if (e.shiftKey) {
            e.preventDefault();
            s.setAppMode("code");
            s.setRightStackTab("preview");
          } else {
            e.preventDefault();
            if (capabilityFeatureEnabled(s.runtimeCapabilities, "global_search", true)) {
              s.toggleQuickOpen();
            }
          }
          break;
        case "m":
        case "M":
          if (e.shiftKey) {
            e.preventDefault();
            document.dispatchEvent(new CustomEvent("open-permission-menu"));
          }
          break;
        case "i":
        case "I":
          if (e.shiftKey) {
            e.preventDefault();
            document.dispatchEvent(new CustomEvent("open-model-menu"));
          }
          break;
        case "e":
        case "E":
          if (e.shiftKey) {
            e.preventDefault();
            openSettings("general");
          }
          break;
        case "k":
        case "K":
          if (!e.shiftKey) {
            e.preventDefault();
            s.toggleCommandPalette();
          }
          break;
        case "n":
          e.preventDefault();
          createConversationInCurrentMode();
          break;
        case ",":
          e.preventDefault();
          s.toggleSettings();
          break;
        case "/":
          e.preventDefault();
          s.toggleShortcutsHelp();
          break;
        case "=":
        case "+":
          e.preventDefault();
          {
            const next = s.textScale + 0.04;
            s.setTextScale(next);
            announceZoom(useAppStore.getState().textScale);
          }
          break;
        case "-":
          e.preventDefault();
          {
            const next = s.textScale - 0.04;
            s.setTextScale(next);
            announceZoom(useAppStore.getState().textScale);
          }
          break;
        case "0":
          e.preventDefault();
          s.setTextScale(1.0);
          announceZoom(1.0);
          break;
        case "j":
          e.preventDefault();
          s.setAppMode("code");
          s.setRightStackTab("terminal");
          break;
        case "\\":
          e.preventDefault();
          {
            const focused = s.panelSlots.find((slot) => slot.focused);
            if (focused && s.panelSlots.length > 1) s.removePanel(focused.id);
          }
          break;
        case "b":
          if (!e.shiftKey) {
            e.preventDefault();
            if (s.leftSidebarWidth > 0) {
              sidebarWidthRef.current = s.leftSidebarWidth;
              s.setLeftSidebarWidth(0);
            } else {
              s.setLeftSidebarWidth(sidebarWidthRef.current);
            }
          } else {
            e.preventDefault();
            s.setAppMode("code");
            s.setRightStackTab("preview");
          }
          break;
        case "v":
          if (e.shiftKey) {
            e.preventDefault();
            const next = s.viewMode === "normal" ? "verbose" : s.viewMode === "verbose" ? "summary" : "normal";
            s.setViewMode(next);
            announceViewMode(next);
          }
          break;
        case ";":
          e.preventDefault();
          s.toggleSideChat();
          break;
        case "s":
          if (!e.shiftKey) {
            e.preventDefault();
            window.dispatchEvent(new Event("editor:save"));
          }
          break;
        case "w":
          if (!e.shiftKey) {
            e.preventDefault();
            window.dispatchEvent(new Event("editor:close-tab"));
          }
          break;
        case "Tab":
          e.preventDefault();
          {
            const convs = s.conversations.filter((c) => !c.archived);
            if (convs.length < 2) break;
            const idx = convs.findIndex((c) => c.id === s.conversationId);
            const next = e.shiftKey
              ? convs[(idx - 1 + convs.length) % convs.length]
              : convs[(idx + 1) % convs.length];
            if (next) {
              s.requestConversationSwitch(next.id);
            }
          }
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
};
