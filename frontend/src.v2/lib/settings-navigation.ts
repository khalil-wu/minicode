import { useAppStore } from "../stores";
import type { Tab } from "../overlays/settingsShared";

export const openSettings = (tab?: Tab) => {
  const state = useAppStore.getState();
  if (!state.settingsOpen) state.toggleSettings();
  if (!tab) return;
  window.setTimeout(() => {
    window.dispatchEvent(new CustomEvent<Tab>("minicode:settings-tab", { detail: tab }));
  }, 0);
};

