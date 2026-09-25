import { FolderMinus } from "lucide-react";
import { ContextMenu } from "../components/ContextMenu";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { pushToast } from "../overlays/ToastContainer";

export const WorkspaceContextMenu = ({ path, position, onClose }: {
  path: string;
  position: { x: number; y: number };
  onClose: () => void;
}) => {
  const remove = async () => {
    try {
      const result = await sendClientCommandAwaitResult({
        type: "workspace.recent.remove", path, preserve_history: true,
      }, "workspace.recent.remove");
      if (!commandResultSucceeded(result)) throw new Error(result.message || "无法移除工作区。");
      if (result.data?.closed_active) useAppStore.getState().setAppMode("cowork");
      pushToast("工作区已移除，历史会话保留在项目的 .minicode 中。重新打开项目即可继续。", "success", 5000);
    } catch (error) {
      pushToast(error instanceof Error ? error.message : "无法移除工作区。", "error", 5000);
    }
  };
  return <ContextMenu position={position} onClose={onClose} items={[{
    label: "移除工作区", icon: <FolderMinus size={15} />, onClick: () => { void remove(); },
  }]} />;
};
