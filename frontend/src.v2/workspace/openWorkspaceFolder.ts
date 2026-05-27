import { pickDirectory, trustWorkspace } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";

export const openWorkspaceFolder = async (): Promise<string | null> => {
  const selectedPath = await pickDirectory();
  if (!selectedPath) return null;

  const trustedPath = await trustWorkspace(selectedPath);
  const workspacePath = trustedPath || selectedPath;

  useAppStore.getState().setWorkingDirectory(workspacePath);
  const sent = sendClientCommand({ type: "workspace.set", path: workspacePath });
  if (sent) {
    pushToast(`Opened workspace: ${workspacePath}`, "success", 2200);
  }
  return workspacePath;
};
