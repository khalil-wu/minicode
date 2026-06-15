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
  const { conversationId, conversations } = useAppStore.getState();
  if (conversationId) {
    useAppStore.setState({
      conversations: conversations.map((conversation) =>
        conversation.id === conversationId
          ? {
              ...conversation,
              workspaceRoot: workspacePath,
              worktreePath: undefined,
              gitIsolated: false,
            }
          : conversation,
      ),
    });
  }
  const sent = sendClientCommand({ type: "workspace.set", path: workspacePath });
  if (sent) {
    useAppStore.getState().setAppMode("code");
    pushToast(`Opened workspace: ${workspacePath}`, "success", 2200);
  }
  return workspacePath;
};
