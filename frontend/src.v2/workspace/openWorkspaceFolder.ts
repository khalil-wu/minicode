import { pickWorkspaceDirectory } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";

export const openWorkspaceFolder = async (): Promise<string | null> => {
  const workspacePath = await pickWorkspaceDirectory();
  if (!workspacePath) return null;

  const sent = sendClientCommand({ type: "workspace.set", path: workspacePath });
  if (sent) {
    // Bind the active conversation immediately. The imported event enriches
    // the workspace metadata later, but the next agent turn must already carry
    // the selected workspace root.
    useAppStore.getState().setWorkingDirectory(workspacePath);
    useAppStore.getState().setAppMode("code");
    pushToast(`Opening workspace: ${workspacePath}`, "info", 2200);
    return workspacePath;
  }
  return null;
};
