import { isDesktop, pickWorkspaceDirectory } from "../desktop/runtime";
import { showPrompt } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { toBackendPermissionMode } from "../protocol/permissions";

export const openWorkspaceFolder = async (): Promise<string | null> => {
  try {
    const workspacePath = isDesktop()
      ? await pickWorkspaceDirectory()
      : await showPrompt({
        title: "打开项目",
        message: "输入后端所在电脑上、已在桌面端授权的项目文件夹绝对路径。浏览器不会上传本机文件。",
        placeholder: "例如 C:\\Projects\\my-app 或 /home/user/my-app",
        defaultValue: useAppStore.getState().workingDirectory || "",
        confirmLabel: "打开项目",
      });
    if (!workspacePath?.trim()) return null;
    return activateWorkspaceFolder(workspacePath.trim());
  } catch (error) {
    pushToast(error instanceof Error ? error.message : "无法打开项目选择器。", "error", 5000);
    return null;
  }
};

export const activateWorkspaceFolder = async (workspacePath: string): Promise<string | null> => {
  try {
    const result = await sendClientCommandAwaitResult(
      { type: "workspace.set", path: workspacePath, permission_mode: toBackendPermissionMode(useAppStore.getState().permissionMode) },
      "workspace.set",
    );
    if (!commandResultSucceeded(result)) {
      const message = result.data?.error_code === "workspace_untrusted"
        ? "此项目尚未授权。请先在桌面端打开并信任该目录，然后在浏览器中重试。"
        : result.message || "无法打开工作区。";
      pushToast(message, "error", 5000);
      return null;
    }
    const activatedPath = typeof result.data?.workspace_root === "string" ? result.data.workspace_root : workspacePath;
    // conversation.switched owns the new conversation and workspace together.
    // Applying the directory again here can overwrite a later user selection.
    useAppStore.getState().setAppMode("code");
    pushToast(`已打开工作区：${activatedPath}`, "info", 2200);
    return activatedPath;
  } catch (error) {
    pushToast(error instanceof Error ? error.message : "无法打开工作区。", "error", 5000);
    return null;
  }
};
