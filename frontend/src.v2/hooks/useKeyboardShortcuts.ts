import { useEffect, useRef } from "react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { openSettings } from "../lib/settings-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { matchesShortcut, matchesShiftedShortcutVariant, type ShortcutActionId } from "../lib/keyboard-shortcuts";
import { buildInterruptCommand } from "../lib/interrupt-command";

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

const isModalTarget = (target: EventTarget | null): boolean =>
  target instanceof HTMLElement
  && Boolean(target.closest("[role='dialog'], .modal-content, .overlay-backdrop, .settings-workspace"));

export const useKeyboardShortcuts = () => {
  const sidebarWidthRef = useRef(280);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const s = useAppStore.getState();
      const createConversationInCurrentMode = () => {
        s.createConversation({ appMode: s.appMode, bindWorkspace: Boolean(s.workingDirectory) });
      };
      const match = (action: ShortcutActionId) => matchesShortcut(e, s.shortcutBindings[action]);

      // The topmost dialog owns its keyboard context. Individual dialogs
      // handle Enter/Escape and navigation locally. Modal-routing shortcuts
      // remain global so users can close or switch top-level surfaces while a
      // search field is focused; workspace mutations stay blocked.
      if (isModalTarget(e.target) && e.key !== "Escape") {
        if (match("commandPalette")) { e.preventDefault(); s.toggleCommandPalette(); return; }
        if (match("settings")) { e.preventDefault(); s.toggleSettings(); return; }
        if (match("shortcutHelp")) { e.preventDefault(); s.toggleShortcutsHelp(); return; }
        if (match("openGeneralSettings")) { e.preventDefault(); openSettings("general"); return; }
        return;
      }

      // Alt+1/2/3 for mode switching (no Ctrl required)
      if (e.altKey && !mod) {
        if (e.key === "1") { e.preventDefault(); s.setAppMode("chat"); return; }
        if (e.key === "2") { e.preventDefault(); s.setAppMode("code"); return; }
        if (e.key === "3") { e.preventDefault(); s.setAppMode("cowork"); return; }
      }

      if (e.key === "Escape") {
        if (isModalTarget(e.target)) return;
        // Only an actually-running turn can be interrupted. Without this gate a
        // stray Escape while idle ran finishStreaming with no target message,
        // which cancels the plan and blocks every in-progress todo.
        if (!s.isStreaming) return;
        e.preventDefault();
        const command = buildInterruptCommand(s);
        sendClientCommand(command);
        return;
      }

      if (!mod && !e.altKey && !e.key.startsWith("F")) return;

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

      if (match("promptHistory")) {
        e.preventDefault();
        window.dispatchEvent(new Event("composer:history-search"));
        return;
      }
      if (match("clearComposer")) {
        e.preventDefault();
        s.setDraft("");
        document.querySelector<HTMLTextAreaElement>("textarea")?.focus();
        return;
      }
      if (match("processDetail")) {
        e.preventDefault();
        const next = s.viewMode === "normal" ? "verbose" : s.viewMode === "verbose" ? "summary" : "normal";
        s.setViewMode(next);
        announceViewMode(next);
        return;
      }
      if (match("toggleDiff")) {
        e.preventDefault();
        const diffPanels = s.panelSlots.filter((panel) => panel.kind === "diff");
        if (diffPanels.length) diffPanels.forEach((panel) => s.removePanel(panel.id));
        else s.addPanel({ id: `diff-${Date.now()}`, kind: "diff" });
        return;
      }
      if (match("openPreview")) {
        e.preventDefault();
        s.setAppMode("code");
        s.setRightStackTab("preview");
        return;
      }
      if (match("globalSearch")) {
        e.preventDefault();
        if (capabilityFeatureEnabled(s.runtimeCapabilities, "global_search", true)) s.toggleQuickOpen();
        return;
      }
      if (match("permissionMenu")) {
        e.preventDefault();
        document.dispatchEvent(new CustomEvent("open-permission-menu"));
        return;
      }
      if (match("modelMenu")) {
        e.preventDefault();
        document.dispatchEvent(new CustomEvent("open-model-menu"));
        return;
      }
      if (match("openGeneralSettings")) {
        e.preventDefault();
        openSettings("general");
        return;
      }
      if (match("commandPalette")) { e.preventDefault(); s.toggleCommandPalette(); return; }
      if (match("newConversation")) { e.preventDefault(); createConversationInCurrentMode(); return; }
      if (match("settings")) { e.preventDefault(); s.toggleSettings(); return; }
      if (match("shortcutHelp")) { e.preventDefault(); s.toggleShortcutsHelp(); return; }
      if (match("zoomIn")) {
        e.preventDefault();
        s.setTextScale(s.textScale + 0.04);
        announceZoom(useAppStore.getState().textScale);
        return;
      }
      if (match("zoomOut")) {
        e.preventDefault();
        s.setTextScale(s.textScale - 0.04);
        announceZoom(useAppStore.getState().textScale);
        return;
      }
      if (match("zoomReset")) { e.preventDefault(); s.setTextScale(1); announceZoom(1); return; }
      if (match("terminal")) {
        e.preventDefault();
        s.setAppMode("code");
        if (!s.dockCollapsed && s.activeBottomTab === "terminal") s.closeBottomDock();
        else s.openBottomTab("terminal");
        return;
      }
      if (match("closePanel")) {
        e.preventDefault();
        const focused = s.panelSlots.find((slot) => slot.focused);
        if (focused && s.panelSlots.length > 1) s.removePanel(focused.id);
        return;
      }
      if (match("leftSidebar")) {
        e.preventDefault();
        if (s.leftSidebarWidth > 0) {
          sidebarWidthRef.current = s.leftSidebarWidth;
          s.setLeftSidebarWidth(0);
        } else {
          s.setLeftSidebarWidth(sidebarWidthRef.current);
        }
        return;
      }
      if (match("sideChat")) { e.preventDefault(); s.toggleSideChat(); return; }
      if (match("saveFile")) { e.preventDefault(); window.dispatchEvent(new Event("editor:save")); return; }
      if (match("closeEditor")) { e.preventDefault(); window.dispatchEvent(new Event("editor:close-tab")); return; }
      const reverseConversation = matchesShiftedShortcutVariant(e, s.shortcutBindings.nextConversation);
      if (match("nextConversation") || reverseConversation) {
        e.preventDefault();
        const conversations = s.conversations.filter((conversation) => !conversation.archived);
        if (conversations.length < 2) return;
        const index = conversations.findIndex((conversation) => conversation.id === s.conversationId);
        const next = reverseConversation
          ? conversations[(index - 1 + conversations.length) % conversations.length]
          : conversations[(index + 1) % conversations.length];
        if (next) s.requestConversationSwitch(next.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
};
