import { useAppStore } from "../stores";
import type { SkillInfo } from "../stores/types";

export const selectSkillForComposer = (skill: SkillInfo) => {
  const state = useAppStore.getState();
  state.addSelectedSkill({ name: skill.name, path: skill.path, description: skill.description, sourceLevel: skill.source_level });
  const chat = state.panelSlots.find((slot) => slot.kind === "chat");
  if (chat) {
    const maximized = state.panelSlots.find((slot) => slot.maximized && slot.id !== chat.id);
    if (maximized) state.togglePanelMaximized(maximized.id);
    state.focusPanel(chat.id);
  } else state.addPanel({ id: "main-chat", kind: "chat", label: "对话" });
  useAppStore.setState({ settingsOpen: false, skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
  window.requestAnimationFrame(() => window.dispatchEvent(new Event("composer:focus")));
};
