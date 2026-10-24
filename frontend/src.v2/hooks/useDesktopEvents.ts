import { useEffect } from "react";
import { useAppStore } from "../stores";
import { desktopNotificationConversationVisible, isDesktop, onEmbeddedBrowserEvent } from "../desktop/runtime";
import { discardClosedBrowserPresentation, discardInactiveBrowserPresentation, focusBrowserAddressBar, openNewBrowserTab, presentExistingBrowserPage } from "../chat/openWebInBrowser";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { openSettings } from "../lib/settings-navigation";
import { useDesktopTerminalMirror } from "./useDesktopTerminalMirror";
import { sendClientCommand } from "../protocol/ws-outbox";
import { nativeMenuAccelerators, nativeMenuBindings, nativeShortcutOverrides } from "../lib/keyboard-shortcuts";
import { toggleChangesPanel } from "../lib/changes-panel";
import { consumeDictationProviderSettings } from "../lib/dictation-preferences";
import { pushToast } from "../overlays/ToastContainer";
import { routeQueuedMessageHistory } from "../composer/queued-message-undo";
import { installDesktopVoiceControl } from "../desktop/voice-control";
import { captureThreadActionOwner, executeThreadAction, THREAD_ACTION_EVENTS, THREAD_ACTION_IDS } from "../lib/thread-actions";
import { isModalTarget, isTopLevelModalOpen } from "../lib/shortcut-context";

const markViewedConversationRead = (isWindowFocused: boolean) => {
  const state = useAppStore.getState();
  const conversationId = state.conversationId;
  if (isWindowFocused && state.isConnected && conversationId
    && state.conversations.find(item => item.id === conversationId)?.hasUnreadTurn
    && desktopNotificationConversationVisible(conversationId, conversationId, state.settingsOpen, state.pendingConversationSwitchId)) {
    sendClientCommand({ type: "conversation.read_state.set", conversation_id: conversationId, has_unread_turn: false });
  }
};

export const useDesktopEvents = () => {
  useDesktopTerminalMirror();
  useEffect(() => {
    const navigation = window.__MINICODE_RUNTIME__?.initialNavigation;
    if (!navigation?.popout || !navigation.workspaceRoot) return;
    const current = useAppStore.getState();
    if (current.isConnected && !current.conversationId) current.setWorkingDirectory(navigation.workspaceRoot);
    return useAppStore.subscribe((state, previous) => {
      if (state.isConnected && !previous.isConnected && !state.conversationId) state.setWorkingDirectory(navigation.workspaceRoot!);
    });
  }, []);
  useEffect(() => installDesktopVoiceControl(), []);
  useEffect(() => {
    if (isDesktop()) return;
    const readViewedConversation = () => markViewedConversationRead(document.hasFocus());
    const unsubscribe = useAppStore.subscribe((state, previous) => {
      if (state.conversations !== previous.conversations || state.conversationId !== previous.conversationId
        || state.settingsOpen !== previous.settingsOpen || state.pendingConversationSwitchId !== previous.pendingConversationSwitchId
        || state.isConnected !== previous.isConnected) readViewedConversation();
      if (state.appMode !== previous.appMode || state.workbenchLayout !== previous.workbenchLayout || state.panelSlots !== previous.panelSlots) requestAnimationFrame(readViewedConversation);
    });
    window.addEventListener("focus", readViewedConversation);
    document.addEventListener("visibilitychange", readViewedConversation);
    readViewedConversation();
    return () => {
      unsubscribe(); window.removeEventListener("focus", readViewedConversation);
      document.removeEventListener("visibilitychange", readViewedConversation);
    };
  }, []);
  useEffect(() => {
    const consumeProviderSettings = () => { void consumeDictationProviderSettings().catch(error => pushToast(error instanceof Error ? error.message : String(error), "error")); };
    const toggleSummary = () => {
      const store = useAppStore.getState();
      store.setContextCardPinned(!store.contextCardPinned);
    };
    const navigateThread = (direction: -1 | 1) => {
      const store = useAppStore.getState();
      const chats = store.conversations.filter(chat => !chat.archived);
      const current = chats.findIndex(chat => chat.id === store.conversationId);
      const target = chats[current + direction];
      if (target) void store.requestConversationSwitch(target.id);
    };
    const openCommandMenu = () => {
      const store = useAppStore.getState();
      if (!store.commandPaletteOpen) store.toggleCommandPalette();
    };
    const findInActivePane = (action: "find" | "find-next" | "find-previous") => {
      const store = useAppStore.getState();
      const editor = store.appMode === "code" && store.panelSlots.find(slot => slot.focused)?.kind === "editor"
        && !document.activeElement?.closest(".mc-sidebar-right");
      window.dispatchEvent(new Event(editor ? `editor:${action}` : action === "find" ? "chat:request-search" : `chat:${action}`));
    };
    const handlers: Record<string, (event: Event) => void> = {
      ...Object.fromEntries(THREAD_ACTION_IDS.map(action => [THREAD_ACTION_EVENTS[action], () => {
        if (isTopLevelModalOpen(useAppStore.getState()) || isModalTarget(document.activeElement)) return;
        const owner = captureThreadActionOwner();
        if (owner) void executeThreadAction(action, owner);
      }])),
      "undo-app-action": () => {
        if (routeQueuedMessageHistory("undo") === "native") void window.__MINICODE_RUNTIME__!.desktop!.menu.execute("nativeTextUndo");
      },
      "redo-app-action": () => {
        if (routeQueuedMessageHistory("redo") === "native") void window.__MINICODE_RUNTIME__!.desktop!.menu.execute("nativeTextRedo");
      },
      "new-conversation": () => {
        const store = useAppStore.getState();
        store.createConversation({ appMode: "cowork", bindWorkspace: store.appMode === "code" && Boolean(store.workingDirectory) });
      },
      "quick-chat": () => {
        useAppStore.getState().createConversation({ appMode: "cowork", bindWorkspace: false });
        window.dispatchEvent(new Event("composer:focus"));
      },
      "open-settings": (event: Event) => {
        if ((event as CustomEvent<{ tab?: "provider" }>).detail?.tab === "provider") consumeProviderSettings();
        else openSettings();
      },
      "open-pets-settings": () => openSettings("pets"),
      "new-project": () => {
        const store = useAppStore.getState();
        if (store.settingsOpen) store.toggleSettings();
        useAppStore.setState({ skillsMarketplaceOpen: false, projectEditTarget: "create" });
      },
      "open-browser-tab": openNewBrowserTab,
      "focus-browser-address-bar": focusBrowserAddressBar,
      "toggle-terminal": () => {
        useAppStore.getState().toggleTerminal();
      },
      "toggle-bottom-panel": () => {
        const store = useAppStore.getState();
        if (!store.dockCollapsed) store.closeBottomDock();
        else store.openBottomTab(store.appMode === "code" ? store.activeBottomTab : "terminal");
      },
      "toggle-file-tree": () => {
        const store = useAppStore.getState();
        if (store.appMode === "code") store.setEditorExplorerOpen(!store.editorExplorerOpen);
        else if (store.rightPanelOpen && store.rightStackTab === "files") store.toggleRightPanel();
        else store.setRightStackTab("files");
      },
      "toggle-pinned-summary": toggleSummary,
      "toggle-side-panel": toggleChangesPanel,
      "toggle-sidebar": () => {
        const store = useAppStore.getState();
        store.setLeftSidebarWidth(store.leftSidebarWidth > 0 ? 0 : store.leftSidebarExpandedWidth);
      },
      "toggle-context": () => {
        const store = useAppStore.getState();
        if (store.rightPanelOpen && store.rightStackTab === "tasks") store.toggleRightPanel();
        else store.setRightStackTab("tasks");
      },
      "open-import-modal": () => {
        void openWorkspaceFolder();
      },
      "open-command-menu": openCommandMenu,
      "search-chats": openCommandMenu,
      "search-files": () => {
        const store = useAppStore.getState();
        if (!store.quickOpenVisible) store.toggleQuickOpen();
      },
      "open-shortcuts": () => {
        const store = useAppStore.getState();
        if (!store.shortcutsHelpOpen) store.toggleShortcutsHelp();
      },
      "find-in-thread": () => findInActivePane("find"),
      "find-next-in-thread": () => findInActivePane("find-next"),
      "find-previous-in-thread": () => findInActivePane("find-previous"),
      "previous-thread": () => navigateThread(-1),
      "next-thread": () => navigateThread(1),
      "open-extensions-marketplace": () => {
        const store = useAppStore.getState();
        if (!store.skillsMarketplaceOpen) store.toggleSkillsMarketplace();
        useAppStore.setState({ skillsMarketplaceTab: "plugins" });
      },
    };

    const listener = (e: Event) => {
      const handler = handlers[e.type];
      if (handler) handler(e);
    };

    for (const event of Object.keys(handlers)) {
      window.addEventListener(event, listener);
    }
    consumeProviderSettings();
    const desktopRuntime = window.__MINICODE_RUNTIME__?.desktop;
    let lastMenuState = "";
    const updateMenuState = () => {
      if (!desktopRuntime) return;
      const state = useAppStore.getState();
      const workspaceVisible = !state.settingsOpen && !state.skillsMarketplaceOpen && !state.projectsOpen;
      const menuState = {
        terminalEnabled: workspaceVisible && state.isConnected && Boolean(state.conversationId) && !state.pendingConversationSwitchId,
        bottomPanelToggleEnabled: workspaceVisible,
        newProjectEnabled: !state.pendingConversationSwitchId,
        editorFocused: Boolean(document.activeElement?.closest(".monaco-editor")),
        accelerators: nativeMenuAccelerators(state.shortcutBindings),
        acceleratorBindings: nativeMenuBindings(state.shortcutBindings),
        shortcutOverrides: nativeShortcutOverrides(state.shortcutBindings),
        shortcutRecording: Boolean(document.querySelector('[data-shortcut-recording="true"]')),
        workspaceRoot: state.workingDirectory,
      };
      const serialized = JSON.stringify(menuState);
      if (serialized === lastMenuState) return;
      lastMenuState = serialized;
      void desktopRuntime.menu.setState(menuState);
    };
    updateMenuState();
    const updateMenuAfterBlur = () => queueMicrotask(updateMenuState);
    document.addEventListener("focusin", updateMenuState);
    document.addEventListener("focusout", updateMenuAfterBlur);
    const removeMenuState = useAppStore.subscribe((state, previous) => {
      if (state.appMode !== previous.appMode || state.isConnected !== previous.isConnected
        || state.conversationId !== previous.conversationId || state.pendingConversationSwitchId !== previous.pendingConversationSwitchId
        || state.workingDirectory !== previous.workingDirectory
        || state.settingsOpen !== previous.settingsOpen || state.skillsMarketplaceOpen !== previous.skillsMarketplaceOpen
        || state.projectsOpen !== previous.projectsOpen || state.shortcutBindings !== previous.shortcutBindings) updateMenuState();
    });
    const observeShortcutRecording = new MutationObserver(updateMenuState);
    observeShortcutRecording.observe(document.body, { subtree: true, attributes: true, attributeFilter: ["data-shortcut-recording"], childList: true });
    let isNativeWindowFocused = false;
    const hideViewedNotifications = () => {
      const state = useAppStore.getState();
      const conversationId = state.conversationId;
      if (conversationId && desktopNotificationConversationVisible(conversationId, conversationId, state.settingsOpen, state.pendingConversationSwitchId)) {
        void desktopRuntime?.hideNotifications({ conversationId, onlyIfFocused: true });
        markViewedConversationRead(isNativeWindowFocused);
      }
    };
    const removeNotificationOwner = useAppStore.subscribe((state, previous) => {
      if (state.conversationId !== previous.conversationId || state.settingsOpen !== previous.settingsOpen || state.pendingConversationSwitchId !== previous.pendingConversationSwitchId
        || state.conversations !== previous.conversations || state.isConnected !== previous.isConnected) hideViewedNotifications();
      if (state.appMode !== previous.appMode || state.workbenchLayout !== previous.workbenchLayout || state.panelSlots !== previous.panelSlots || state.skillsMarketplaceOpen !== previous.skillsMarketplaceOpen) requestAnimationFrame(hideViewedNotifications);
    });
    const updateNativeFocus = (state: { isFocused: boolean }) => {
      isNativeWindowFocused = state.isFocused;
      if (state.isFocused) hideViewedNotifications();
    };
    const removeWindowFocus = desktopRuntime?.windowControls.onStateChanged(updateNativeFocus);
    void desktopRuntime?.windowControls.getState().then(updateNativeFocus);
    document.addEventListener("visibilitychange", hideViewedNotifications);
    const removeBrowserListener = onEmbeddedBrowserEvent((event) => {
      if (event.type === "presentation-requested") presentExistingBrowserPage(event);
      if (event.type === "closed") discardClosedBrowserPresentation(event);
    });
    const removeBrowserPresentationState = useAppStore.subscribe(discardInactiveBrowserPresentation);
    const removeDeepLinkListener = desktopRuntime?.onDeepLink(async (payload) => {
      if (!payload?.id || !payload.target) return;
      if (payload.target.kind === "conversation" && payload.target.conversationId) {
        await useAppStore.getState().requestConversationSwitch(payload.target.conversationId);
      } else if (payload.target.kind === "url" && /^https?:\/\//i.test(payload.target.url)) {
        await desktopRuntime.openExternal(payload.target.url);
      }
      await desktopRuntime.ackDeepLink(payload.id);
    });

    return () => {
      for (const event of Object.keys(handlers)) {
        window.removeEventListener(event, listener);
      }
      removeDeepLinkListener?.();
      removeMenuState();
      observeShortcutRecording.disconnect();
      if (desktopRuntime) {
        const state = useAppStore.getState();
        void desktopRuntime.menu.setState({ terminalEnabled: false, bottomPanelToggleEnabled: false, newProjectEnabled: false, editorFocused: false,
          accelerators: nativeMenuAccelerators(state.shortcutBindings), acceleratorBindings: nativeMenuBindings(state.shortcutBindings), shortcutOverrides: nativeShortcutOverrides(state.shortcutBindings), shortcutRecording: false, workspaceRoot: state.workingDirectory });
      }
      document.removeEventListener("focusin", updateMenuState);
      document.removeEventListener("focusout", updateMenuAfterBlur);
      removeWindowFocus?.();
      removeNotificationOwner();
      document.removeEventListener("visibilitychange", hideViewedNotifications);
      removeBrowserListener?.();
      removeBrowserPresentationState();
    };
  }, []);
};
