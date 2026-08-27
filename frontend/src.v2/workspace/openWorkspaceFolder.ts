import { pickDirectory, trustWorkspace } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";

export const openWorkspaceFolder = async (): Promise<string | null> => {
  const selectedPath = await pickDirectory();
  if (!selectedPath) return null;

  const trustedPath = await trustWorkspace(selectedPath);
  const workspacePath = trustedPath || selectedPath;

  const sent = sendClientCommand({ type: "workspace.set", path: workspacePath });
  if (sent) {
    useAppStore.getState().setAppMode("code");
    pushToast(`Opening workspace: ${workspacePath}`, "info", 2200);
    return workspacePath;
  }
  return null;
};
