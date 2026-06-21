import { useEffect } from "react";
import { useAppStore } from "../stores";
import { isDesktop } from "../desktop/runtime";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";

export const useDesktopEvents = () => {
  useEffect(() => {
    if (!isDesktop()) return;

    const handlers: Record<string, () => void> = {
      "new-conversation": () => {
        const store = useAppStore.getState();
        store.createConversation({ appMode: store.appMode, bindWorkspace: Boolean(store.workingDirectory) });
      },
      "open-settings": () => useAppStore.getState().toggleSettings(),
      "toggle-terminal": () => useAppStore.getState().toggleDock(),
      "toggle-sidebar": () => {
        const store = useAppStore.getState();
        store.setLeftSidebarWidth(store.leftSidebarWidth > 0 ? 0 : 320);
      },
      "open-import-modal": () => {
        void openWorkspaceFolder();
      },
      "open-extensions-marketplace": () => useAppStore.getState().toggleSkillsMarketplace(),
    };

    const listener = (e: Event) => {
      const handler = handlers[e.type];
      if (handler) handler();
    };

    for (const event of Object.keys(handlers)) {
      window.addEventListener(event, listener);
    }

    return () => {
      for (const event of Object.keys(handlers)) {
        window.removeEventListener(event, listener);
      }
    };
  }, []);
};
