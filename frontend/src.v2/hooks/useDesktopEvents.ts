import { useEffect } from "react";
import { useAppStore } from "../stores";
import { desktopNotificationConversationVisible, isDesktop, onEmbeddedBrowserEvent } from "../desktop/runtime";
import { discardClosedBrowserPresentation, discardInactiveBrowserPresentation, presentExistingBrowserPage } from "../chat/openWebInBrowser";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { openSettings } from "../lib/settings-navigation";
import { useDesktopTerminalMirror } from "./useDesktopTerminalMirror";
import { sendClientCommand } from "../protocol/ws-outbox";

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
    if (!isDesktop()) return;

    const handlers: Record<string, () => void> = {
      "new-conversation": () => {
        const store = useAppStore.getState();
        store.createConversation({ appMode: "cowork", bindWorkspace: store.appMode === "code" && Boolean(store.workingDirectory) });
      },
      "quick-chat": () => {
        useAppStore.getState().createConversation({ appMode: "cowork", bindWorkspace: false });
        window.dispatchEvent(new Event("composer:focus"));
      },
      "open-settings": () => openSettings(),
      "toggle-terminal": () => {
        const store = useAppStore.getState();
        if (!store.dockCollapsed && store.activeBottomTab === "terminal") store.closeBottomDock();
        else store.openBottomTab("terminal");
      },
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
      "open-extensions-marketplace": () => {
        const store = useAppStore.getState();
        if (!store.skillsMarketplaceOpen) store.toggleSkillsMarketplace();
        useAppStore.setState({ skillsMarketplaceTab: "plugins" });
      },
    };

    const listener = (e: Event) => {
      const handler = handlers[e.type];
      if (handler) handler();
    };

    for (const event of Object.keys(handlers)) {
      window.addEventListener(event, listener);
    }
    const desktopRuntime = window.__MINICODE_RUNTIME__?.desktop;
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
      removeWindowFocus?.();
      removeNotificationOwner();
      document.removeEventListener("visibilitychange", hideViewedNotifications);
      removeBrowserListener?.();
      removeBrowserPresentationState();
    };
  }, []);
};
