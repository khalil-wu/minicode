import { useI18n } from "../i18n";
import { FolderMinus } from "lucide-react";
import { ContextMenu } from "../components/ContextMenu";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";

export const WorkspaceContextMenu = ({ path, position, anchor, onClose }: {
  path: string;
  position: { x: number; y: number };
  anchor?: HTMLElement;
  onClose: () => void;
}) => {
  const { t: tr } = useI18n();
  const remove = async () => {
    try {
      const result = await sendClientCommandAwaitResult({
        type: "workspace.recent.remove", path,
      }, "workspace.recent.remove");
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("无法移除工作区。"));
    } catch (error) {
      pushToast(error instanceof Error ? error.message : tr("无法移除工作区。"), "error", 5000);
    }
  };
  return <ContextMenu position={position} anchor={anchor} onClose={onClose} items={[{
    label: tr("移除工作区"), icon: <FolderMinus size={15} />, onClick: () => { void remove(); },
  }]} />;
};
