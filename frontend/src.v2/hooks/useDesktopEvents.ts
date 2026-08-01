import { useEffect } from "react";
import { useAppStore } from "../stores";
import { isDesktop } from "../desktop/runtime";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { openSettings } from "../lib/settings-navigation";
import { LEFT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";

export const useDesktopEvents = () => {
  useEffect(() => {
    if (!isDesktop()) return;

    const handlers: Record<string, () => void> = {
      "new-conversation": () => {
        const store = useAppStore.getState();
        store.createConversation({ appMode: store.appMode, bindWorkspace: Boolean(store.workingDirectory) });
      },
      "open-settings": () => openSettings(),
      "toggle-terminal": () => {
        const store = useAppStore.getState();
        if (!store.dockCollapsed && store.activeBottomTab === "terminal") store.closeBottomDock();
        else store.openBottomTab("terminal");
      },
      "toggle-sidebar": () => {
        const store = useAppStore.getState();
        store.setLeftSidebarWidth(store.leftSidebarWidth > 0 ? 0 : LEFT_SIDEBAR_DEFAULT_WIDTH);
      },
      "open-import-modal": () => {
        void openWorkspaceFolder();
      },
      "open-extensions-marketplace": () => {
        const store = useAppStore.getState();
        if (!store.skillsMarketplaceOpen) store.toggleSkillsMarketplace();
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
    };
  }, []);
};
