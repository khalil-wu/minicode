import { useAppStore } from "../stores";
import type { Tab } from "../overlays/settingsShared";
import type { RightStackTab } from "../stores/types";
import { isCompactWorkbenchViewport } from "../stores/shared-helpers";

export const openSettings = (tab?: Tab) => {
  const state = useAppStore.getState();
  if (tab) state.setSettingsTab(tab);
  if (!state.settingsOpen) state.toggleSettings();
};


export const openRightPanelFromSettings = (tab: RightStackTab) => {
  const state = useAppStore.getState();
  if (isCompactWorkbenchViewport()) {
    if (state.rightPanelOpen) state.toggleRightPanel();
    if (useAppStore.getState().settingsOpen) useAppStore.getState().toggleSettings();
    window.setTimeout(() => useAppStore.getState().setRightStackTab(tab), 0);
    return;
  }
  state.setRightStackTab(tab);
  if (useAppStore.getState().settingsOpen) useAppStore.getState().toggleSettings();
};
