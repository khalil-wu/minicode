import { useEffect } from "react";
import { toggleChangesPanel } from "../lib/changes-panel";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { openSettings } from "../lib/settings-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { isCodeShortcut, matchesShortcut, matchesShiftedShortcutVariant, nativeShortcutAction, type ShortcutActionId } from "../lib/keyboard-shortcuts";
import { buildInterruptCommand } from "../lib/interrupt-command";
import { numberedShortcut } from "../lib/shortcut-navigation";
import { SHORTCUT_DEFINITIONS } from "../lib/keyboard-shortcuts";
import { queueHistoryShortcutIsStandard, routeQueuedMessageHistory } from "../composer/queued-message-undo";
import { captureThreadActionOwner, executeThreadAction, THREAD_ACTION_IDS } from "../lib/thread-actions";
import { isModalTarget, isTopLevelModalOpen } from "../lib/shortcut-context";

const announceViewMode = (mode: string) => {
  pushToast(`View mode: ${mode.charAt(0).toUpperCase()}${mode.slice(1)}`, "info", 1200);
};

export const useKeyboardShortcuts = () => {
  useEffect(() => {
    const onFileSearch = (event: KeyboardEvent) => {
      const state = useAppStore.getState();
      if (state.settingsOpen && state.settingsTab === "shortcuts" && document.querySelector('[data-shortcut-recording="true"]')) return;
      if (event.isComposing || event.keyCode === 229 || !matchesShortcut(event, state.shortcutBindings.globalSearch)) return;
      if (window.__MINICODE_RUNTIME__?.desktop?.menu) {
        event.preventDefault(); event.stopPropagation();
        void window.__MINICODE_RUNTIME__.desktop.menu.execute("globalSearch");
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (capabilityFeatureEnabled(state.runtimeCapabilities, "global_search", true)) state.toggleQuickOpen();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
      const mod = e.metaKey || e.ctrlKey;
      const s = useAppStore.getState();
      const createConversationInCurrentMode = () => {
        s.createConversation({ appMode: "cowork", bindWorkspace: s.appMode === "code" && Boolean(s.workingDirectory) });
      };
      const match = (action: ShortcutActionId) => (s.appMode === "code" || !isCodeShortcut(action)) && matchesShortcut(e, s.shortcutBindings[action]);
      const editorFocused = Boolean(document.activeElement?.closest(".monaco-editor"));
      if (editorFocused && (match("saveFile") || match("saveAllFiles"))) {
        e.preventDefault();
        window.dispatchEvent(new Event(match("saveAllFiles") ? "editor:save-all" : "editor:save"));
        return;
      }
      if (match("undoAppAction") || match("redoAppAction")) {
        const direction = match("undoAppAction") ? "undo" : "redo";
        if (routeQueuedMessageHistory(direction, queueHistoryShortcutIsStandard(e, direction)) === "handled") {
          e.preventDefault(); e.stopPropagation();
        }
        return;
      }
      // Frameless renderer input uses the same owned command as a native menu click.
      const modalOpen = isTopLevelModalOpen(s);
      const threadAction = THREAD_ACTION_IDS.find(match);
      const nativeAction = nativeShortcutAction(e, s.shortcutBindings, s.appMode === "code");
      if (window.__MINICODE_RUNTIME__?.desktop?.menu && nativeAction) {
        if (threadAction && (modalOpen || isModalTarget(e.target))) return;
        e.preventDefault();
        void window.__MINICODE_RUNTIME__.desktop.menu.execute(nativeAction);
        return;
      }
      const routeToComposer = (action: () => void) => {
        window.dispatchEvent(new Event("composer:focus"));
        if (s.skillsMarketplaceOpen) useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
        const chat = s.panelSlots.find((slot) => slot.kind === "chat");
        if (s.appMode === "code") {
          s.setAppMode("cowork");
          if (chat) s.focusPanel(chat.id);
        }
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

      const numbered = numberedShortcut(e, s.primaryNumberShortcutTarget);
      if (numbered && !SHORTCUT_DEFINITIONS.some(shortcut => match(shortcut.id))) {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent(numbered.target === "tabs" ? "select-numbered-tab" : "select-numbered-chat", {
          detail: { index: numbered.index, conversationId: s.conversationId },
        }));
        return;
      }

      if (threadAction) {
        e.preventDefault();
        const owner = captureThreadActionOwner();
        if (owner) void executeThreadAction(threadAction, owner);
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
        routeToComposer(() => document.querySelector<HTMLElement>("[data-composer-input]")?.focus());
        return;
      }
      if (match("processDetail")) {
        e.preventDefault();
        const next = s.viewMode === "normal" ? "verbose" : s.viewMode === "verbose" ? "summary" : "normal";
        s.setViewMode(next);
        announceViewMode(next);
        return;
      }
      if (match("toggleDiff") || match("sidePanel")) {
        e.preventDefault();
        toggleChangesPanel();
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
      if (match("workspaceSearch")) {
        e.preventDefault();
        if (capabilityFeatureEnabled(s.runtimeCapabilities, "global_search", true)) s.openWorkspaceSearch();
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
      if (match("terminal")) {
        e.preventDefault();
        s.toggleTerminal();
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
        s.setLeftSidebarWidth(s.leftSidebarWidth > 0 ? 0 : s.leftSidebarExpandedWidth);
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
