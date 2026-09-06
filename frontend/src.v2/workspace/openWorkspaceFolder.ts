import { pickWorkspaceDirectory } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";

export const openWorkspaceFolder = async (): Promise<string | null> => {
  const workspacePath = await pickWorkspaceDirectory();
  if (!workspacePath) return null;
  return activateWorkspaceFolder(workspacePath);
};

export const activateWorkspaceFolder = async (workspacePath: string): Promise<string | null> => {
  try {
    const result = await sendClientCommandAwaitResult(
      { type: "workspace.set", path: workspacePath },
      "workspace.set",
    );
    if (!commandResultSucceeded(result)) {
      pushToast(result.message || "无法打开工作区。", "error", 5000);
      return null;
    }
    const activatedPath = typeof result.data?.workspace_root === "string" ? result.data.workspace_root : workspacePath;
    useAppStore.getState().setWorkingDirectory(activatedPath);
    useAppStore.getState().setAppMode("code");
    pushToast(`已打开工作区：${activatedPath}`, "info", 2200);
    return activatedPath;
  } catch (error) {
    pushToast(error instanceof Error ? error.message : "无法打开工作区。", "error", 5000);
    return null;
  }
};
