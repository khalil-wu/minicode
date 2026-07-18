import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";

export const openAutomations = () => {
  const state = useAppStore.getState();
  if (!state.automationsOpen) state.toggleAutomations();
  sendClientCommand({ type: "scheduler.list" });
};

