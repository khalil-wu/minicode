import { useAppStore } from "../stores";

export function toggleChangesPanel() {
  const state = useAppStore.getState();
  const maximizedEditor = state.appMode === "code" && state.panelSlots.find(slot => slot.maximized && slot.kind !== "chat");
  if (state.rightPanelOpen && state.rightStackTab === "diff" && !maximizedEditor) {
    state.toggleRightPanel();
    return;
  }
  if (maximizedEditor) state.togglePanelMaximized(maximizedEditor.id);
  state.setRightStackTab("diff");
}
