import { useEffect, useRef } from "react";
import { workspaceRootsEqual } from "../lib/workspace-path";
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

const isTopLevelModalOpen = (state: ReturnType<typeof useAppStore.getState>): boolean =>
  state.commandPaletteOpen || state.quickOpenVisible || state.settingsOpen
  || state.shortcutsHelpOpen || state.liveArtifactsOpen
  || (state.agentEditorOpen && capabilityFeatureEnabled(state.runtimeCapabilities, "agent_editor", true));

export const useKeyboardShortcuts = () => {
  const sidebarWidthRef = useRef(280);
  useEffect(() => {
    const onFileSearch = (event: KeyboardEvent) => {
      const state = useAppStore.getState();
      if (state.settingsOpen && state.settingsTab === "shortcuts" && document.querySelector('[data-shortcut-recording="true"]')) return;
      if (event.isComposing || event.keyCode === 229 || !matchesShortcut(event, state.shortcutBindings.globalSearch)) return;
      event.preventDefault();
      event.stopPropagation();
      if (capabilityFeatureEnabled(state.runtimeCapabilities, "global_search", true)) state.toggleQuickOpen();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
      const mod = e.metaKey || e.ctrlKey;
      const s = useAppStore.getState();
      const createConversationInCurrentMode = () => {
        s.createConversation({ appMode: s.appMode, bindWorkspace: Boolean(s.workingDirectory) });
      };
      const match = (action: ShortcutActionId) => matchesShortcut(e, s.shortcutBindings[action]);
      const modalOpen = isTopLevelModalOpen(s);
      const routeToComposer = (action: () => void) => {
        window.dispatchEvent(new Event("composer:focus"));
        if (s.skillsMarketplaceOpen) useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
        const chat = s.panelSlots.find((slot) => slot.kind === "chat");
        if (s.appMode === "code" && chat) s.focusPanel(chat.id);
        requestAnimationFrame(() => {
          const current = useAppStore.getState();
          if (current.conversationId !== s.conversationId
            || !workspaceRootsEqual(current.workingDirectory, s.workingDirectory)
            || isTopLevelModalOpen(current)) return;
          action();
        });
      };

      // The topmost dialog owns its keyboard context. Individual dialogs
      // handle Enter/Escape and navigation locally. Modal-routing shortcuts
      // remain global so users can close or switch top-level surfaces while a
      // search field is focused; workspace mutations stay blocked.
      if ((modalOpen || isModalTarget(e.target)) && e.key !== "Escape") {
        if (match("commandPalette")) { e.preventDefault(); s.toggleCommandPalette(); return; }
        if (match("settings")) { e.preventDefault(); s.toggleSettings(); return; }
        if (match("shortcutHelp")) { e.preventDefault(); s.toggleShortcutsHelp(); return; }
        if (match("globalSearch")) {
          e.preventDefault();
          if (capabilityFeatureEnabled(s.runtimeCapabilities, "global_search", true)) s.toggleQuickOpen();
          return;
        }
        if (match("openGeneralSettings")) { e.preventDefault(); openSettings("general"); return; }
        return;
      }

      // Alt+1/2/3 for mode switching (no Ctrl required)
      if (e.altKey && !mod) {
        const mode = e.key === "1" ? "chat" : e.key === "2" ? "code" : e.key === "3" ? "cowork" : null;
        if (mode) {
          e.preventDefault();
          useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
          s.setAppMode(mode);
          return;
        }
      }

      if (e.key === "Escape") {
        if (modalOpen || isModalTarget(e.target)) return;
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
        routeToComposer(() => window.dispatchEvent(new Event("composer:history-search")));
        return;
      }
      if (match("clearComposer")) {
        e.preventDefault();
        s.setDraft("");
        routeToComposer(() => document.querySelector<HTMLTextAreaElement>("[data-composer-input]")?.focus());
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
        if (s.rightPanelOpen && s.rightStackTab === "diff") s.toggleRightPanel();
        else { s.setAppMode("code"); s.setRightStackTab("diff"); }
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
        routeToComposer(() => document.dispatchEvent(new CustomEvent("open-permission-menu")));
        return;
      }
      if (match("modelMenu")) {
        e.preventDefault();
        routeToComposer(() => document.dispatchEvent(new CustomEvent("open-model-menu")));
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
        if (!s.rightPanelExpanded && !s.panelSlots.some((slot) => slot.maximized) && !s.dockCollapsed && s.activeBottomTab === "terminal") s.closeBottomDock();
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
      if (match("saveAllFiles")) { e.preventDefault(); window.dispatchEvent(new Event("editor:save-all")); return; }
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
    window.addEventListener("keydown", onFileSearch, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onFileSearch, true);
      window.removeEventListener("keydown", onKey);
    };
  }, []);
};
