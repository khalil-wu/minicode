import { pickWorkspaceDirectory } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";

export const openWorkspaceFolder = async (): Promise<string | null> => {
  const workspacePath = await pickWorkspaceDirectory();
  if (!workspacePath) return null;

  const sent = sendClientCommand({ type: "workspace.set", path: workspacePath });
  if (sent) {
    useAppStore.getState().setAppMode("code");
    pushToast(`Opening workspace: ${workspacePath}`, "info", 2200);
    return workspacePath;
  }
  return null;
};
