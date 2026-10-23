import type { AppStore } from "../stores/types";
import { capabilityFeatureEnabled } from "../protocol/capabilities";

export const isModalTarget = (target: EventTarget | null): boolean =>
  target instanceof HTMLElement
  && Boolean(target.closest("[role='dialog'], .modal-content, .overlay-backdrop, .settings-workspace"));

export const isTopLevelModalOpen = (state: AppStore): boolean =>
  state.commandPaletteOpen || state.quickOpenVisible || state.settingsOpen
  || state.shortcutsHelpOpen || state.liveArtifactsOpen
  || (state.agentEditorOpen && capabilityFeatureEnabled(state.runtimeCapabilities, "agent_editor", true));
